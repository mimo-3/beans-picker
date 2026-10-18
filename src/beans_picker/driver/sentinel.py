"""Activation guard."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Final

import anyio

from beans_picker._aio import Sleep
from beans_picker._proc import Runner, run
from beans_picker._text import DIGIT, WS, trim
from beans_picker.driver.types import Activation, ForegroundLog, carry_foreground

type FrontSampler = Callable[[], Awaitable[int | None]]
type Restorer = Callable[[int], Awaitable[bool]]
"""Puts the app with this pid in front; False when it could not."""

RESTORE_CHECKS: Final = 6
RESTORE_CHECK_INTERVAL_S: Final = 0.05
STOP_WAIT_S: Final = 2.0
"""How long `stop` waits for a sample still putting the previous app back before it cancels it."""
MAX_ACTIVATIONS: Final = 16
"""Activations kept per watch; an app that keeps coming forward does not grow the report without bound."""

_log = logging.getLogger(__name__)

PID_LINE: Final = re.compile(f'"pid"{WS}*={WS}*({DIGIT}+)')


async def front_pid(*, runner: Runner = run) -> int | None:
    """The pid of the frontmost app, from LaunchServices; None when it cannot be read."""
    try:
        asn = trim((await runner(("lsappinfo", "front"))).stdout)
        out = await runner(("lsappinfo", "info", "-only", "pid", asn))
    except Exception:
        return None
    m = PID_LINE.search(out.stdout)
    return int(m.group(1)) if m else None


def now_utc() -> str:
    now = datetime.now(UTC)
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


class ActivationSentinel:
    """Watches pids, records each time one comes to the front, and puts the app in front before back each time.

    While a call that delivers in the foreground is running, the previous app is not put back: that would move
    the keys or clicks to it. The put-back then happens in the sample after the call.
    """

    def __init__(
        self,
        sample_front: FrontSampler = front_pid,
        interval_ms: int = 80,
        *,
        sleep: Sleep = asyncio.sleep,
        restore: Restorer | None = None,
        restore_sleep: Sleep = asyncio.sleep,
        stop_wait_s: float = STOP_WAIT_S,
    ) -> None:
        self.restore = restore
        self._restore_sleep = restore_sleep
        self._sample_front = sample_front
        self._interval_s = interval_ms / 1000
        self._sleep = sleep
        self._stop_wait_s = stop_wait_s
        self._pids: set[int] = set()
        self._ticker: asyncio.Task[None] | None = None
        self._periodic: set[asyncio.Task[ForegroundLog]] = set()
        self._probes: set[asyncio.Task[None]] = set()
        self._inflight: asyncio.Task[None] | None = None
        # Bumped by `stop`, so a probe that started under an earlier watch never records anything.
        self._generation = 0
        self._current = "start"
        # The last app seen in front that is not watched: the one to put back.
        self._previous: int | None = None
        self._activations: list[Activation] = []
        self._truncated = False
        # A watched app is in front now: the next samples belong to the activation already recorded.
        self._in_front = False
        # A foreground delivery is running: record, but do not put the previous app back yet.
        self._holding = False
        # One put-back try is allowed per activation and per guarded call; a failed one is not hammered.
        self._may_put_back = False

    @property
    def activations(self) -> tuple[Activation, ...]:
        """Every activation recorded since the watch began, oldest first."""
        return tuple(self._activations)

    @property
    def watching(self) -> bool:
        return bool(self._pids)

    def log(self) -> ForegroundLog:
        return ForegroundLog(tuple(self._activations), self._truncated)

    async def watch(self, pid: int) -> None:
        """Start watching `pid`."""
        if not self._pids:
            self._activations = []
            self._truncated = False
            self._previous = None
            self._in_front = False
            self._holding = False
            self._may_put_back = False
        front = await self._sample_front()
        if front == pid:
            return
        if front is not None and front not in self._pids:
            self._previous = front
        self._pids.add(pid)
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick())

    async def stop(self) -> ForegroundLog:
        """Stop watching every pid; returns the activations recorded during the watch.

        A sample still putting the previous app back is given up to `stop_wait_s` to finish first.
        A final sample then puts back an app left in front by an interrupted foreground delivery.
        """
        with anyio.CancelScope(shield=True):
            log = await self._stop()
        return log

    async def _stop(self) -> ForegroundLog:
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        running = self._inflight
        if running is not None and not running.done():
            await asyncio.wait({running}, timeout=self._stop_wait_s)
        self._holding = False
        if self._pids and self.restore is not None:
            await self.settle()
        for task in self._periodic:
            task.cancel()
        for probe in self._probes:
            probe.cancel()
        self._inflight = None
        self._generation += 1
        self._pids.clear()
        self._holding = False
        log = self.log()
        self._activations = []
        self._truncated = False
        return log

    async def begin(self, tool: str, *, foreground: bool = False) -> None:
        """Name the driver call now running, so an activation says which route caused it.

        `foreground`: the call delivers in the foreground, so the previous app stays behind until it returns.
        """
        self._current = tool
        self._holding = foreground
        # Claim the hold before yielding, so no new probe can start a restore.
        # A restore already submitted must finish before any foreground input is sent.
        running = self._inflight
        if foreground and running is not None and not running.done():
            await asyncio.wait({running})

    def end(self, tool: str) -> None:
        self._current = f"after {tool}"
        self._holding = False
        self._may_put_back = True

    async def settle(self) -> None:
        """Take a bounded final sample, including when the caller's cancel scope has ended.

        Focus recovery is best effort and must not replace the action's result or cancellation.
        """
        with anyio.move_on_after(self._stop_wait_s, shield=True):
            try:
                await self.sample()
            except Exception as err:
                _log.debug("final front sample failed: %s", type(err).__name__)

    async def sample(self) -> tuple[Activation, ...]:
        """Take one sample right now (on top of the periodic ones); returns the activations recorded so far."""
        # A probe in flight: wait for it (and for its restore), then take a fresh one.
        while (running := self._inflight) is not None and not running.done():
            await asyncio.wait({running})
        if not self._pids:
            return self.activations
        probe = asyncio.create_task(self._probe(self._current, self._generation))
        self._probes.add(probe)
        probe.add_done_callback(self._probe_done)
        self._inflight = probe
        # A caller that goes away does not cancel the probe; `stop` does.
        await asyncio.wait({probe})
        if not probe.cancelled() and (err := probe.exception()) is not None:
            raise err
        return self.activations

    def _probe_done(self, probe: asyncio.Task[None]) -> None:
        self._probes.discard(probe)
        if self._inflight is probe:
            self._inflight = None
        if not probe.cancelled():
            probe.exception()  # retrieved here too, for a probe whose caller went away

    async def _probe(self, during: str, generation: int) -> None:
        front = await self._sample_front()
        if generation != self._generation or front is None:
            return
        if front not in self._pids:
            self._previous = front
            self._in_front = False
            return
        if not self._in_front:
            self._in_front = True
            self._may_put_back = True
            if len(self._activations) < MAX_ACTIVATIONS:
                self._activations.append(Activation(pid=front, during=during, at=now_utc()))
            else:
                self._truncated = True
            _log.debug("pid %d came to the front during %s", front, during)
        if self._holding or not self._may_put_back or self._previous is None:
            return
        self._may_put_back = False
        if not await self._put_back(self._previous) or generation != self._generation:
            return
        self._in_front = False
        if self._activations and not self._truncated:
            self._activations[-1] = dataclasses.replace(self._activations[-1], restored=True)

    async def _put_back(self, pid: int) -> bool:
        """Activate `pid` and read the front app again: a request the system accepted is not yet an app in front."""
        # An app that came under watch after it was seen in front is never brought forward.
        if self.restore is None or pid in self._pids:
            return False
        try:
            if not await self.restore(pid):
                return False
        except Exception as err:
            _log.debug("putting the previous app back failed: %s", type(err).__name__)
            return False
        for _ in range(RESTORE_CHECKS):
            if await self._sample_front() == pid:
                return True
            await self._restore_sleep(RESTORE_CHECK_INTERVAL_S)
        return False

    async def _tick(self) -> None:
        while True:
            await self._sleep(self._interval_s)
            task = asyncio.create_task(self._periodic_sample())
            self._periodic.add(task)
            task.add_done_callback(self._periodic_done)

    async def _periodic_sample(self) -> ForegroundLog:
        await self.sample()
        return self.log()

    def _periodic_done(self, task: asyncio.Task[ForegroundLog]) -> None:
        self._periodic.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            _log.debug("periodic front sample failed: %s", type(err).__name__)


class Watch:
    """What `watching` saw, filled in when the block ends."""

    __slots__ = ("log",)

    def __init__(self) -> None:
        self.log = ForegroundLog()


@asynccontextmanager
async def watching(sentinel: ActivationSentinel, pid: int | None) -> AsyncIterator[Watch]:
    """Watch `pid` (nothing when None) for the block; a failure carries the activations seen before it."""
    seen = Watch()
    if pid is None:
        yield seen
        return
    await sentinel.watch(pid)
    try:
        yield seen
    except Exception as err:
        seen.log = await sentinel.stop()
        carry_foreground(err, seen.log)
        raise
    except BaseException:
        await sentinel.stop()
        raise
    seen.log = await sentinel.stop()
