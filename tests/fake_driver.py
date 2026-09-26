"""A stand-in for `cua-driver mcp`: a stdio MCP server with a few of its tools.

Run as `python fake_driver.py --state DIR mcp`. Every start appends a line to `DIR/launches`, so a
process knows its launch number (1 for the first start); every tool call is appended to
`DIR/calls.jsonl` as `{"launch", "tool", "args"}`. Tools:

- `list_windows`, `get_window_state` (reads) and `click`, `end_session` (actions) take `session`;
  `echo` takes it only from the second launch on, so a reconnect has new schemas to load.
- `get_window_state` declares an output schema with a `uint64` format and answers with data that
  does not match it.
- `list_windows` and `click` exit the process mid-call when `die_in_launch` equals the launch
  number, after `die_after` seconds.
- `refuse` answers with an error and a refusal; `fail_plain` with an error and text only;
  `mixed` with text, an image and more text; `whoami` with the process id.
- `click` waits `hold` seconds before answering.
- While the file `DIR/fail_list` exists, `tools/list` fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

_SESSION = {"session": {"type": "string"}}


def _tool(name: str, *, session: bool, output_schema: dict[str, Any] | None = None) -> Tool:
    props: dict[str, Any] = {"pid": {"type": "integer"}, **(_SESSION if session else {})}
    return Tool(name=name, input_schema={"type": "object", "properties": props}, output_schema=output_schema)


def _tools(launch: int) -> list[Tool]:
    return [
        _tool("list_windows", session=True),
        _tool(
            "get_window_state",
            session=True,
            output_schema={
                "type": "object",
                "properties": {"snapshot_id": {"type": "integer", "format": "uint64"}},
                "required": ["snapshot_id"],
            },
        ),
        _tool("click", session=True),
        _tool("end_session", session=True),
        _tool("echo", session=launch >= 2),
        _tool("refuse", session=False),
        _tool("fail_plain", session=False),
        _tool("mixed", session=False),
        _tool("whoami", session=False),
    ]


def _text(s: str) -> list[TextContent | ImageContent]:
    return [TextContent(type="text", text=s)]


def _record(state: Path, launch: int, tool: str, args: dict[str, Any]) -> None:
    with (state / "calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"launch": launch, "tool": tool, "args": args}) + "\n")


def _register_launch(state: Path) -> int:
    path = state / "launches"
    with path.open("a", encoding="utf-8") as f:
        f.write("x\n")
    return len(path.read_text(encoding="utf-8").splitlines())


def build(state: Path, launch: int) -> Server[Any]:
    async def on_list_tools(ctx: object, params: PaginatedRequestParams | None) -> ListToolsResult:
        if (state / "fail_list").exists():
            raise RuntimeError("tools are not available")
        return ListToolsResult(tools=_tools(launch))

    async def on_call_tool(ctx: object, params: CallToolRequestParams) -> CallToolResult:
        name = params.name
        args: dict[str, Any] = dict(params.arguments or {})
        _record(state, launch, name, args)
        if name in ("list_windows", "click") and args.get("die_in_launch") == launch:
            await asyncio.sleep(float(args.get("die_after", 0)))
            os._exit(3)
        if name == "end_session" and (state / "hold_end_session").exists():
            await asyncio.Event().wait()  # a daemon that stopped answering
        if name == "click" and "hold" in args:
            await asyncio.sleep(float(args["hold"]))
        if name == "list_windows":
            windows = [{"window_id": 10, "pid": 42, "title": "Main", "layer": 0, "z_index": 1}]
            data = {"windows": windows, "launch": launch}
            return CallToolResult(content=_text("1 window"), structured_content=data)
        if name == "get_window_state":
            return CallToolResult(content=_text("state"), structured_content={"snapshot_id": "s1", "launch": launch})
        if name in ("click", "end_session", "echo"):
            return CallToolResult(content=_text(f"{name} done"), structured_content={"args": args, "launch": launch})
        if name == "refuse":
            refusal = {"refusal": {"code": "stale_element_token", "message": "token gone"}, "extra": 1}
            return CallToolResult(content=_text("refused text"), structured_content=refusal, is_error=True)
        if name == "fail_plain":
            return CallToolResult(content=_text("plain failure"), is_error=True)
        if name == "mixed":
            image = ImageContent(type="image", data="AAAA", mime_type="image/png")
            content: list[TextContent | ImageContent] = [*_text("a"), image, *_text("b")]
            return CallToolResult(content=content)
        if name == "whoami":
            return CallToolResult(content=_text(str(os.getpid())), structured_content={"pid": os.getpid()})
        return CallToolResult(content=_text(f"unknown tool {name}"), is_error=True)

    return Server("fake-cua-driver", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve(state: Path) -> None:
    launch = _register_launch(state)
    server = build(state, launch)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("command", choices=["mcp"])
    asyncio.run(serve(parser.parse_args().state))


if __name__ == "__main__":
    main()
