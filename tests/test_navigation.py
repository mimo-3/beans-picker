from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from mcp import Client

from beans_picker._json import JsonObject
from beans_picker._text import WS_CODE_POINTS
from beans_picker.driver.types import ToolResult
from beans_picker.errors import ToolError
from beans_picker.server import create_server
from beans_picker.tools.capabilities import capability_tool
from beans_picker.tools.capability_args import browser_url
from beans_picker.tools.driver import ALLOWED_TOOLS
from beans_picker.tools.session import Target
from tests.fakes import FakeRunner, RecordingSleep
from tests.test_capabilities import TARGET, BrowserDriver, CapabilityDriver, ok, session_for, text_result, window_data

BROWSER: JsonObject = {**TARGET, "targetId": "target", "tabId": "tab"}
URL = "https://example.test/"
NEW_TOOLS = ["browser_navigate", "browser_dialog", "windows", "launch_app"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///tmp/a",
        "blob:https://example.test/a",
        "chrome://settings",
        "ftp://example.test/",
        "https:///missing",
        "http:",
        "https://[broken",
        "//example.test/",
        "",
        "https://example.test/\x00",
        "https://example.test/\x7f",
        "https://example.test/\x85",
        "https://example.test/" + "a" * 8192,
        "https://example.test/" + "\U0001f600" * 4090,
        *["https://example.test/" + chr(c) for c in WS_CODE_POINTS],
    ],
)
async def test_bad_navigation_url_is_never_dispatched(url: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    calls = len(driver.tools)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "browser_navigate", {**BROWSER, "url": url})
    assert info.value.code == "bad_arguments"
    assert len(driver.tools) == calls
    assert not driver.sentinel.watching
    await session.close()


@pytest.mark.parametrize("url", [URL, "HTTP://example.test/", "HTTPS://example.test/a?b=c#d", "ABOUT:blank"])
async def test_navigation_reads_fresh_page_and_preserves_requested_url(url: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.page_url = url
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = text_result(await capability_tool(session, "browser_navigate", {**BROWSER, "url": url}))
    assert result["status"] == "done"
    assert result["url"] == result["finalUrl"] == url
    assert "snapshot" in result
    assert driver.calls[-2] == ("browser_navigate", {"target_id": "target", "tab_id": "tab", "url": url})
    assert driver.tools[-1] == "get_browser_state"
    assert not driver.sentinel.watching
    await session.close()


@pytest.mark.parametrize(
    ("page", "final", "status"),
    [
        ("https://example.test/redirect", "https://example.test/redirect", "done"),
        ("https://other.test/", URL, "unverified"),
        (None, URL, "unverified"),
    ],
)
async def test_navigation_verifies_redirects_and_never_claims_missing_readback(
    page: str | None, final: str, status: str, tmp_path: Path
) -> None:
    driver = BrowserDriver()
    driver.page_url = page
    driver.navigate_result = {"final_url": final}
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = text_result(await capability_tool(session, "browser_navigate", {**BROWSER, "url": URL}))
    assert result["status"] == status
    assert ("reason" in result) == (status == "unverified")
    assert ("finalUrl" in result) == (page is not None)
    await session.close()


@pytest.mark.parametrize("name", ["browser_navigate", "browser_dialog"])
@pytest.mark.parametrize("invalid", ["missing", "reconnect", "foreign_window", "foreign_tab", "foreign_target"])
async def test_new_browser_tools_require_the_current_exact_session_binding(
    name: str, invalid: str, tmp_path: Path
) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    if invalid != "missing":
        await capability_tool(session, "browser_state", TARGET)
    args = {**BROWSER, "url": URL} if name == "browser_navigate" else {**BROWSER, "action": "inspect"}
    if invalid == "reconnect":
        driver.generation += 1
    elif invalid == "foreign_window":
        session.browser_bindings[("target", "tab")] = (Target(8, 11), 1)
    elif invalid == "foreign_tab":
        args["tabId"] = "foreign"
    elif invalid == "foreign_target":
        args["targetId"] = "foreign"
    with pytest.raises(ToolError) as info:
        await capability_tool(session, name, args)
    assert info.value.code == "browser_binding_required"
    assert name not in driver.tools
    await session.close()


@pytest.mark.parametrize("kind", ["alert", "confirm", "prompt", "beforeunload"])
async def test_dialog_inspect_returns_driver_data_without_trying_a_blocked_snapshot(kind: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.dialog_result.update(kind=kind, message="Exact message\n ")
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = text_result(await capability_tool(session, "browser_dialog", {**BROWSER, "action": "inspect"}))
    assert result == {"status": "done", "present": True, "dialogId": "d1", "type": kind, "message": "Exact message\n "}
    assert driver.tools[-1] == "browser_dialog"
    await session.close()


@pytest.mark.parametrize("action", ["accept", "dismiss"])
@pytest.mark.parametrize("receipt", ["resolved", "current", "status", "unverified", "unresolved"])
async def test_dialog_resolution_refreshes_only_after_resolution(action: str, receipt: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.dialog_result = {"status": "ok", "dialog_id": "d1"}
    if receipt == "resolved":
        driver.dialog_result["resolved"] = True
    elif receipt == "current":
        driver.dialog_result["action"] = action
    elif receipt == "status":
        driver.dialog_result["status"] = "resolved"
    elif receipt == "unresolved":
        driver.dialog_result.update(resolved=False, action=action)
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    args: JsonObject = {**BROWSER, "action": action, "dialogId": "d1", "allowDestructive": True}
    if action == "accept":
        args["promptText"] = "\n exact \U0001f600 "
    result = text_result(await capability_tool(session, "browser_dialog", args))
    verified = receipt not in {"unverified", "unresolved"}
    assert result["status"] == ("done" if verified else "unverified")
    assert ("snapshot" in result) == verified
    payload = next(args for name, args in driver.calls if name == "browser_dialog")
    assert payload.get("delivery_mode", "background") == "background"
    assert payload["dialog_id"] == "d1"
    assert payload.get("prompt_text") == args.get("promptText")
    assert not driver.sentinel.watching
    await session.close()


@pytest.mark.parametrize("allow", [False, None])
async def test_accept_without_confirmation_never_dispatches(allow: bool | None, tmp_path: Path) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = text_result(
        await capability_tool(
            session,
            "browser_dialog",
            {
                **BROWSER,
                "action": "accept",
                "dialogId": "d1",
                "allowDestructive": allow,
            },
        )
    )
    assert result["status"] == "needs_confirmation"
    assert "browser_dialog" not in driver.tools
    await session.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"action": "wrong"},
        {"action": "dismiss"},
        {"action": "accept"},
        {"action": "inspect", "promptText": "no"},
        {"action": "dismiss", "dialogId": "d1", "promptText": "no"},
        {"action": "accept", "dialogId": "d1", "promptText": 1},
    ],
)
async def test_invalid_dialog_arguments_do_not_dispatch(extra: JsonObject, tmp_path: Path) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "browser_dialog", {**BROWSER, **extra})
    assert info.value.code == "bad_arguments"
    assert "browser_dialog" not in driver.tools
    await session.close()


@pytest.mark.parametrize("name", NEW_TOOLS)
async def test_new_tools_preserve_missing_driver_capabilities(name: str, tmp_path: Path) -> None:
    class OlderDriver(BrowserDriver):
        tool_schemas: ClassVar[dict[str, JsonObject]] = (
            {
                "list_windows": {},
                "get_browser_state": {},
            }
            if name != "windows"
            else {}
        )

    driver = OlderDriver()
    session = await session_for(driver, tmp_path)
    # OS access remains behind the fake runner even for an unknown launch capability.
    session._runner = FakeRunner()
    args: JsonObject = {"name": "Test"} if name == "launch_app" else {}
    if name.startswith("browser_"):
        await capability_tool(session, "browser_state", TARGET)
        args = {**BROWSER, "url": URL} if name == "browser_navigate" else {**BROWSER, "action": "inspect"}
    with pytest.raises(ToolError) as info:
        await capability_tool(session, name, args)
    assert info.value.code == "unknown_driver_tool"
    assert name not in driver.tools
    assert not driver.sentinel.watching
    await session.close()


@pytest.mark.parametrize(
    ("filters", "ids"),
    [
        ({}, [10, 11, 12, 13]),
        ({"app": "sAfArI"}, [10, 11]),
        ({"app": "Saf"}, []),
        ({"pid": 7}, [10]),
        ({"onScreenOnly": True}, [11]),
        ({"onScreenOnly": False}, [10, 11, 12, 13]),
        ({"app": "Safari", "pid": 8, "onScreenOnly": True}, [11]),
    ],
)
async def test_windows_filters_exact_live_records(filters: JsonObject, ids: list[int], tmp_path: Path) -> None:
    data: JsonObject = {
        "windows": [
            {
                "pid": 7,
                "window_id": 10,
                "app_name": "Safari",
                "title": "Exact",
                "bounds": {"x": -10, "y": 20, "width": 640, "height": 480},
                "is_on_screen": True,
                "on_current_space": False,
                "z_index": 2,
            },
            {"pid": 8, "window_id": 11, "app_name": "Safari", "is_on_screen": False},
            {"pid": 9, "window_id": 12, "app_name": "Safari Preview", "is_on_screen": True},
            {"pid": 10, "window_id": 13},
        ]
    }

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        # The driver filters by current Space, independently of is_on_screen.
        if args.get("on_screen_only") is True:
            return ok({"windows": [{"pid": 8, "window_id": 11, "app_name": "Safari", "is_on_screen": False}]})
        return ok(data)

    driver = CapabilityDriver(response)
    session = await session_for(driver, tmp_path)
    result = text_result(await capability_tool(session, "windows", filters))
    assert result["status"] == "done"
    windows = result["windows"]
    assert isinstance(windows, list)
    assert [w["windowId"] for w in windows if isinstance(w, dict)] == ids
    if 10 in ids:
        expected: JsonObject = {
            "pid": 7,
            "windowId": 10,
            "app": "Safari",
            "bounds": {"x": -10.0, "y": 20.0, "width": 640.0, "height": 480.0},
            "onScreen": True,
            "onCurrentSpace": False,
            "zIndex": 2,
        }
        if "app" in filters or "pid" in filters:
            expected["title"] = "Exact"
        assert windows[0] == expected
    payload = {"pid": filters["pid"]} if "pid" in filters else {}
    if "onScreenOnly" in filters:
        payload["on_screen_only"] = filters["onScreenOnly"]
    assert driver.calls == [("list_windows", payload)]
    assert not driver.sentinel.watching
    await session.close()


async def test_launch_maps_only_the_typed_fields_and_reports_ready_windows(tmp_path: Path) -> None:
    source = tmp_path / "bpnav-fix-folder"
    source.mkdir()
    driver = CapabilityDriver(
        lambda tool, args: ok(
            {
                **window_data(),
                "pid": 7,
                "name": "Test",
                "bundle_id": "test.app",
                "launch_state": {"window_ready": True},
                "self_activation_suppressed": True,
            }
        )
    )
    session = await session_for(driver, tmp_path)
    session._runner = FakeRunner()
    result = text_result(
        await capability_tool(
            session,
            "launch_app",
            {
                "bundleId": "test.app",
                "name": "Test",
                "urls": [URL, str(source), str(tmp_path)],
                "newInstance": True,
            },
        )
    )
    assert result["status"] == "done"
    assert result["pid"] == 7
    assert result["bundleId"] == "test.app"
    assert result["name"] == "Test"
    assert driver.calls == [
        (
            "launch_app",
            {
                "bundle_id": "test.app",
                "name": "Test",
                "urls": [URL, str(source), str(tmp_path)],
                "creates_new_application_instance": True,
            },
        )
    ]
    await session.close()


@pytest.mark.parametrize("ready", [False, None])
async def test_launch_without_ready_window_is_unverified(ready: bool | None, tmp_path: Path) -> None:
    data: JsonObject = {"pid": 7, "windows": []}
    if ready is not None:
        data.update(window_data())
        data["launch_state"] = {"window_ready": ready}
    driver = CapabilityDriver(lambda tool, args: ok(data))
    session = await session_for(driver, tmp_path)
    session._runner = FakeRunner()
    result = text_result(await capability_tool(session, "launch_app", {"name": "Test", "urls": []}))
    assert result["status"] == "unverified"
    await session.close()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"name": ""},
        {"name": "Test", "urls": ["about:blank"]},
        {"name": "Test", "urls": ["file:///tmp/a"]},
        {"name": "Test", "urls": ["relative"]},
        {"name": "Test", "urls": [URL] * 17},
        {"name": "Test", "urls": "wrong"},
        {"name": "Test", "urls": [1]},
    ],
)
async def test_invalid_launch_never_dispatches(args: JsonObject, tmp_path: Path) -> None:
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError):
        await capability_tool(session, "launch_app", args)
    assert driver.calls == []
    await session.close()


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo"])
async def test_launch_rejects_unsafe_local_paths(kind: str, tmp_path: Path) -> None:
    import os

    path = tmp_path / "bpnav-fix-input"
    if kind == "symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(path)
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "launch_app", {"name": "Test", "urls": [str(path)]})
    assert info.value.code == "bad_path"
    assert driver.calls == []
    await session.close()


@pytest.mark.parametrize("suppressed", [True, False])
@pytest.mark.parametrize("restored", [True, False])
async def test_launch_focus_steal_restores_previous_app_and_fails(
    suppressed: bool, restored: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = CapabilityDriver(lambda tool, args: ok({"pid": 7, "self_activation_suppressed": suppressed}))
    session = await session_for(driver, tmp_path)
    samples = iter([99, 7 if suppressed else 100])
    restores: list[int] = []

    async def front() -> int | None:
        return next(samples)

    async def restore(pid: int) -> bool:
        restores.append(pid)
        return restored

    monkeypatch.setattr(session, "front", front)
    monkeypatch.setattr(session, "restore_front", restore)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "launch_app", {"name": "Test"})
    assert info.value.code == "foreground_violation"
    assert info.value.message.endswith("was put back" if restored else "was not put back")
    assert restores == [99]
    await session.close()


@pytest.mark.parametrize("prior", [None, 7])
async def test_launch_does_not_activate_the_target_as_a_restore(
    prior: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = CapabilityDriver(lambda tool, args: ok({"pid": 7, "self_activation_suppressed": False}))
    session = await session_for(driver, tmp_path)

    async def front() -> int | None:
        return prior

    monkeypatch.setattr(session, "front", front)
    with pytest.raises(ToolError) as info:
        await capability_tool(session, "launch_app", {"name": "Test"})
    assert info.value.message.endswith("not put back")
    await session.close()


@pytest.mark.parametrize("outcome", ["done", "refused", "timeout", "exception"])
async def test_session_restore_checks_the_actual_front_app(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await session_for(CapabilityDriver(), tmp_path)
    sleep = RecordingSleep()
    session._sleep = sleep

    async def activate(pid: int) -> bool:
        assert pid == 99
        if outcome == "exception":
            raise RuntimeError("helper failed")
        return outcome != "refused"

    async def front() -> int | None:
        return 99 if outcome == "done" else 7

    monkeypatch.setattr(session, "_activate", activate)
    monkeypatch.setattr(session, "front", front)
    assert await session.restore_front(99) is (outcome == "done")
    assert len(sleep.delays) == (6 if outcome == "timeout" else 0)
    await session.close()


@pytest.mark.parametrize("name", NEW_TOOLS)
async def test_new_tools_remain_screen_lock_guarded(name: str, tmp_path: Path) -> None:
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)

    async def locked() -> bool:
        return True

    args = (
        {**BROWSER, "url": URL}
        if name == "browser_navigate"
        else {**BROWSER, "action": "inspect"}
        if name == "browser_dialog"
        else {"name": "Test"}
        if name == "launch_app"
        else {}
    )
    async with Client(create_server(session, screen_locked=locked), mode="legacy") as client:
        result = text_result(await client.call_tool(name, args))
    assert result["code"] == "screen_locked"
    assert driver.calls == []
    await session.close()


def test_raw_allowlist_is_unchanged_and_url_length_uses_utf16() -> None:
    assert not {"browser_navigate", "browser_dialog", "launch_app"} & ALLOWED_TOOLS
    assert browser_url("https://example.test/" + "a" * (8192 - len(URL)))


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("launch_app", {"name": "Test", "newInstance": "yes"}),
        ("launch_app", {"name": "Test", "additional_arguments": ["--unsafe"]}),
        ("launch_app", {"name": "Test", "webkit_inspector_port": 9222}),
        ("launch_app", {"name": "Test", "urls": [URL] * 17}),
        ("windows", {"pid": True}),
        ("windows", {"onScreenOnly": "yes"}),
        ("browser_navigate", {**BROWSER, "url": 1}),
        ("browser_navigate", {**BROWSER, "url": URL, "delivery_mode": "foreground"}),
        ("browser_dialog", {**BROWSER, "action": "invalid"}),
        ("browser_dialog", {**BROWSER, "action": "accept", "dialogId": "d1", "allowDestructive": "yes"}),
    ],
)
async def test_new_public_schemas_reject_invalid_and_unsafe_inputs(name: str, args: JsonObject, tmp_path: Path) -> None:
    from tests.raw_mcp import raw_client

    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)

    async def unlocked() -> bool:
        return False

    async with raw_client(create_server(session, screen_locked=unlocked)) as client:
        await client.initialize()
        result = await client.call_tool(name, args)
    assert result["isError"] is True
    assert driver.calls == []
    await session.close()


async def test_new_tools_dispatch_through_the_public_mcp_surface(tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.page_url = URL
    original = driver.response

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "launch_app":
            return ok({**window_data(), "pid": 7, "name": "Test", "bundle_id": "test.app"})
        if tool == "browser_dialog" and args["action"] == "dismiss":
            assert driver.sentinel.watching
            return ok({"status": "ok", "dialog_id": "d1", "action": "dismiss"})
        if tool == "browser_navigate":
            assert driver.sentinel.watching
        return original(tool, args)

    driver.on_call = response
    session = await session_for(driver, tmp_path)
    session._runner = FakeRunner()

    async def unlocked() -> bool:
        return False

    async with Client(create_server(session, screen_locked=unlocked), mode="legacy") as client:
        windows = text_result(await client.call_tool("windows", {}))
        assert windows["windows"]
        launched = text_result(await client.call_tool("launch_app", {"name": "Test"}))
        assert launched["status"] == "done"
        await client.call_tool("browser_state", TARGET)
        navigated = text_result(await client.call_tool("browser_navigate", {**BROWSER, "url": URL}))
        assert navigated["status"] == "done"
        inspected = text_result(await client.call_tool("browser_dialog", {**BROWSER, "action": "inspect"}))
        assert inspected["dialogId"] == "d1"
        dismissed = text_result(
            await client.call_tool("browser_dialog", {**BROWSER, "action": "dismiss", "dialogId": "d1"})
        )
        assert dismissed["status"] == "done"
    assert not driver.sentinel.watching
    await session.close()


@pytest.mark.parametrize("name", ["browser_navigate", "browser_dialog"])
@pytest.mark.parametrize("code", ["foreground_required", "browser_action_unavailable"])
async def test_browser_refusals_preserve_codes_and_stop_the_activation_watch(
    name: str, code: str, tmp_path: Path
) -> None:
    from beans_picker.driver.types import ToolRefused

    driver = BrowserDriver()
    original = driver.response

    def response(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == name:
            assert driver.sentinel.watching
            return ToolRefused(code=code, message="background refusal", data={}, text="", ms=1)
        return original(tool, args)

    driver.on_call = response
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    args = {**BROWSER, "url": URL} if name == "browser_navigate" else {**BROWSER, "action": "inspect"}
    with pytest.raises(ToolError) as info:
        await capability_tool(session, name, args)
    assert info.value.code == code
    assert not driver.sentinel.watching
    await session.close()
