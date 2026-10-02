"""Opt-in app control through cua-driver, without candidate selection or effect checks."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from mcp.types import CallToolResult, ImageContent, TextContent

from beans_picker._json import JsonObject, JsonValue
from beans_picker.driver.mcp import Driver, steals_focus
from beans_picker.driver.types import ToolResult
from beans_picker.errors import DriverError, DriverTimeout, DriverUnavailable, ForegroundViolation, ToolError
from beans_picker.observe.png import MAX_PNG_BYTES, decode_png
from beans_picker.tools.args import DriverArgs
from beans_picker.tools.session import ToolSession

ALLOWED_TOOLS: Final = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "scroll",
        "type_text",
        "press_key",
        "hotkey",
        "set_value",
        "zoom",
        "move_cursor",
        "page",
        "launch_app",
        "get_window_state",
        "list_windows",
        "list_apps",
        "get_screen_size",
        "get_cursor_position",
        "get_accessibility_tree",
    }
)
INPUT_TOOLS: Final = frozenset(
    {"click", "double_click", "right_click", "drag", "scroll", "type_text", "press_key", "hotkey", "set_value", "page"}
)
REFUSED_ACTIONS: Final = {"page": frozenset({"enable_javascript_apple_events"})}

_PATH_KEYS: Final = frozenset({"path", "file", "directory", "dir"})
_OMITTED: Final = "[file omitted]"


async def driver_tool(session: ToolSession, args: DriverArgs) -> CallToolResult:
    """Forward one background call; files are embedded only from an owned capture directory."""
    try:
        return await _driver_tool(session, args)
    except DriverError as err:
        raise ToolError("driver_error", "cua-driver refused the call") from err
    except DriverTimeout as err:
        raise ToolError("driver_timeout", "cua-driver did not answer in time; the action may have occurred") from err
    except DriverUnavailable as err:
        raise ToolError("driver_unavailable", "cua-driver is unavailable") from err
    except ForegroundViolation as err:
        raise ToolError("foreground_violation", "the target app came to the front during the driver call") from err


async def _driver_tool(session: ToolSession, args: DriverArgs) -> CallToolResult:
    tool, arguments = args["tool"], args["arguments"]
    driver = await session.driver()
    schemas = getattr(driver, "tool_schemas", None)
    if not isinstance(schemas, Mapping) or tool not in schemas:
        raise ToolError("unknown_driver_tool", "the tool is not in cua-driver's loaded schemas")
    if tool not in ALLOWED_TOOLS:
        raise ToolError("driver_tool_disallowed", "the driver tool is not in the app-control allowlist")
    if steals_focus(tool, arguments):
        raise ToolError("foreground_disallowed", "the driver call would bring an app to the front")
    if arguments.get("scope") == "desktop":
        raise ToolError("desktop_disallowed", "raw driver calls must target an app, not the desktop")
    action = arguments.get("action")
    if isinstance(action, str) and action in REFUSED_ACTIONS.get(tool, ()):
        raise ToolError("driver_action_disallowed", "the driver action changes configuration and cannot be forwarded")
    pid = arguments.get("pid")
    if tool in INPUT_TOOLS and (isinstance(pid, bool) or not isinstance(pid, int)):
        raise ToolError("driver_pid_required", "input calls require an integer target pid")
    _check_outputs(arguments, schemas[tool])
    watched = tool in INPUT_TOOLS and isinstance(pid, int)
    try:
        if watched and isinstance(pid, int):
            await driver.sentinel.watch(pid)
        paths = (str(session.paths.cache),)
        if "screenshot_out_file" not in arguments:
            return await _forward(driver, tool, arguments, paths=paths)
        session.paths.shots.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="capture-", dir=session.paths.shots) as directory:
            out = Path(directory) / "window.png"
            return await _forward(
                driver, tool, {**arguments, "screenshot_out_file": str(out)}, paths=(directory, *paths), out=out
            )
    finally:
        if watched:
            driver.sentinel.stop()


def _check_outputs(arguments: JsonObject, schema: object) -> None:
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    declared = properties if isinstance(properties, Mapping) else {}
    # Check declared outputs and unrecognized path arguments before forwarding either.
    for key in dict.fromkeys([*declared, *arguments]):
        if key not in arguments or key == "screenshot_out_file":
            continue
        if _path_key(key) or key.lower().endswith(("_out", "_out_file")):
            raise ToolError("driver_file_output_disallowed", "only screenshot_out_file may name a file output")


async def _forward(
    driver: Driver, tool: str, arguments: JsonObject, *, paths: tuple[str, ...], out: Path | None = None
) -> CallToolResult:
    result = await driver.call(tool, arguments)
    if not result.ok or result.data.get("effect") == "failed":
        raise ToolError("driver_error", "cua-driver refused the call")
    images = [ImageContent(type="image", data=image.data, mime_type=image.mime_type) for image in result.images]
    if out is not None:
        images.append(await asyncio.to_thread(_capture_image, result, out))
    return CallToolResult(
        content=[TextContent(type="text", text=_safe_text(result.text, paths)), *images],
        structured_content=_safe_data(result.data, paths),
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


def _path_key(key: str) -> bool:
    return key.lower() in _PATH_KEYS or key.lower().endswith(("_path", "_file", "_dir", "_directory"))


def _safe_text(value: str, paths: tuple[str, ...]) -> str:
    for path in paths:
        value = value.replace(path, _OMITTED)
    return value


def _safe_data(data: JsonObject, paths: tuple[str, ...]) -> JsonObject:
    return {
        _safe_text(key, paths): _OMITTED if _path_key(key) else _safe_value(value, paths) for key, value in data.items()
    }


def _safe_value(value: JsonValue, paths: tuple[str, ...]) -> JsonValue:
    if isinstance(value, str):
        return _safe_text(value, paths)
    if isinstance(value, dict):
        return _safe_data(value, paths)
    if isinstance(value, list):
        return [_safe_value(item, paths) for item in value]
    return value
