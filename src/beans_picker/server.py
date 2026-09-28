"""The MCP server: observe, act and extract over one background cua-driver session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Any, Final

import mcp.types as types
from mcp import MCPError
from mcp.server import NotificationOptions, Server, ServerRequestContext
from mcp.server.models import InitializationOptions

from beans_picker import __version__, _json
from beans_picker._json import JsonObject
from beans_picker.driver import lock
from beans_picker.errors import BeansPickerError, ToolError, failure
from beans_picker.tools.act import act_tool
from beans_picker.tools.args import (
    INPUT_SCHEMAS,
    act_args,
    check_arguments,
    extract_args,
    observe_args,
    validation_text,
)
from beans_picker.tools.extract import extract_tool
from beans_picker.tools.observe import observe_tool
from beans_picker.tools.session import Session

_log = logging.getLogger(__name__)

SERVER_NAME: Final = "beans-picker"

INSTRUCTIONS: Final = "\n".join(
    [
        "macOS app control in the background (the app is never brought to the front).",
        "- observe: lists a window's candidate actions with stable ids; with an instruction, Jev ranks them.",
        "- act: performs one action, picked by Jev from the instruction or given as candidateId, then checks the "
        "effect on fresh snapshots. Steps you already know (fill these fields, tick these boxes, then press Save) go "
        "in one call with `then`: they run in order and stop at the first that is neither done nor unverified. "
        "Text is entered exactly as given in `text` and verified by exact equality.",
        "  status: done | unverified (the field changed but its exact text could not be read) | no_effect | mismatch "
        "(something changed, not what was asked) | ambiguous (choose a candidateId) | needs_confirmation "
        "(irreversible: repeat with allowDestructive) | not_found | failed.",
        "  When a step brings up something new (a menu, a dialog, another page), the result carries its screenText "
        "and newCandidates with their ids: act on those directly instead of observing again.",
        "- extract: returns the text or value of the element Jev picks, exactly as read; a table or list comes back "
        "row by row.",
        "Rows scrolled out of view are not on the window until a scroll candidate brings them in.",
        'Commands that live only in a right-click menu (rename, star, move to trash) are reached with an "open the '
        'context menu" candidate; its items are then pressed like any other.',
        "Keys (Return, Escape, Tab, Space, the arrows) go to the focused control: a keyboard drag is focus the "
        "handle, Space, arrows, Space.",
        "Speed: observe without an instruction and act with a candidateId make no Jev call. Name the step with an "
        "instruction when the id is not in hand or several controls look alike.",
        "Plan the steps yourself and read each step's status.",
    ]
)

DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "observe": "List the candidate actions on a window (id, kind, what it does). With `instruction`, Jev ranks "
        "them and the most likely come first with their probability `p`.",
        "act": "Perform one action on a window and verify its effect, then any steps in `then` the same way. Jev "
        "picks the action for `instruction` unless `candidateId` (from observe or an earlier act) is given.",
        "extract": "Return the text or value of the element Jev picks for `instruction` (e.g. 'the result shown on "
        "the display'), exactly as read from accessibility. A table or list is returned as `rows`.",
    }
)

type ScreenLocked = Callable[[], Awaitable[bool]]


def error(code: str, message: str) -> types.CallToolResult:
    """A tool-level failure: `isError` with `{"status":"failed","code","message"}` as its text."""
    text = _json.dumps({"status": "failed", "code": code, "message": message})
    return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=text)])


def _protocol_error(text: str) -> types.CallToolResult:
    return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=text)])


async def run(
    session: Session,
    fn: Callable[[], Awaitable[object]],
    *,
    acts: bool,
    screen_locked: ScreenLocked = lock.screen_locked,
) -> types.CallToolResult:
    """One tool call, after every earlier one, and never while the screen is locked."""

    async def body() -> types.CallToolResult:
        try:
            if await screen_locked():
                verb = "done" if acts else "read"
                return error("screen_locked", f"the screen is locked; nothing was {verb}. Unlock it and call again")
            return types.CallToolResult(content=[types.TextContent(type="text", text=_json.dumps(await fn()))])
        except Exception as err:
            code, message = failure(err)
            if not isinstance(err, ToolError):
                _log.warning("tool call failed (%s, %s)", code, type(err).__name__)
            return error(code, message)

    try:
        return await session.exclusive(body)
    except BeansPickerError:
        return error("internal", "server is shutting down")


class _Server(Server[Session]):
    """Announces a changeable tool list and omits an empty `experimental` capability, as MCP clients expect."""

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        options = notification_options if notification_options is not None else NotificationOptions(tools_changed=True)
        init = super().create_initialization_options(options, experimental_capabilities, extensions)
        if not init.capabilities.experimental:
            init.capabilities = init.capabilities.model_copy(update={"experimental": None})
        return init


def _tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=DESCRIPTIONS[name],
            input_schema=dict(schema),
            execution=types.ToolExecution(task_support="forbidden"),
        )
        for name, schema in INPUT_SCHEMAS.items()
    ]


def create_server(
    session: Session | None = None, *, screen_locked: ScreenLocked = lock.screen_locked
) -> Server[Session]:
    """The configured server (not connected). A given session stays the caller's to close; without one,
    the server makes a `Session()` and closes it when a run ends."""
    owned = session is None
    the_session = session if session is not None else Session()

    @asynccontextmanager
    async def lifespan(_: Server[Session]) -> AsyncIterator[Session]:
        try:
            yield the_session
        finally:
            if owned:
                await asyncio.shield(the_session.close())

    async def on_list_tools(
        ctx: ServerRequestContext[Session], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tools())

    async def on_call_tool(
        ctx: ServerRequestContext[Session], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        name = params.name
        if name not in INPUT_SCHEMAS:
            raise MCPError(types.INVALID_PARAMS, f"Tool {name} not found")
        checked, issues = check_arguments(name, params.arguments)
        if issues:
            return _protocol_error(validation_text(name, issues))
        return await run(
            the_session, _handler(the_session, name, checked), acts=name == "act", screen_locked=screen_locked
        )

    return _Server(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _handler(session: Session, name: str, arguments: JsonObject) -> Callable[[], Awaitable[object]]:
    match name:
        case "observe":
            return lambda: observe_tool(session, observe_args(arguments))
        case "act":
            return lambda: act_tool(session, act_args(arguments))
        case _:
            return lambda: extract_tool(session, extract_args(arguments))
