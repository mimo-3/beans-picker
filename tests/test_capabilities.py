from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from mcp import Client, MCPError
from mcp.types import CallToolResult, ImageContent, TextContent

from beans_picker._json import JsonObject
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult
from beans_picker.errors import ToolError
from beans_picker.paths import Paths
from beans_picker.server import create_server
from beans_picker.tools.artifacts import publish
from beans_picker.tools.capabilities import capability_tool
from beans_picker.tools.session import Session
from tests.fakes import FakeDriver
from tests.helpers import blank, encode_png

TARGET: JsonObject = {"pid": 7, "windowId": 10}
FRAME: JsonObject = {"x": 20, "y": 30, "width": 640, "height": 480}
PNG = encode_png(blank(2, 2))


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


class CapabilityDriver(FakeDriver):
    generation = 1
    tool_schemas: ClassVar[dict[str, JsonObject]] = {
        name: {}
        for name in (
            "list_windows",
            "get_window_state",
            "get_desktop_state",
            "clipboard_read",
            "clipboard_write",
            "set_window_frame",
            "get_browser_state",
            "browser_set_input_files",
            "browser_download",
        )
    }


def ok(data: JsonObject) -> ToolOk:
    return ToolOk(data=data, text="", ms=1)


def window_data(pid: int = 7, window_id: int = 10, frame: JsonObject | None = None) -> JsonObject:
    return {"windows": [{"pid": pid, "window_id": window_id, "bounds": frame or FRAME}]}


def text_result(result: object) -> JsonObject:
    if isinstance(result, CallToolResult):
        texts = [item for item in result.content if isinstance(item, TextContent)]
        assert texts
        value: JsonObject = json.loads(texts[0].text)
        return value
    assert isinstance(result, dict)
    return result


async def session_for(driver: FakeDriver, tmp_path: Path) -> Session:
    async def connect() -> FakeDriver:
        return driver

    return Session(connect, paths=Paths(cache=tmp_path / "bp-impl-cache"))


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("screenshot", {}),
        ("screenshot", {"scope": "screen", "out": "relative.png"}),
        ("record_start", {"pid": 7, "out": "/bp-impl-record.mp4"}),
        ("clipboard_write", {}),
        ("clipboard_write", {"text": "x", "imagePath": "/bp-impl-image.png"}),
        ("set_window_frame", {**TARGET, **FRAME, "width": 0}),
        ("set_window_frame", {**TARGET, **FRAME, "x": True}),
        ("browser_upload", {**TARGET, "targetId": "t", "tabId": "a", "ref": "e1", "files": []}),
        ("browser_download", {**TARGET, "targetId": "t", "tabId": "a", "ref": "e1", "out": "relative"}),
    ],
)
async def test_invalid_arguments_are_refused_before_any_mutation(name: str, args: JsonObject, tmp_path: Path) -> None:
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)

    async def unlocked() -> bool:
        return False

    async with Client(create_server(session, screen_locked=unlocked), mode="legacy") as client:
        try:
            result = await client.call_tool(name, args)
        except MCPError:
            pass
        else:
            assert result.is_error
    assert all(tool == "list_windows" for tool in driver.tools)
    await session.close()


@pytest.mark.parametrize("name", ["screenshot", "set_window_frame", "browser_state"])
async def test_target_window_must_belong_to_exact_pid(name: str, tmp_path: Path) -> None:
    driver = CapabilityDriver(lambda tool, args: ok(window_data(pid=99)))
    session = await session_for(driver, tmp_path)
    args = {**TARGET, **FRAME} if name == "set_window_frame" else TARGET
    with pytest.raises(ToolError) as info:
        await capability_tool(session, name, args)
    assert info.value.code == "window_not_found"
    assert driver.tools == ["list_windows"]
    await session.close()


@pytest.mark.parametrize("scope", ["window", "screen"])
async def test_screenshot_publishes_valid_png_and_returns_mcp_image(
    scope: str, tmp_path: Path, permissive_umask: None
) -> None:
    out = tmp_path / "bp-impl-capture.png"

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "list_windows":
            return ok(window_data())
        assert tool == ("get_window_state" if scope == "window" else "get_desktop_state")
        private = Path(str(args["screenshot_out_file"]))
        assert private != out
        assert private.name.startswith("bp-impl-")
        assert private.parent.name.startswith("bp-impl-")
        assert private.parent.stat().st_mode & 0o077 == 0
        private.write_bytes(PNG)
        private.chmod(0o666)
        return ok({"screenshot_file_path": str(private)})

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    args: JsonObject = {"scope": scope, "out": str(out)}
    if scope == "window":
        args.update(TARGET)
    result = await capability_tool(session, "screenshot", args)
    assert isinstance(result, CallToolResult)
    assert any(isinstance(item, ImageContent) and item.mime_type == "image/png" for item in result.content)
    assert text_result(result)["status"] == "done"
    assert out.read_bytes() == PNG
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    images = [item for item in result.content if isinstance(item, ImageContent)]
    assert base64.b64decode(images[0].data) == out.read_bytes()
    assert "activate_app" not in driver.tools
    await session.close()


async def test_screenshot_never_overwrites_destination(tmp_path: Path) -> None:
    out = tmp_path / "bp-impl-existing.png"
    out.write_bytes(b"keep me")
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError):
        await capability_tool(session, "screenshot", {"scope": "screen", "out": str(out)})
    assert out.read_bytes() == b"keep me"
    assert driver.calls == []
    await session.close()


@pytest.mark.parametrize("attack", ["outside", "symlink", "invalid_png", "missing"])
async def test_screenshot_refuses_untrusted_or_invalid_driver_artifacts(attack: str, tmp_path: Path) -> None:
    outside = tmp_path / "bp-impl-private.png"
    outside.write_bytes(PNG)
    out = tmp_path / "bp-impl-published.png"

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        path = Path(str(args["screenshot_out_file"]))
        if attack == "outside":
            return ok({"screenshot_file_path": str(outside)})
        if attack == "symlink":
            path.symlink_to(outside)
        elif attack == "invalid_png":
            path.write_bytes(b"not png")
        return ok({"screenshot_file_path": str(path)})

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError):
        await capability_tool(session, "screenshot", {"scope": "screen", "out": str(out)})
    assert not out.exists()
    assert outside.read_bytes() == PNG
    await session.close()


async def test_clipboard_read_requests_text_and_returns_it(tmp_path: Path) -> None:
    driver = CapabilityDriver(lambda tool, args: ok({"text": "ミモ\nhello", "types": ["public.utf8-plain-text"]}))
    session = await session_for(driver, tmp_path)
    result = text_result(await capability_tool(session, "clipboard_read", {}))
    assert result["text"] == "ミモ\nhello"
    assert driver.calls == [("clipboard_read", {"include_text": True})]
    await session.close()


@pytest.mark.parametrize(("readback", "status"), [("hello", "done"), ("old text", "mismatch")])
async def test_clipboard_write_checks_text_readback(readback: str, status: str, tmp_path: Path) -> None:
    driver = CapabilityDriver(lambda tool, args: ok({"text": readback}) if tool == "clipboard_read" else ok({}))
    session = await session_for(driver, tmp_path)
    result = text_result(await capability_tool(session, "clipboard_write", {"text": "hello"}))
    assert result["status"] == status
    assert driver.calls == [("clipboard_write", {"text": "hello"}), ("clipboard_read", {"include_text": True})]
    await session.close()


@pytest.mark.parametrize(("actual", "status"), [(FRAME, "done"), ({**FRAME, "width": 600}, "mismatch")])
async def test_window_frame_result_is_based_on_fresh_window_readback(
    actual: JsonObject, status: str, tmp_path: Path
) -> None:
    reads = 0

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal reads
        if tool == "list_windows":
            reads += 1
            return ok(window_data(frame=actual if reads > 1 else {**FRAME, "x": 0}))
        assert tool == "set_window_frame"
        assert args == {"pid": 7, "window_id": 10, **FRAME}
        return ok({"success": True})

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    result = text_result(await capability_tool(session, "set_window_frame", {**TARGET, **FRAME}))
    assert result["status"] == status
    assert result["frame"] == actual
    assert driver.tools == ["list_windows", "set_window_frame", "list_windows"]
    await session.close()


async def test_foreground_required_is_preserved_without_retry_or_activation(tmp_path: Path) -> None:
    def response(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "list_windows":
            return ok(window_data())
        return ToolRefused(code="foreground_required", message="background unavailable", data={}, text="", ms=1)

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "set_window_frame", {**TARGET, **FRAME})
    assert info.value.code == "foreground_required"
    assert driver.tools == ["list_windows", "set_window_frame"]
    await session.close()


class BrowserDriver(CapabilityDriver):
    def __init__(self, *, quality: str = "exact", tabs: list[str] | None = None) -> None:
        self.quality = quality
        self.tabs = tabs if tabs is not None else ["tab"]
        self.download_refusal: str | None = None
        self.download_attack: str | None = None
        super().__init__(self.response)

    def response(self, tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "list_windows":
            return ok(window_data())
        if tool == "get_browser_state":
            if "target_id" not in args:
                return ok(
                    {
                        "status": "ok",
                        "target_id": "target",
                        "binding_quality": self.quality,
                        "mutation_allowed": self.quality == "exact",
                        "tabs": [{"tab_id": tab} for tab in self.tabs],
                    }
                )
            assert args["target_id"] == "target"
            assert args["tab_id"] in self.tabs
            assert args["snapshot_format"] == "semantic_v2"
            return ok(
                {
                    "status": "ok",
                    "target_id": "target",
                    "tab_id": args["tab_id"],
                    "snapshot": {
                        "id": "p1",
                        "refs": [{"ref": "p1:0", "role": "button"}],
                        "outline": "Upload file",
                    },
                }
            )
        if tool == "browser_set_input_files":
            return ok({"status": "ok", "file_count": 1})
        if tool == "browser_download":
            if self.download_refusal:
                return ToolRefused(
                    code=self.download_refusal, message="explicit browser consent needed", data={}, text="", ms=1
                )
            folder = Path(str(args["destination_root"]))
            assert folder.name.startswith("bp-impl-")
            assert folder.stat().st_mode & 0o077 == 0
            ident = "bp-impl-download-id"
            if self.download_attack == "outside":
                ident = "../bp-impl-outside"
            elif self.download_attack == "symlink":
                (folder / ident).symlink_to(folder.parent / "bp-impl-outside")
            elif self.download_attack != "missing":
                (folder / ident).write_bytes(b"abc")
                (folder / ident).chmod(0o666)
            size = 8 if self.download_attack == "wrong_size" else 3
            return ok({"status": "completed", "download_id": ident, "bytes": size})
        raise AssertionError(f"unexpected driver tool {tool}")


async def test_browser_state_binds_exact_window_and_returns_semantic_snapshot(tmp_path: Path) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    result = text_result(await capability_tool(session, "browser_state", TARGET))
    assert result["status"] == "done"
    assert result["targetId"] == "target"
    assert result["tabId"] == "tab"
    assert driver.tools == ["list_windows", "get_browser_state", "get_browser_state"]
    assert driver.calls[1][1] == {"pid": 7, "window_id": 10}
    await session.close()


async def test_browser_state_refuses_heuristic_binding(tmp_path: Path) -> None:
    driver = BrowserDriver(quality="heuristic")
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError):
        await capability_tool(session, "browser_state", TARGET)
    assert driver.tools == ["list_windows", "get_browser_state"]
    await session.close()


async def test_browser_state_requires_tab_choice_when_binding_has_multiple_tabs(tmp_path: Path) -> None:
    driver = BrowserDriver(tabs=["one", "two"])
    session = await session_for(driver, tmp_path)
    ambiguous = text_result(await capability_tool(session, "browser_state", TARGET))
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["tabs"] == [{"tab_id": "one"}, {"tab_id": "two"}]
    assert driver.tools == ["list_windows", "get_browser_state"]
    result = text_result(await capability_tool(session, "browser_state", {**TARGET, "tabId": "two"}))
    assert result["tabId"] == "two"
    assert result["targetId"] == ambiguous["targetId"]
    assert driver.tools == ["list_windows", "get_browser_state", "list_windows", "get_browser_state"]
    assert driver.calls[-1][1] == {
        "target_id": "target",
        "tab_id": "two",
        "snapshot_format": "semantic_v2",
    }
    await session.close()


async def test_upload_uses_live_binding_and_reads_back_page_without_claiming_verified_effect(tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-upload.txt"
    source.write_text("payload")
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = text_result(
        await capability_tool(
            session,
            "browser_upload",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "files": [str(source)],
            },
        )
    )
    assert result["status"] == "unverified"
    assert result["fileCount"] == 1
    assert driver.tools[-2:] == ["browser_set_input_files", "get_browser_state"]
    payload = driver.calls[-2][1]
    assert payload["files"] == [str(source)]
    assert payload["target_id"] == "target"
    assert payload["tab_id"] == "tab"
    assert "activate_app" not in driver.tools
    await session.close()


@pytest.mark.parametrize("name", ["browser_upload", "browser_download"])
async def test_browser_mutations_reject_capabilities_not_observed_in_this_session(name: str, tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-upload.txt"
    source.write_text("payload")
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    args: JsonObject = {**TARGET, "targetId": "foreign", "tabId": "tab", "ref": "p1:0"}
    args.update({"files": [str(source)]} if name == "browser_upload" else {"out": str(tmp_path / "bp-impl-out")})
    with pytest.raises(ToolError):
        await capability_tool(session, name, args)
    assert "browser_set_input_files" not in driver.tools
    assert "browser_download" not in driver.tools
    await session.close()


async def test_upload_refuses_symlink_source_before_page_mutation(tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-real.txt"
    source.write_text("payload")
    link = tmp_path / "bp-impl-link.txt"
    link.symlink_to(source)
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    with pytest.raises(ToolError):
        await capability_tool(
            session,
            "browser_upload",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "files": [str(link)],
            },
        )
    assert "browser_set_input_files" not in driver.tools
    await session.close()


async def test_download_publishes_only_the_verified_completed_artifact(tmp_path: Path, permissive_umask: None) -> None:
    out = tmp_path / "bp-impl-download.bin"
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = text_result(
        await capability_tool(
            session,
            "browser_download",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "out": str(out),
            },
        )
    )
    assert result["status"] == "done"
    assert result["path"] == str(out)
    assert result["bytes"] == 3
    assert out.read_bytes() == b"abc"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert driver.calls[-1][1].get("delivery_mode", "background") == "background"
    await session.close()


@pytest.mark.parametrize("replacement", ["symlink", "regular"])
def test_publish_rejects_source_swap_before_link_without_exposing_replacement(
    replacement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bp-impl-staged"
    outside = tmp_path / "bp-impl-outside"
    out = tmp_path / "bp-impl-published"
    source.write_bytes(b"abc")
    source.chmod(0o666)
    outside.write_bytes(b"private")
    outside.chmod(0o644)
    link = os.link

    def replace_then_link(*args: object, **kwargs: object) -> None:
        source.unlink()
        if replacement == "symlink":
            source.symlink_to(outside)
        else:
            source.write_bytes(b"unverified")
        link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", replace_then_link)
    with pytest.raises(ToolError) as info:
        publish(source, out, expected_bytes=3)
    assert info.value.code == "unsafe_driver_output"
    assert not out.exists()
    assert not out.is_symlink()
    assert outside.read_bytes() == b"private"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_publish_does_not_remove_a_destination_replaced_after_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bp-impl-staged"
    out = tmp_path / "bp-impl-published"
    source.write_bytes(b"abc")
    link = os.link

    def replace_destination(*args: object, **kwargs: object) -> None:
        link(*args, **kwargs)  # type: ignore[arg-type]
        out.unlink()
        out.write_bytes(b"keep replacement")

    monkeypatch.setattr(os, "link", replace_destination)
    with pytest.raises(ToolError):
        publish(source, out)
    assert out.read_bytes() == b"keep replacement"


def test_publish_rechecks_expected_size_after_setting_private_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bp-impl-staged"
    out = tmp_path / "bp-impl-published"
    source.write_bytes(b"abc")
    chmod = os.fchmod

    def mutate_during_chmod(fd: int, mode: int) -> None:
        chmod(fd, mode)
        source.write_bytes(b"changed size")

    monkeypatch.setattr(os, "fchmod", mutate_during_chmod)
    with pytest.raises(ToolError) as info:
        publish(source, out, expected_bytes=3)
    assert info.value.code == "unverified_output"
    assert not out.exists()


async def test_download_preserves_browser_consent_refusal(tmp_path: Path) -> None:
    out = tmp_path / "bp-impl-consent.bin"
    driver = BrowserDriver()
    driver.download_refusal = "browser_consent_required"
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    with pytest.raises(ToolError) as info:
        await capability_tool(
            session,
            "browser_download",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "out": str(out),
            },
        )
    assert info.value.code == "browser_consent_required"
    assert not out.exists()
    assert "activate_app" not in driver.tools
    await session.close()


@pytest.mark.parametrize("attack", ["outside", "symlink", "wrong_size", "missing"])
async def test_download_rejects_untrusted_or_incomplete_artifacts(attack: str, tmp_path: Path) -> None:
    outside = tmp_path / "bp-impl-outside"
    outside.write_bytes(b"preserve")
    out = tmp_path / "bp-impl-download.bin"
    driver = BrowserDriver()
    driver.download_attack = attack
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    with pytest.raises(ToolError):
        await capability_tool(
            session,
            "browser_download",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "out": str(out),
            },
        )
    assert not out.exists()
    assert outside.read_bytes() == b"preserve"
    await session.close()


async def test_missing_driver_schema_fails_explicitly_without_using_an_unknown_route(tmp_path: Path) -> None:
    class NoSchemas(CapabilityDriver):
        tool_schemas: ClassVar[dict[str, JsonObject]] = {}

    driver = NoSchemas()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "clipboard_read", {})
    assert info.value.code == "unknown_driver_tool"
    assert driver.calls == []
    await session.close()


async def test_clipboard_image_write_reports_unverified_pixel_identity(tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-clipboard.png"
    source.write_bytes(PNG)
    driver = CapabilityDriver(lambda tool, args: ok({"types": ["public.png"]}))
    session = await session_for(driver, tmp_path)
    result = text_result(await capability_tool(session, "clipboard_write", {"imagePath": str(source)}))
    assert result["status"] == "unverified"
    assert result["types"] == ["public.png"]
    assert driver.calls == [
        ("clipboard_write", {"image_path": str(source)}),
        ("clipboard_read", {"include_text": True}),
    ]
    await session.close()


async def test_browser_mutation_rejects_binding_after_driver_reconnect(tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-upload.txt"
    source.write_text("payload")
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    driver.generation += 1
    with pytest.raises(ToolError) as info:
        await capability_tool(
            session,
            "browser_upload",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "files": [str(source)],
            },
        )
    assert info.value.code == "browser_binding_required"
    assert "browser_set_input_files" not in driver.tools
    await session.close()


async def test_browser_mutation_rejects_binding_reused_for_another_window(tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-upload.txt"
    source.write_text("payload")
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    original = driver.on_call

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "list_windows":
            return ok(window_data(pid=8, window_id=11))
        assert original is not None
        result = original(tool, args)
        assert isinstance(result, ToolOk | ToolRefused)
        return result

    driver.on_call = response
    with pytest.raises(ToolError) as info:
        await capability_tool(
            session,
            "browser_upload",
            {
                "pid": 8,
                "windowId": 11,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "files": [str(source)],
            },
        )
    assert info.value.code == "browser_binding_required"
    assert "browser_set_input_files" not in driver.tools
    await session.close()


async def test_reconnect_during_snapshot_does_not_mint_a_usable_binding(tmp_path: Path) -> None:
    source = tmp_path / "bp-impl-upload.txt"
    source.write_text("payload")
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    original = driver.on_call

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        assert original is not None
        result = original(tool, args)
        assert isinstance(result, ToolOk | ToolRefused)
        if tool == "get_browser_state" and "target_id" in args:
            driver.generation += 1
        return result

    driver.on_call = response
    await capability_tool(session, "browser_state", TARGET)
    with pytest.raises(ToolError) as info:
        await capability_tool(
            session,
            "browser_upload",
            {
                **TARGET,
                "targetId": "target",
                "tabId": "tab",
                "ref": "p1:0",
                "files": [str(source)],
            },
        )
    assert info.value.code == "browser_binding_required"
    assert "browser_set_input_files" not in driver.tools
    await session.close()


async def test_screenshot_can_return_only_saved_file_metadata(tmp_path: Path) -> None:
    out = tmp_path / "bp-impl-file-only.png"

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        staged = Path(str(args["screenshot_out_file"]))
        staged.write_bytes(PNG)
        return ok({"screenshot_file_path": str(staged)})

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    result = await capability_tool(
        session,
        "screenshot",
        {
            "scope": "screen",
            "out": str(out),
            "includeImage": False,
        },
    )
    assert isinstance(result, CallToolResult)
    assert all(isinstance(item, TextContent) for item in result.content)
    assert out.read_bytes() == PNG
    await session.close()


async def test_screenshot_image_only_uses_session_private_cache_and_cleans_stage(tmp_path: Path) -> None:
    stages: list[Path] = []

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        staged = Path(str(args["screenshot_out_file"]))
        stages.append(staged.parent)
        staged.write_bytes(PNG)
        return ok({"screenshot_file_path": str(staged)})

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    result = await capability_tool(session, "screenshot", {"scope": "screen"})
    assert isinstance(result, CallToolResult)
    assert "path" not in text_result(result)
    assert any(isinstance(item, ImageContent) for item in result.content)
    assert not stages[0].exists()
    await session.close()


async def test_mcp_record_tools_dispatch_exact_window_to_session_recorder(tmp_path: Path) -> None:
    from typing import cast

    from beans_picker.recording import Recorder

    starts: list[tuple[int, int, Path]] = []
    stops: list[bool] = []

    class OwnedRecorder:
        async def start(self, pid: int, window_id: int, out: Path) -> dict[str, object]:
            starts.append((pid, window_id, out))
            return {"status": "recording", "path": str(out)}

        async def stop(self) -> dict[str, object]:
            stops.append(True)
            return {"status": "done", "path": str(tmp_path / "bp-impl-owned.mp4"), "durationSeconds": 2.5}

        async def close(self) -> None:
            pass

    async def unlocked() -> bool:
        return False

    driver = CapabilityDriver(lambda tool, args: ok(window_data()))
    session = await session_for(driver, tmp_path)
    session.recorder = cast(Recorder, OwnedRecorder())
    out = tmp_path / "bp-impl-owned.mp4"
    async with Client(create_server(session, screen_locked=unlocked), mode="legacy") as client:
        started = await client.call_tool("record_start", {**TARGET, "out": str(out)})
        stopped = await client.call_tool("record_stop", {})
    assert text_result(started)["status"] == "recording"
    assert text_result(stopped)["durationSeconds"] == 2.5
    assert starts == [(7, 10, out)]
    assert stops == [True]
    assert driver.tools == ["list_windows"]
    await session.close()
