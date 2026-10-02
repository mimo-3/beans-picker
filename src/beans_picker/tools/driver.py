"""Opt-in app control through cua-driver, without candidate selection or effect checks."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from mcp.types import CallToolResult, ImageContent, TextContent

from beans_picker._json import JsonObject, JsonValue
from beans_picker.driver.mcp import Driver, steals_focus
from beans_picker.driver.types import ToolResult
from beans_picker.errors import ToolError
from beans_picker.observe.png import MAX_PNG_BYTES, decode_png
from beans_picker.tools.args import DriverArgs
from beans_picker.tools.session import ToolSession

DENIED_TOOLS: Final = frozenset(
    {
        "kill_app",
        "set_config",
        "replay_trajectory",
        "start_recording",
        "stop_recording",
        "install_ffmpeg",
        "check_for_update",
        "start_session",
        "end_session",
        "set_agent_cursor_enabled",
        "set_agent_cursor_motion",
        "set_agent_cursor_style",
    }
)

_FILE_REFERENCE: Final = re.compile(r'(?<![A-Za-z0-9:/])(?:file://|~/|/)[^\n\r\u2028\u2029"<>`]*')
_PATH_KEYS: Final = frozenset({"path", "file", "directory", "dir"})
_OMITTED: Final = "[file omitted]"


async def driver_tool(session: ToolSession, args: DriverArgs) -> CallToolResult:
    """Forward one background call; files are embedded only from an owned capture directory."""
    tool, arguments = args["tool"], args["arguments"]
    if steals_focus(tool, arguments):
        raise ToolError("foreground_disallowed", "the driver call would bring an app to the front")
    if tool in DENIED_TOOLS:
        raise ToolError("driver_tool_disallowed", "the driver tool is not app control and cannot be forwarded")
    driver = await session.driver()
    schemas = getattr(driver, "tool_schemas", None)
    if not isinstance(schemas, Mapping) or tool not in schemas:
        raise ToolError("unknown_driver_tool", "the tool is not in cua-driver's loaded schemas")
    if "screenshot_out_file" not in arguments:
        return await _forward(driver, tool, arguments)
    session.paths.shots.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="dragpt-capture-", dir=session.paths.shots) as directory:
        out = Path(directory) / "window.png"
        return await _forward(driver, tool, {**arguments, "screenshot_out_file": str(out)}, out=out)


async def _forward(driver: Driver, tool: str, arguments: JsonObject, *, out: Path | None = None) -> CallToolResult:
    result = await driver.call(tool, arguments)
    images = [ImageContent(type="image", data=image.data, mime_type=image.mime_type) for image in result.images]
    if result.ok and out is not None:
        images.append(await asyncio.to_thread(_capture_image, result, out))
    return CallToolResult(
        is_error=not result.ok,
        content=[TextContent(type="text", text=_safe_text(result.text)), *images],
        structured_content=_safe_data(result.data),
    )


def _capture_image(result: ToolResult, out: Path) -> ImageContent:
    given = result.data.get("screenshot_file_path", str(out))
    if not isinstance(given, str) or Path(given).parent != out.parent:
        raise ToolError("unsafe_driver_output", "cua-driver returned a file outside the capture directory")
    try:
        directory = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(Path(given).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("not a private regular file")
                data = source.read(MAX_PNG_BYTES + 1)
        finally:
            os.close(directory)
        if decode_png(data) is None:
            raise ValueError("not a bounded PNG")
    except (OSError, ValueError) as err:
        raise ToolError("unsafe_driver_output", "cua-driver returned an unreadable or unsafe capture") from err
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png")


def _safe_text(value: str) -> str:
    return _FILE_REFERENCE.sub(_OMITTED, value)


def _safe_data(data: JsonObject) -> JsonObject:
    return {
        _safe_text(key): _OMITTED
        if key.lower() in _PATH_KEYS or key.lower().endswith(("_path", "_file", "_dir", "_directory"))
        else _safe_value(value)
        for key, value in data.items()
    }


def _safe_value(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return _safe_data(value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    return value
