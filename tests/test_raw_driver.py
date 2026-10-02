from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
import zlib
from pathlib import Path

import pytest
from mcp import Client, MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, ImageContent, TextContent

from beans_picker import config
from beans_picker._json import JsonObject, JsonValue
from beans_picker.driver.mcp import CuaDriver, CuaDriverOptions, Driver
from beans_picker.driver.sentinel import ActivationSentinel
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult
from beans_picker.errors import DriverError, DriverTimeout, DriverUnavailable
from beans_picker.paths import Paths
from beans_picker.server import create_server
from beans_picker.tools.driver import driver_tool
from beans_picker.tools.session import JevLike, Session
from tests.fakes import FakeDriver, OnCall
from tests.helpers import blank, encode_png
from tests.test_driver_mcp import FakeBin


class RawDriver(FakeDriver):
    def __init__(self, tools: tuple[str, ...], on_call: OnCall | None = None) -> None:
        super().__init__(on_call)
        self.tool_schemas: dict[str, JsonObject] = {name: {"type": "object"} for name in tools}


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEANS_PICKER_RAW_DRIVER", "1")


async def _unlocked() -> bool:
    return False


async def _locked() -> bool:
    return True


def _no_jev() -> JevLike:
    raise AssertionError("raw driver calls must not use Jev")


def _session(driver: Driver, cache: Path) -> Session:
    async def connect() -> Driver:
        return driver

    return Session(connect, paths=Paths(cache=cache), jev_factory=_no_jev)


async def _call(driver: Driver, cache: Path, args: JsonObject, *, locked: bool = False) -> CallToolResult:
    session = _session(driver, cache)
    try:
        server = create_server(session, screen_locked=_locked if locked else _unlocked)
        async with Client(server, mode="legacy") as client:
            return await client.call_tool("driver", args)
    finally:
        await session.close()


def _failure(result: CallToolResult) -> JsonObject:
    first = result.content[0]
    assert isinstance(first, TextContent)
    parsed: JsonObject = json.loads(first.text)
    assert result.is_error
    return parsed


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "anything"])
async def test_disabled_driver_is_neither_listed_nor_callable(
    value: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if value is None:
        monkeypatch.delenv("BEANS_PICKER_RAW_DRIVER", raising=False)
    else:
        monkeypatch.setenv("BEANS_PICKER_RAW_DRIVER", value)
    driver = RawDriver(("double_click",))
    session = _session(driver, tmp_path / "raw-cache")
    try:
        async with Client(create_server(session, screen_locked=_unlocked), mode="legacy") as client:
            assert "driver" not in [t.name for t in (await client.list_tools()).tools]
            with pytest.raises(MCPError) as info:
                await client.call_tool("driver", {"tool": "double_click", "arguments": {}})
        assert info.value.code == INVALID_PARAMS
        assert info.value.message == "Tool driver not found"
        assert driver.calls == []
    finally:
        await session.close()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_raw_driver_accepts_truthy_settings(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEANS_PICKER_RAW_DRIVER", value)
    assert config.raw_driver()


@pytest.mark.usefixtures("enabled")
async def test_enabled_driver_forwards_one_call_and_keeps_text_data_and_images(tmp_path: Path) -> None:
    image = ImageContent(type="image", data=base64.b64encode(encode_png(blank(2, 2))).decode(), mime_type="image/png")
    answer = ToolOk(
        data={"effect": "confirmed", "nested": [1, {"label": "Delete"}]}, text="clicked twice", ms=1, images=(image,)
    )
    driver = RawDriver(("double_click",), lambda _tool, _args: answer)
    session = _session(driver, tmp_path / "raw-cache")
    try:
        async with Client(create_server(session, screen_locked=_unlocked), mode="legacy") as client:
            tools = (await client.list_tools()).tools
            assert "driver" in [t.name for t in tools]
            args: JsonObject = {"pid": 3, "window_id": 4, "custom": {"label": "Delete", "values": [1, True, None]}}
            result = await client.call_tool("driver", {"tool": "double_click", "arguments": args})
        assert not result.is_error
        assert result.structured_content == answer.data
        assert result.content == [TextContent(type="text", text=answer.text), image]
        assert driver.calls == [("double_click", args)]
    finally:
        await session.close()


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("move_cursor", {}),
        ("double_click", {"pid": 3, "delivery_mode": "foreground"}),
    ],
)
async def test_focus_stealing_calls_are_refused_before_driver_call(
    tool: str, arguments: JsonObject, tmp_path: Path
) -> None:
    driver = RawDriver((tool,))
    result = await _call(driver, tmp_path / "raw-cache", {"tool": tool, "arguments": arguments})
    assert _failure(result)["code"] == "foreground_disallowed"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "tool",
    [
        "bring_to_front",
        "invoke_menu",
        "get_desktop_state",
        "get_config",
        "get_agent_cursor_state",
        "get_recording_state",
        "health_report",
        "check_permissions",
        "future_driver_tool",
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
    ],
)
async def test_non_control_tools_are_refused_even_when_driver_lists_them(tool: str, tmp_path: Path) -> None:
    driver = RawDriver((tool,))
    result = await _call(driver, tmp_path / "raw-cache", {"tool": tool, "arguments": {}})
    assert _failure(result)["code"] == "driver_tool_disallowed"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("schemas_available", [True, False])
@pytest.mark.parametrize("tool", ["unknown", "click", "bring_to_front", "kill_app"])
async def test_unknown_driver_tools_are_refused(tmp_path: Path, schemas_available: bool, tool: str) -> None:
    driver = RawDriver(("double_click",)) if schemas_available else FakeDriver()
    result = await _call(driver, tmp_path / "raw-cache", {"tool": tool, "arguments": {}})
    assert _failure(result)["code"] == "unknown_driver_tool"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
async def test_screen_lock_prevents_raw_actions(tmp_path: Path) -> None:
    driver = RawDriver(("double_click",))
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "double_click", "arguments": {}}, locked=True)
    assert _failure(result) == {
        "status": "failed",
        "code": "screen_locked",
        "message": "the screen is locked; nothing was done. Unlock it and call again",
    }
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
async def test_raw_driver_refusal_omits_private_text_data_and_images(tmp_path: Path) -> None:
    image = ImageContent(type="image", data="private-image", mime_type="image/png")
    driver = RawDriver(
        ("zoom",),
        lambda _tool, _args: ToolRefused(
            code="stale_element_token",
            message="SENTINEL-private-message",
            data={"refusal": {"message": "SENTINEL-private-data"}},
            text="SENTINEL-private-text",
            ms=1,
            images=(image,),
        ),
    )
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "zoom", "arguments": {}})
    assert _failure(result) == {"status": "failed", "code": "driver_error", "message": "cua-driver refused the call"}
    assert len(result.content) == 1
    assert result.structured_content is None
    assert "SENTINEL" not in result.model_dump_json()
    assert "private-image" not in result.model_dump_json()


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("reported_path", [True, False])
async def test_owned_screenshot_is_returned_as_image_and_cleaned_up(tmp_path: Path, reported_path: bool) -> None:
    png = encode_png(blank(2, 2))
    written: list[Path] = []
    requested = tmp_path / "raw-caller.png"

    def capture(_tool: str, args: dict[str, object]) -> ToolResult:
        raw = args["screenshot_out_file"]
        assert isinstance(raw, str)
        path = Path(raw)
        assert path != requested
        assert path.parent.name.startswith("capture-")
        assert path.parent.parent == tmp_path / "raw-cache" / "shots"
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        path.write_bytes(png)
        written.append(path)
        return ToolOk(data={"screenshot_file_path": raw} if reported_path else {}, text=f"screenshot: {raw}", ms=1)

    driver = RawDriver(("get_window_state",), capture)
    result = await _call(
        driver,
        tmp_path / "raw-cache",
        {
            "tool": "get_window_state",
            "arguments": {"pid": 3, "window_id": 4, "screenshot_out_file": str(requested)},
        },
    )
    assert not result.is_error
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert len(images) == 1
    assert base64.b64decode(images[0].data) == png
    assert str(written[0]) not in result.model_dump_json()
    assert not written[0].parent.exists()
    assert not requested.exists()


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "symlink",
        "hardlink",
        "not_png",
        "directory",
        "fifo",
        "missing",
        "malformed_png",
        "truncated_png",
        "oversized_dimensions",
    ],
)
async def test_unsafe_screenshot_paths_never_become_images_or_leak_paths(tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "raw-private.png"
    png = encode_png(blank(2, 2))
    outside.write_bytes(png)
    written: list[Path] = []

    def capture(_tool: str, args: dict[str, object]) -> ToolResult:
        raw = args["screenshot_out_file"]
        assert isinstance(raw, str)
        path = Path(raw)
        written.append(path)
        reported = outside if kind == "outside" else path
        if kind == "symlink":
            path.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, path)
        elif kind == "not_png":
            path.write_text("private content", encoding="utf-8")
        elif kind == "directory":
            path.mkdir()
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "malformed_png":
            path.write_bytes(png[:8] + b"not image chunks")
        elif kind == "truncated_png":
            path.write_bytes(png[:-4])
        elif kind == "oversized_dimensions":
            header = (2**30).to_bytes(4) + png[20:29]
            checksum = zlib.crc32(b"IHDR" + header).to_bytes(4)
            path.write_bytes(png[:16] + header + checksum + png[33:])
        return ToolOk(data={"screenshot_file_path": str(reported)}, text=f"saved at {reported}", ms=1)

    driver = RawDriver(("get_window_state",), capture)
    result = await _call(
        driver,
        tmp_path / "raw-cache",
        {
            "tool": "get_window_state",
            "arguments": {"screenshot_out_file": str(tmp_path / "raw-request.png")},
        },
    )
    assert _failure(result)["code"] == "unsafe_driver_output"
    assert not any(isinstance(c, ImageContent) for c in result.content)
    assert str(outside) not in result.model_dump_json()
    assert str(written[0]) not in result.model_dump_json()
    assert not written[0].parent.exists()
    assert outside.read_bytes() == png


@pytest.mark.usefixtures("enabled")
async def test_path_keys_are_redacted_but_unowned_text_and_urls_are_unchanged(tmp_path: Path) -> None:
    outside = tmp_path / "raw-private.png"
    outside.write_bytes(encode_png(blank(2, 2)))
    url = "https://example.com/#/settings"
    driver = RawDriver(
        ("list_apps",),
        lambda _tool, _args: ToolOk(
            data={
                "apps": [{"name": "Editor", "path": str(outside), "icon_file": "relative-icon.png", "url": url}],
                "screenshot_file_path": str(outside),
                "details": f"local file: {outside}",
            },
            text=f"Editor\n{outside}\n{url}",
            ms=1,
        ),
    )
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "list_apps", "arguments": {}})
    assert not result.is_error
    assert result.structured_content == {
        "apps": [{"name": "Editor", "path": "[file omitted]", "icon_file": "[file omitted]", "url": url}],
        "screenshot_file_path": "[file omitted]",
        "details": f"local file: {outside}",
    }
    assert result.content == [TextContent(type="text", text=f"Editor\n{outside}\n{url}")]
    assert not (tmp_path / "raw-cache").exists()


@pytest.mark.usefixtures("enabled")
async def test_driver_exceptions_are_sanitized_in_results_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(_tool: str, _args: dict[str, object]) -> ToolResult:
        raise RuntimeError("SENTINEL-private-screen-text")

    driver = RawDriver(("zoom",), fail)
    with caplog.at_level(logging.WARNING):
        result = await _call(driver, tmp_path / "raw-cache", {"tool": "zoom", "arguments": {}})
    assert _failure(result)["code"] == "internal"
    assert "SENTINEL" not in result.model_dump_json() + caplog.text
    assert all(record.exc_info is None for record in caplog.records)


_INPUT_TOOLS = (
    "click",
    "double_click",
    "right_click",
    "drag",
    "scroll",
    "type_text",
    "press_key",
    "hotkey",
    "set_value",
    "page",
)


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("tool", _INPUT_TOOLS)
@pytest.mark.parametrize("pid", [None, True, False, "3", 3.0, [], {}])
async def test_input_calls_require_an_integer_pid(tool: str, pid: JsonValue, tmp_path: Path) -> None:
    driver = RawDriver((tool,))
    arguments: JsonObject = {} if pid is None else {"pid": pid}
    result = await _call(driver, tmp_path / "raw-cache", {"tool": tool, "arguments": arguments})
    assert _failure(result)["code"] == "driver_pid_required"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("tool", _INPUT_TOOLS)
async def test_input_calls_watch_the_target_pid_and_stop_after_return(tool: str, tmp_path: Path) -> None:
    async def front() -> int:
        return 1

    def answer(_tool: str, _args: dict[str, object]) -> ToolResult:
        assert driver.sentinel.watching
        return ToolOk(data={}, text="done", ms=1)

    driver = RawDriver((tool,), answer)
    driver.sentinel = ActivationSentinel(front)
    session = _session(driver, tmp_path / "raw-cache")
    try:
        result = await driver_tool(session, {"tool": tool, "arguments": {"pid": 3}})
        assert not result.is_error
        assert not driver.sentinel.watching
        assert driver.calls == [(tool, {"pid": 3})]
    finally:
        await session.close()


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "tool",
    [
        "zoom",
        "launch_app",
        "get_window_state",
        "list_windows",
        "list_apps",
        "get_screen_size",
        "get_cursor_position",
        "get_accessibility_tree",
    ],
)
async def test_non_input_app_tools_are_allowed_without_pid(tool: str, tmp_path: Path) -> None:
    driver = RawDriver((tool,))
    result = await _call(driver, tmp_path / "raw-cache", {"tool": tool, "arguments": {}})
    assert not result.is_error
    assert driver.calls == [(tool, {})]


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("tool", ["click", "page", "get_window_state"])
async def test_desktop_scope_is_refused_even_with_pid(tool: str, tmp_path: Path) -> None:
    driver = RawDriver((tool,))
    result = await _call(driver, tmp_path / "raw-cache", {"tool": tool, "arguments": {"pid": 3, "scope": "desktop"}})
    assert _failure(result)["code"] == "desktop_disallowed"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
async def test_page_cannot_enable_browser_javascript_preferences(tmp_path: Path) -> None:
    driver = RawDriver(("page",))
    result = await _call(
        driver,
        tmp_path / "raw-cache",
        {
            "tool": "page",
            "arguments": {"pid": 3, "action": "enable_javascript_apple_events", "user_has_confirmed_enabling": True},
        },
    )
    assert _failure(result)["code"] == "driver_action_disallowed"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "action", ["execute_javascript", "get_text", "query_dom", "click_element", "insert_text", "type_keystrokes"]
)
async def test_page_allows_other_actions_with_pid(action: str, tmp_path: Path) -> None:
    driver = RawDriver(("page",))
    args: JsonObject = {"pid": 3, "action": action}
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "page", "arguments": args})
    assert not result.is_error
    assert driver.calls == [("page", args)]


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "key", ["debug_image_out", "trace_out_file", "output_path", "output_file", "output_dir", "output_directory"]
)
@pytest.mark.parametrize("in_schema", [True, False])
async def test_click_refuses_other_file_outputs_before_call(key: str, in_schema: bool, tmp_path: Path) -> None:
    driver = RawDriver(("click",))
    if in_schema:
        driver.tool_schemas["click"] = {"type": "object", "properties": {key: {"type": "string"}}}
    destination = tmp_path / "raw-private-output"
    result = await _call(
        driver, tmp_path / "raw-cache", {"tool": "click", "arguments": {"pid": 3, key: str(destination)}}
    )
    assert _failure(result)["code"] == "driver_file_output_disallowed"
    assert driver.calls == []
    assert not destination.exists()


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "error", [DriverError("click", "tool_error", "SENTINEL"), DriverTimeout("SENTINEL"), DriverUnavailable("SENTINEL")]
)
async def test_driver_failure_exceptions_omit_private_details(error: Exception, tmp_path: Path) -> None:
    def fail(_tool: str, _args: dict[str, object]) -> ToolResult:
        raise error

    driver = RawDriver(("click",), fail)
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "click", "arguments": {"pid": 3}})
    assert result.is_error
    assert len(result.content) == 1
    assert result.structured_content is None
    assert "SENTINEL" not in result.model_dump_json()


@pytest.mark.usefixtures("enabled")
async def test_text_preserves_slashes_and_urls_and_redacts_owned_paths_anywhere(tmp_path: Path) -> None:
    cache = tmp_path / "raw-cache"
    url = "https://example.com/#/settings"
    plain = f"Save / Cancel\n{url}\nsaved:/Users/example/unowned.png"
    driver = RawDriver(
        ("list_apps",),
        lambda _tool, _args: ToolOk(
            data={"label": plain, "nested": [{"label": f"cached:{cache}/shots/capture-owned/window.png"}]},
            text=f"{plain}\ncached:{cache}/shots/capture-owned/window.png",
            ms=1,
        ),
    )
    result = await _call(driver, cache, {"tool": "list_apps", "arguments": {}})
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["label"] == plain
    text = result.content[0]
    assert isinstance(text, TextContent)
    assert text.text.startswith(plain + "\ncached:")
    assert str(cache) not in result.model_dump_json()
    assert "[file omitted]" in result.model_dump_json()


@pytest.mark.usefixtures("enabled")
async def test_capture_directory_is_redacted_even_after_a_colon(tmp_path: Path) -> None:
    captured: list[str] = []

    def capture(_tool: str, args: dict[str, object]) -> ToolResult:
        raw = args["screenshot_out_file"]
        assert isinstance(raw, str)
        path = Path(raw)
        path.write_bytes(encode_png(blank(2, 2)))
        captured.append(str(path.parent))
        return ToolOk(data={"details": f"saved:{path.parent}"}, text=f"saved:{path.parent}", ms=1)

    driver = RawDriver(("get_window_state",), capture)
    result = await _call(
        driver,
        tmp_path / "raw-cache",
        {"tool": "get_window_state", "arguments": {"screenshot_out_file": "ignored.png"}},
    )
    assert not result.is_error
    assert captured[0] not in result.model_dump_json()
    capture_directory = Path(captured[0])
    assert not await asyncio.to_thread(capture_directory.exists)


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("cancel", [False, True])
async def test_input_watch_stops_after_failure_or_cancellation(cancel: bool, tmp_path: Path) -> None:
    entered = asyncio.Event()
    watched: list[bool] = []

    async def fail(_tool: str, _args: dict[str, object]) -> ToolResult:
        watched.append(driver.sentinel.watching)
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("failure")

    driver = RawDriver(("click",), fail)
    session = _session(driver, tmp_path / "raw-cache")
    try:
        task = asyncio.create_task(driver_tool(session, {"tool": "click", "arguments": {"pid": 3}}))
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else Exception):
            await task
        assert watched == [True]
        assert not driver.sentinel.watching
    finally:
        await session.close()


@pytest.mark.usefixtures("enabled")
async def test_raw_input_reports_activation_detected_by_real_driver_guard(tmp_path: Path) -> None:
    fake_bin = FakeBin(tmp_path)
    driver = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    samples = 0

    async def front() -> int:
        nonlocal samples
        samples += 1
        return 1 if samples == 1 else 3

    driver.sentinel = ActivationSentinel(front)
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "click", "arguments": {"pid": 3}})
    assert _failure(result)["code"] == "foreground_violation"
    assert not driver.sentinel.watching


@pytest.mark.usefixtures("enabled")
async def test_driver_failed_effect_omits_private_text_data_and_images(tmp_path: Path) -> None:
    driver = RawDriver(
        ("click",),
        lambda _tool, _args: ToolOk(
            data={"effect": "failed", "detail": "SENTINEL-private-data"},
            text="SENTINEL-private-text",
            ms=1,
            images=(ImageContent(type="image", data="private-image", mime_type="image/png"),),
        ),
    )
    result = await _call(driver, tmp_path / "raw-cache", {"tool": "click", "arguments": {"pid": 3}})
    assert _failure(result)["code"] == "driver_error"
    assert len(result.content) == 1
    assert result.structured_content is None
    assert "SENTINEL" not in result.model_dump_json()
    assert "private-image" not in result.model_dump_json()


@pytest.mark.usefixtures("enabled")
async def test_refused_capture_does_not_read_file_or_forward_images(tmp_path: Path) -> None:
    directories: list[Path] = []

    def refuse(_tool: str, args: dict[str, object]) -> ToolResult:
        raw = args["screenshot_out_file"]
        assert isinstance(raw, str)
        directories.append(Path(raw).parent)
        return ToolRefused(
            code="tool_error",
            message="private failure",
            data={"screenshot_file_path": raw},
            text=f"failed:{raw}",
            ms=1,
            images=(ImageContent(type="image", data="private-image", mime_type="image/png"),),
        )

    driver = RawDriver(("get_window_state",), refuse)
    result = await _call(
        driver,
        tmp_path / "raw-cache",
        {"tool": "get_window_state", "arguments": {"screenshot_out_file": "ignored.png"}},
    )
    assert _failure(result)["code"] == "driver_error"
    assert len(result.content) == 1
    assert result.structured_content is None
    assert str(directories[0]) not in result.model_dump_json()
    assert not directories[0].exists()
