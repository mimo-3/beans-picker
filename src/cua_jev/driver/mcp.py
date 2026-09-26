"""One persistent MCP stdio session with cua-driver."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TextIO

import anyio
from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client
from mcp.types import CONNECTION_CLOSED, CallToolResult, Implementation, TextContent

from cua_jev import __version__, config
from cua_jev._json import JsonObject
from cua_jev._numbers import round_half_up, scalar_text
from cua_jev.driver.sentinel import ActivationSentinel
from cua_jev.driver.types import ToolOk, ToolRefused, ToolResult, as_obj, get_obj, get_str
from cua_jev.errors import DriverError, ForegroundViolation

_log = logging.getLogger(__name__)

READ_TOOLS: Final = frozenset(
    {"get_window_state", "list_windows", "get_accessibility_tree", "list_apps", "get_config", "get_screen_size"}
)
"""Calls that only read; they are retried once after a reconnect and never guarded."""

FOREGROUND_TOOLS: Final = frozenset({"bring_to_front", "activate", "activate_app", "invoke_menu", "move_cursor"})
"""Tools that change the frontmost app, raise a window or move the real cursor."""

DEFAULT_SESSION: Final = "cua-jev"

_CLIENT_INFO: Final = Implementation(name="cua-jev", version=__version__)
_TRANSPORT_MESSAGE: Final = re.compile("closed|EPIPE|not connected|ECONNRESET", re.IGNORECASE | re.ASCII)
_TRANSPORT_TYPES: Final = (ConnectionError, anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
_BASE36: Final = "0123456789abcdefghijklmnopqrstuvwxyz"
END_SESSION_WAIT_S: Final = 5.0
"""How long shutdown waits for `end_session`, the one request with a limit: a daemon that stopped
answering must not keep the server from exiting."""


class Driver(Protocol):
    """What the rest of the package needs from a cua-driver connection."""

    sentinel: ActivationSentinel

    async def call(self, tool: str, args: Mapping[str, object] | None = None) -> ToolResult: ...

    async def must(self, tool: str, args: Mapping[str, object] | None = None) -> JsonObject: ...

    async def close(self) -> None: ...


type Connect = Callable[[], Awaitable[Driver]]


def steals_focus(tool: str, args: Mapping[str, object]) -> bool:
    """True for a call that would bring an app to the front."""
    return tool in FOREGROUND_TOOLS or args.get("delivery_mode") == "foreground"


def is_transport_error(err: BaseException) -> bool:
    """True when the connection to cua-driver is gone, as opposed to an error answer."""
    for e in _chain(err):
        if isinstance(e, _TRANSPORT_TYPES):
            return True
        if isinstance(e, MCPError) and e.code == CONNECTION_CLOSED:
            return True
        if _TRANSPORT_MESSAGE.search(str(e)):
            return True
    return False


def _chain(err: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    todo = [err]
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        if isinstance(e, BaseExceptionGroup):
            todo.extend(reversed(e.exceptions))
        if e.__cause__ is not None:
            todo.append(e.__cause__)


def _first_leaf(group: ExceptionGroup[Exception]) -> Exception:
    leaves: list[Exception] = []
    todo: list[Exception] = [group]
    while todo:
        e = todo.pop()
        if isinstance(e, ExceptionGroup):
            todo.extend(reversed(e.exceptions))
        else:
            leaves.append(e)
    return leaves[0] if len(leaves) == 1 else group


def _truthy(value: object) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, list | dict):
        return True
    return bool(value)


def _base36(n: int) -> str:
    digits: list[str] = []
    while True:
        n, r = divmod(n, 36)
        digits.append(_BASE36[r])
        if n == 0:
            return "".join(reversed(digits))


def _open_devnull() -> TextIO:
    return Path(os.devnull).open("w", encoding="utf-8")


class _LenientSession(ClientSession):
    """Skips output-schema checks: cua-driver's schemas use formats outside JSON Schema (`uint64`)."""

    async def validate_tool_result(self, name: str, result: CallToolResult) -> None:
        return None


class _ConnectionLost(ConnectionError): ...


class _Connection:
    """Opens and holds the transport in one owner task: the SDK's cancel scopes must not cross tasks."""

    def __init__(self, bin_path: str) -> None:
        self._bin = bin_path
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self._owner: asyncio.Task[None] | None = None
        self._session: ClientSession | None = None
        self._ended = False

    @classmethod
    async def open(cls, bin_path: str) -> _Connection:
        conn = cls(bin_path)
        conn._owner = asyncio.create_task(conn._own(), name="cua-driver connection")
        try:
            conn._session = await asyncio.shield(conn._ready)
        except BaseException:
            await conn.close()
            if conn._ready.done() and not conn._ready.cancelled():
                conn._ready.exception()  # retrieved: the caller sees it (or its own cancellation)
            raise
        return conn

    def session(self) -> ClientSession:
        if self._ended or self._session is None:
            raise _ConnectionLost("the connection to cua-driver is closed")
        return self._session

    async def close(self) -> None:
        self._stop.set()
        owner = self._owner
        if owner is None:
            return
        if not self._ready.done():
            owner.cancel()
        await asyncio.wait({owner})

    async def _own(self) -> None:
        params = StdioServerParameters(command=self._bin, args=["mcp"])
        errlog = _open_devnull()
        try:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                _LenientSession(read, write, client_info=_CLIENT_INFO) as session,
            ):
                await session.initialize()
                self._ready.set_result(session)
                await self._stop.wait()
        except* Exception as group:
            failure = _first_leaf(group)
            if not self._ready.done():
                self._ready.set_exception(failure)
            else:
                _log.debug("cua-driver connection ended: %r", failure)
        finally:
            self._ended = True
            errlog.close()
            if not self._ready.done():
                self._ready.cancel()


@dataclass(frozen=True, slots=True, kw_only=True)
class CuaDriverOptions:
    bin: str | None = None
    """The cua-driver binary; default `config.driver_bin()`."""
    session: str | None = None
    """The session label's prefix; default `cua-jev`."""


class CuaDriver:
    """A cua-driver connection that labels sessions, reconnects, and refuses foreground calls."""

    def __init__(self, conn: _Connection, bin_path: str, session: str) -> None:
        self._conn = conn
        self._bin = bin_path
        self._reconnecting: asyncio.Task[None] | None = None
        # Session names are single-use in the daemon once ended, so each connection's is unique.
        self.session = f"{session}-{os.getpid()}-{_base36(time.time_ns() // 1_000_000)}"
        self.sentinel = ActivationSentinel()
        self.session_tools: frozenset[str] = frozenset()
        self.generation = 0
        """Bumped by every reconnect."""

    @classmethod
    async def connect(cls, opts: CuaDriverOptions | None = None) -> CuaDriver:
        """Start `cua-driver mcp` and read which tools take a session label."""
        opts = opts if opts is not None else CuaDriverOptions()
        bin_path = opts.bin if opts.bin is not None else config.driver_bin()
        conn = await _Connection.open(bin_path)
        driver = cls(conn, bin_path, opts.session if opts.session is not None else DEFAULT_SESSION)
        try:
            await driver.load_schemas()
        except BaseException:
            await conn.close()
            raise
        return driver

    async def load_schemas(self) -> None:
        """Read `tools/list`: the tools whose input schema has a `session` property."""
        listed = await self._conn.session().list_tools()
        names: set[str] = set()
        for tool in listed.tools:
            props = as_obj(tool.input_schema.get("properties"))
            if props is not None and "session" in props:
                names.add(tool.name)
        self.session_tools = frozenset(names)

    async def call(self, tool: str, args: Mapping[str, object] | None = None) -> ToolResult:
        """Call a tool."""
        payload: Mapping[str, object] = args if args is not None else {}
        guarded = self.sentinel.watching and tool not in READ_TOOLS and tool != "end_session"
        if not guarded:
            return await self._call_guarded(tool, payload)
        if self.sentinel.violation is not None:
            raise ForegroundViolation(self.sentinel.violation)
        self.sentinel.begin(tool)
        cancelled = False
        try:
            return await self._call_guarded(tool, payload)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self.sentinel.end(tool)
            if not cancelled and (v := await self.sentinel.sample()) is not None:
                raise ForegroundViolation(v)

    async def must(self, tool: str, args: Mapping[str, object] | None = None) -> JsonObject:
        """Like `call`, but a refusal raises DriverError."""
        r = await self.call(tool, args)
        if not r.ok:
            raise DriverError(tool, r.code, r.message)
        return r.data

    async def close(self) -> None:
        """Stop the sentinel, end the session in the daemon, and stop the child."""
        self.sentinel.stop()
        try:
            task = self._reconnecting
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})
            try:
                async with asyncio.timeout(END_SESSION_WAIT_S):
                    await self._call_once("end_session", {"session": self.session}, self._conn)
            except Exception as err:
                _log.debug("end_session failed: %r", err)
        finally:
            await self._conn.close()

    async def _call_guarded(self, tool: str, args: Mapping[str, object]) -> ToolResult:
        conn, generation = self._conn, self.generation
        try:
            return await self._call_once(tool, args, conn)
        except Exception as err:
            if not is_transport_error(err):
                raise
            _log.debug("%s: connection to cua-driver lost (%r); reconnecting", tool, err)
        await self._reconnect(generation)
        # Only reads are replayed: an action may have landed before the transport died.
        if tool in READ_TOOLS:
            return await self._call_once(tool, args, self._conn)
        message = f"{tool}: connection to cua-driver was lost; reconnected but did not retry the action"
        return ToolRefused(code="transport_lost", message=message, data={}, text=message, ms=0)

    async def _reconnect(self, failed_generation: int) -> None:
        if self.generation != failed_generation:
            return
        task = self._reconnecting
        if task is None or task.done():
            task = asyncio.create_task(self._replace_connection())
            task.add_done_callback(_retrieve)
            self._reconnecting = task
        await asyncio.shield(task)

    async def _replace_connection(self) -> None:
        # Close the old connection first so a half-dead `cua-driver mcp` child is not orphaned.
        await self._conn.close()
        self._conn = await _Connection.open(self._bin)
        await self.load_schemas()
        self.generation += 1
        _log.debug("reconnected to cua-driver (generation %d)", self.generation)

    async def _call_once(self, tool: str, args: Mapping[str, object], conn: _Connection) -> ToolResult:
        if steals_focus(tool, args):
            mode = args.get("delivery_mode")
            via = f" (delivery_mode {scalar_text(mode)})" if _truthy(mode) else ""
            message = f"{tool}{via} would bring an app to the front; cua-jev runs background-only"
            return ToolRefused(code="foreground_disallowed", message=message, data={}, text=message, ms=0)
        arguments = {"session": self.session, **args} if tool in self.session_tools else dict(args)
        session = conn.session()
        t0 = time.perf_counter()
        res = await session.call_tool(tool, arguments)
        ms = round_half_up((time.perf_counter() - t0) * 1000)
        text = "\n".join(c.text if isinstance(c, TextContent) else "" for c in res.content)
        data: JsonObject = res.structured_content if res.structured_content is not None else {}
        if res.is_error:
            refusal = get_obj(data, "refusal") or {}
            code = get_str(refusal, "code")
            reason = get_str(refusal, "message")
            _log.debug("%s refused in %d ms (%s)", tool, ms, code)
            return ToolRefused(
                code=code if code is not None else "tool_error",
                message=reason if reason is not None else text,
                data=data,
                text=text,
                ms=ms,
            )
        _log.debug("%s ok in %d ms", tool, ms)
        return ToolOk(data=data, text=text, ms=ms)


def _retrieve(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
