"""One Session per server process: a cua-driver connection, a Jev client, and a queue that serializes tool calls."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from beans_picker._aio import Clock, Sleep
from beans_picker._proc import Runner, run
from beans_picker.driver.app import AppTarget, ensure_app, pick_window, resolve_bundle
from beans_picker.driver.mcp import CuaDriver, Driver
from beans_picker.driver.sentinel import front_pid
from beans_picker.driver.types import windows_of
from beans_picker.errors import BeansPickerError, ToolError
from beans_picker.jev.client import JevClient, JevUsage
from beans_picker.jev.rank import Asker
from beans_picker.menus.menukeys import MenuKeys
from beans_picker.observe.exacttext import ExactText
from beans_picker.observe.helpers import Helpers
from beans_picker.observe.snapshot import observe
from beans_picker.observe.types import Snapshot
from beans_picker.paths import Paths
from beans_picker.tools.args import TargetArgs

_log = logging.getLogger(__name__)

type Connect = Callable[[], Awaitable[Driver]]


class JevLike(Asker, Protocol):
    """What a Session owns for Jev: tools take usage after each call, `close` releases it."""

    def take_usage(self) -> JevUsage: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Target:
    pid: int
    window_id: int


class ToolSession(Protocol):
    """What the tools use of a Session."""

    types_into_web_fields: set[int]
    """Apps whose web pages ignored an AXValue write to a text field: their fields are typed into."""

    @property
    def paths(self) -> Paths: ...

    @property
    def menu_keys(self) -> MenuKeys: ...

    async def target(self, args: TargetArgs) -> Target: ...

    async def snapshot(self, t: Target) -> Snapshot: ...

    async def driver(self) -> Driver: ...

    def jev(self) -> JevLike: ...


async def _connect_driver() -> Driver:
    return await CuaDriver.connect()


class Session:
    """The process-wide state behind the tools; see `exclusive` and `close` for the call lifecycle."""

    def __init__(
        self,
        connect: Connect | None = None,
        *,
        paths: Paths | None = None,
        jev_factory: Callable[[], JevLike] | None = None,
        runner: Runner = run,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._connect: Connect = connect if connect is not None else _connect_driver
        self._jev_factory: Callable[[], JevLike] = jev_factory if jev_factory is not None else JevClient
        self._runner = runner
        self._sleep = sleep
        self.types_into_web_fields: set[int] = set()
        self._paths = paths if paths is not None else Paths.default()
        self.helpers = Helpers(self._paths, runner=runner)
        self.exact_text = ExactText(self.helpers, self._paths, runner=runner, clock=clock)
        self._menu_keys = MenuKeys(self.helpers, self._paths, runner=runner)
        self._driver_task: asyncio.Task[Driver] | None = None
        self._jev: JevLike | None = None
        self._lock = asyncio.Lock()
        self._bodies: set[asyncio.Task[object]] = set()
        self._running: asyncio.Task[object] | None = None
        self._closing = False

    @property
    def paths(self) -> Paths:
        return self._paths

    @property
    def menu_keys(self) -> MenuKeys:
        return self._menu_keys

    async def exclusive[R](self, fn: Callable[[], Awaitable[R]]) -> R:
        """Runs `fn` after every earlier call has finished (first come, first served)."""
        if self._closing:
            raise BeansPickerError("server is shutting down")

        async def body() -> R:
            async with self._lock:
                current = asyncio.current_task()
                self._running = current
                try:
                    return await fn()
                finally:
                    self._running = None

        task = asyncio.ensure_future(body())
        self._bodies.add(task)
        task.add_done_callback(self._forget)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A call still waiting for its turn is dropped; one already running finishes.
            if self._running is not task:
                task.cancel()
            raise

    def _forget(self, task: asyncio.Task[object]) -> None:
        self._bodies.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved here, so an abandoned body's error is never "never retrieved"

    async def driver(self) -> Driver:
        """The cua-driver connection, opened on first use; a failed open is retried by the next call."""
        if self._driver_task is None:
            self._driver_task = asyncio.ensure_future(self._connect())
        task = self._driver_task
        try:
            return await task
        except BaseException:
            if self._driver_task is task and task.done():
                self._driver_task = None
            raise

    def jev(self) -> JevLike:
        """The Jev client, created on first use; raises JevUnavailable when no key is configured."""
        if self._jev is None:
            self._jev = self._jev_factory()
        return self._jev

    async def target(self, args: TargetArgs) -> Target:
        """The window a call acts on: by pid, by app (launched in the background if needed), or by window id."""
        driver = await self.driver()
        window_id = args.get("windowId")
        if "pid" in args:
            pid = args["pid"]
            return Target(pid, await _window_of(driver, pid, window_id))
        if "app" in args:
            app = args["app"]
            bundle_id = resolve_bundle(app)
            app_target = AppTarget(bundle_id=bundle_id) if bundle_id else AppTarget(name=app)
            ctx = await ensure_app(driver, app_target, runner=self._runner, sleep=self._sleep)
            if window_id is None:
                return Target(ctx.pid, ctx.window_id)
            return Target(ctx.pid, await _window_of(driver, ctx.pid, window_id))
        if window_id is not None:
            windows = windows_of(await driver.must("list_windows", {})) or []
            w = next((x for x in windows if x.window_id == window_id), None)
            if w is None:
                raise ToolError("window_not_found", f"no window with id {window_id}")
            return Target(w.pid, w.window_id)
        raise ToolError("bad_target", "give app, pid or windowId")

    async def snapshot(self, t: Target) -> Snapshot:
        """A fresh snapshot of the target window, with the app's menu shortcuts learned."""
        driver = await self.driver()
        await self._menu_keys.learn(t.pid)
        return await observe(
            driver,
            t.pid,
            t.window_id,
            front_pid=functools.partial(front_pid, runner=self._runner),
            read_exact=self.exact_text.read_fields,
        )

    async def close(self) -> None:
        """Ends the session: cancels queued and running work, then closes the sentinel, driver and Jev client."""
        if self._closing:
            return
        self._closing = True
        running = self._running
        for task in list(self._bodies):
            if task is not running:
                task.cancel()
        if running is not None:
            running.cancel()
            await asyncio.wait([running])
        for owner in (self._menu_keys, self.helpers):
            try:
                await owner.close()
            except Exception as err:
                _log.debug("stopping background work failed: %s", type(err).__name__)
        driver = await self._settled_driver()
        if driver is not None:
            try:
                driver.sentinel.stop()
            except Exception as err:
                _log.debug("stopping the sentinel failed: %s", type(err).__name__)
            try:
                await driver.close()
            except Exception as err:
                _log.debug("closing cua-driver failed: %s", type(err).__name__)
        if self._jev is not None:
            try:
                await self._jev.aclose()
            except Exception as err:
                _log.debug("closing the Jev client failed: %s", type(err).__name__)

    async def _settled_driver(self) -> Driver | None:
        task = self._driver_task
        if task is None:
            return None
        if not task.done():
            task.cancel()
            await asyncio.wait([task])
        if task.cancelled() or task.exception() is not None:
            return None
        return task.result()


async def _window_of(driver: Driver, pid: int, window_id: int | None) -> int:
    windows = windows_of(await driver.must("list_windows", {"pid": pid}))
    if windows is None:
        raise BeansPickerError(f"list_windows gave no window list for pid {pid}")
    if window_id is not None:
        if not any(w.window_id == window_id for w in windows):
            raise ToolError("window_not_found", f"pid {pid} has no window {window_id}")
        return window_id
    w = pick_window(windows)
    if w is None:
        raise ToolError("window_not_found", f"pid {pid} has no usable window")
    return w.window_id
