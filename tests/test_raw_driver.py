from __future__ import annotations

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
from beans_picker._json import JsonObject
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult
from beans_picker.paths import Paths
from beans_picker.server import create_server
from beans_picker.tools.session import JevLike, Session
from tests.fakes import FakeDriver, OnCall
from tests.helpers import blank, encode_png


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


def _session(driver: FakeDriver, cache: Path) -> Session:
    async def connect() -> FakeDriver:
        return driver

    return Session(connect, paths=Paths(cache=cache), jev_factory=_no_jev)


async def _call(driver: FakeDriver, cache: Path, args: JsonObject, *, locked: bool = False) -> CallToolResult:
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
    session = _session(driver, tmp_path / "dragpt-cache")
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
    session = _session(driver, tmp_path / "dragpt-cache")
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
        ("bring_to_front", {}),
        ("move_cursor", {}),
        ("invoke_menu", {}),
        ("double_click", {"delivery_mode": "foreground"}),
    ],
)
async def test_focus_stealing_calls_are_refused_before_driver_call(
    tool: str, arguments: JsonObject, tmp_path: Path
) -> None:
    driver = RawDriver((tool,))
    result = await _call(driver, tmp_path / "dragpt-cache", {"tool": tool, "arguments": arguments})
    assert _failure(result)["code"] == "foreground_disallowed"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "tool",
    [
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
    result = await _call(driver, tmp_path / "dragpt-cache", {"tool": tool, "arguments": {}})
    assert _failure(result)["code"] == "driver_tool_disallowed"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("schemas_available", [True, False])
async def test_unknown_driver_tools_are_refused(tmp_path: Path, schemas_available: bool) -> None:
    driver = RawDriver(("double_click",)) if schemas_available else FakeDriver()
    result = await _call(driver, tmp_path / "dragpt-cache", {"tool": "unknown", "arguments": {}})
    assert _failure(result)["code"] == "unknown_driver_tool"
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
async def test_screen_lock_prevents_raw_actions(tmp_path: Path) -> None:
    driver = RawDriver(("double_click",))
    result = await _call(driver, tmp_path / "dragpt-cache", {"tool": "double_click", "arguments": {}}, locked=True)
    assert _failure(result) == {
        "status": "failed",
        "code": "screen_locked",
        "message": "the screen is locked; nothing was done. Unlock it and call again",
    }
    assert driver.calls == []


@pytest.mark.usefixtures("enabled")
async def test_raw_driver_refusal_preserves_its_result(tmp_path: Path) -> None:
    data: JsonObject = {"refusal": {"code": "stale_element_token", "message": "token gone"}}
    driver = RawDriver(
        ("zoom",),
        lambda _tool, _args: ToolRefused(
            code="stale_element_token",
            message="token gone",
            data=data,
            text="refused",
            ms=1,
        ),
    )
    result = await _call(driver, tmp_path / "dragpt-cache", {"tool": "zoom", "arguments": {}})
    assert result.is_error
    assert result.structured_content == data
    assert result.content == [TextContent(type="text", text="refused")]


@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("reported_path", [True, False])
async def test_owned_screenshot_is_returned_as_image_and_cleaned_up(tmp_path: Path, reported_path: bool) -> None:
    png = encode_png(blank(2, 2))
    written: list[Path] = []
    requested = tmp_path / "dragpt-caller.png"

    def capture(_tool: str, args: dict[str, object]) -> ToolResult:
        raw = args["screenshot_out_file"]
        assert isinstance(raw, str)
        path = Path(raw)
        assert path != requested
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        path.write_bytes(png)
        written.append(path)
        return ToolOk(data={"screenshot_file_path": raw} if reported_path else {}, text=f"screenshot: {raw}", ms=1)

    driver = RawDriver(("get_window_state",), capture)
    result = await _call(
        driver,
        tmp_path / "dragpt-cache",
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
    outside = tmp_path / "dragpt-private.png"
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
        tmp_path / "dragpt-cache",
        {
            "tool": "get_window_state",
            "arguments": {"screenshot_out_file": str(tmp_path / "dragpt-request.png")},
        },
    )
    assert _failure(result)["code"] == "unsafe_driver_output"
    assert not any(isinstance(c, ImageContent) for c in result.content)
    assert str(outside) not in result.model_dump_json()
    assert str(written[0]) not in result.model_dump_json()
    assert not written[0].parent.exists()
    assert outside.read_bytes() == png


@pytest.mark.usefixtures("enabled")
async def test_unrequested_file_references_are_redacted_without_losing_urls(tmp_path: Path) -> None:
    outside = tmp_path / "dragpt-private.png"
    outside.write_bytes(encode_png(blank(2, 2)))
    url = "https://example.com/apps/details"
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
    result = await _call(driver, tmp_path / "dragpt-cache", {"tool": "list_apps", "arguments": {}})
    assert not result.is_error
    assert result.structured_content == {
        "apps": [{"name": "Editor", "path": "[file omitted]", "icon_file": "[file omitted]", "url": url}],
        "screenshot_file_path": "[file omitted]",
        "details": "local file: [file omitted]",
    }
    assert result.content == [TextContent(type="text", text=f"Editor\n[file omitted]\n{url}")]
    assert not (tmp_path / "dragpt-cache").exists()


@pytest.mark.usefixtures("enabled")
async def test_driver_exceptions_are_sanitized_in_results_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(_tool: str, _args: dict[str, object]) -> ToolResult:
        raise RuntimeError("SENTINEL-private-screen-text")

    driver = RawDriver(("zoom",), fail)
    with caplog.at_level(logging.WARNING):
        result = await _call(driver, tmp_path / "dragpt-cache", {"tool": "zoom", "arguments": {}})
    assert _failure(result)["code"] == "internal"
    assert "SENTINEL" not in result.model_dump_json() + caplog.text
    assert all(record.exc_info is None for record in caplog.records)
