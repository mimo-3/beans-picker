from __future__ import annotations

from pathlib import Path

import pytest

from beans_picker._json import JsonObject
from beans_picker._text import utf16_len
from beans_picker.driver.sentinel import RESTORE_CHECK_INTERVAL_S, RESTORE_CHECKS
from beans_picker.driver.types import ToolRefused, ToolResult
from beans_picker.errors import ToolError
from beans_picker.tools.capabilities import capability_tool
from tests.fakes import FakeRunner, RecordingSleep
from tests.test_capabilities import TARGET, BrowserDriver, CapabilityDriver, ok, session_for, window_data
from tests.test_navigation import BROWSER, URL


@pytest.mark.parametrize(
    "kind",
    ["file", "app", "bundle", "pkg", "workflow", "prefPane", "arbitrary", "Contents", "Info.plist", "dot", "slash"],
)
async def test_launch_only_accepts_canonical_plain_folders(kind: str, tmp_path: Path) -> None:
    path = tmp_path / "bpnav-fix-folder"
    if kind == "file":
        path.write_text("not a folder")
    else:
        if kind not in {"Contents", "Info.plist", "dot", "slash"}:
            path = path.with_suffix("." + kind)
        path.mkdir()
        if kind == "Contents":
            (path / kind).mkdir()
        elif kind == "Info.plist":
            (path / kind).write_text("bundle")
    raw = str(path) + ("/." if kind == "dot" else "/" if kind == "slash" else "")
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError) as err:
        await capability_tool(session, "launch_app", {"name": "Test", "urls": [raw]})
    assert err.value.code == "bad_path"
    assert err.value.message == "only plain folders can be opened; files and bundles are refused"
    assert not driver.calls
    await session.close()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        *[
            ("name", s)
            for s in ["/Applications/Test.app", "x/y", "x:y", "-Test", ".Test", "x\x00", "x\x85", "x\u200b", "x\u202e"]
        ],
        *[("bundleId", s) for s in [".test", "-test", "x/y", "x:y", "x y", "x\n", "é.test", "a" * 256]],
    ],
)
async def test_launch_identity_validation_precedes_driver(key: str, value: str, tmp_path: Path) -> None:
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError) as err:
        await capability_tool(session, "launch_app", {key: value})
    assert err.value.code == "bad_arguments"
    assert not driver.calls
    await session.close()


@pytest.mark.parametrize(
    "url",
    [
        "-https://example.test",
        "--args",
        "http://u@example.test",
        "https://u:p@example.test",
        "https://example.test/\u200b",
        "https://example.test/\u202e",
    ],
)
async def test_launch_refuses_options_userinfo_and_format_characters(url: str, tmp_path: Path) -> None:
    driver = CapabilityDriver()
    session = await session_for(driver, tmp_path)
    with pytest.raises(ToolError) as err:
        await capability_tool(session, "launch_app", {"name": "Test", "urls": [url]})
    assert err.value.code == "bad_arguments"
    assert not driver.calls
    await session.close()


@pytest.mark.parametrize(
    "url",
    [
        "about:config",
        "about:",
        "about:blank?x",
        "about:blank#x",
        "about://blank",
        "http://u:p@example.test",
        "https://@example.test",
        "https://example.test/\u200b",
        "https://example.test/\u202e",
        "https://example.test:invalid/",
    ],
)
async def test_browser_refuses_nonblank_about_userinfo_and_format_characters(url: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    with pytest.raises(ToolError) as err:
        await capability_tool(session, "browser_navigate", {**BROWSER, "url": url})
    assert err.value.code == "bad_arguments"
    assert "browser_navigate" not in driver.tools
    await session.close()


@pytest.mark.parametrize(
    ("requested", "observed", "status"),
    [
        ("HTTP://Example.test", "http://example.test/", "done"),
        ("HTTPS://Example.test:443?q=A#B", "https://example.test/?q=A#B", "done"),
        ("http://Example.test:80", "http://example.test/", "done"),
        ("ABOUT:BLANK", "about:blank", "done"),
        ("http://example.test:81", "http://example.test/", "unverified"),
        ("https://example.test/?q=A#B", "https://example.test/?q=a#B", "unverified"),
        ("https://example.test/?q=A#B", "https://example.test/?q=A#b", "unverified"),
    ],
)
async def test_navigation_compares_normalized_urls(requested: str, observed: str, status: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.navigate_result = {}
    driver.page_url = observed
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    result = await capability_tool(session, "browser_navigate", {**BROWSER, "url": requested})
    assert isinstance(result, dict)
    assert result["status"] == status
    assert result["url"] == requested
    assert result["finalUrl"] == observed
    await session.close()


@pytest.mark.parametrize("length", [8192, 8193])
async def test_prompt_limit_counts_utf16_units(length: int, tmp_path: Path) -> None:
    driver = BrowserDriver()
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    prompt = "😀" * 4096 + ("x" if length == 8193 else "")
    args: JsonObject = {**BROWSER, "action": "accept", "dialogId": "d1", "allowDestructive": True, "promptText": prompt}
    if length > 8192:
        with pytest.raises(ToolError) as err:
            await capability_tool(session, "browser_dialog", args)
        assert err.value.code == "bad_arguments"
        assert "browser_dialog" not in driver.tools
    else:
        await capability_tool(session, "browser_dialog", args)
        assert driver.calls[-1][1]["prompt_text"] == prompt
    await session.close()


@pytest.mark.parametrize("minimal", [False, True])
async def test_dialog_inspect_whitelists_public_keys(minimal: bool, tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.dialog_result = {"status": "ok", "unknown": "secret", "target_id": "private"}
    expected: JsonObject = {"status": "done"}
    if not minimal:
        driver.dialog_result.update(
            present=True, dialog_id="d1", kind="prompt", message="untrusted", default_prompt="value"
        )
        expected.update(present=True, dialogId="d1", type="prompt", message="untrusted", defaultPrompt="value")
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    assert await capability_tool(session, "browser_dialog", {**BROWSER, "action": "inspect"}) == expected
    await session.close()


@pytest.mark.parametrize("tool", ["browser_navigate", "browser_dialog"])
async def test_successful_action_with_blocked_snapshot_returns_unverified(tool: str, tmp_path: Path) -> None:
    driver = BrowserDriver()
    driver.dialog_result = {"resolved": True}
    session = await session_for(driver, tmp_path)
    await capability_tool(session, "browser_state", TARGET)
    original = driver.response

    def response(name: str, args: dict[str, object]) -> ToolResult:
        if name == "get_browser_state":
            return ToolRefused(code="snapshot_blocked", message="blocked", data={}, text="", ms=1)
        return original(name, args)

    driver.on_call = response
    extra: JsonObject = {"url": URL} if tool == "browser_navigate" else {"action": "dismiss", "dialogId": "d1"}
    result = await capability_tool(session, tool, {**BROWSER, **extra})
    assert isinstance(result, dict)
    assert result["status"] == "unverified"
    assert result["driverCode"] == "snapshot_blocked"
    assert "dialog may be open" in result["reason"]
    assert "browser_dialog" in result["reason"]
    assert "inspect" in result["reason"]
    key = "url" if tool == "browser_navigate" else "dialogId"
    assert result[key] == extra[key]
    assert "snapshot" not in result
    assert not driver.sentinel.watching
    await session.close()


@pytest.mark.parametrize("filters", [{}, {"pid": 7}, {"app": "Test"}, {"onScreenOnly": True}])
async def test_window_titles_are_scoped_and_utf16_bounded(filters: JsonObject, tmp_path: Path) -> None:
    title = "x" + "😀" * 200
    driver = CapabilityDriver(
        lambda tool, args: ok({"windows": [{"pid": 7, "window_id": 10, "app_name": "Test", "title": title}]})
    )
    session = await session_for(driver, tmp_path)
    result = await capability_tool(session, "windows", filters)
    assert isinstance(result, dict)
    assert result["status"] == "done"
    window = result["windows"][0]
    if "app" in filters or "pid" in filters:
        assert window["title"] == "x" + "😀" * 127
        assert utf16_len(window["title"]) <= 256
    else:
        assert "title" not in window
    expected: JsonObject = {"pid": 7} if "pid" in filters else {}
    if "onScreenOnly" in filters:
        expected["on_screen_only"] = filters["onScreenOnly"]
    assert driver.calls == [("list_windows", expected)]
    await session.close()


@pytest.mark.parametrize("pid", [7, None])
@pytest.mark.parametrize("changed", [7, 100, None])
async def test_launch_watches_delayed_changes_to_any_front_app(
    pid: int | None, changed: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = CapabilityDriver(lambda tool, args: ok({"pid": pid, "self_activation_suppressed": True}))
    session = await session_for(driver, tmp_path)
    sleep = RecordingSleep()
    session._sleep = sleep
    samples = iter([99] * RESTORE_CHECKS + [changed])
    restores: list[int] = []

    async def front() -> int | None:
        return next(samples)

    async def restore(previous: int) -> bool:
        restores.append(previous)
        return True

    monkeypatch.setattr(session, "front", front)
    monkeypatch.setattr(session, "restore_front", restore)
    out = await capability_tool(session, "launch_app", {"name": "Test"})
    assert isinstance(out, dict)
    (seen,) = out["foreground"]
    assert isinstance(seen, dict)
    assert seen["pid"] == (pid if pid is not None else changed)
    assert seen["restored"] is True
    assert restores == [99]
    assert sleep.delays == [RESTORE_CHECK_INTERVAL_S] * (RESTORE_CHECKS - 1)
    await session.close()


async def test_launch_unknown_pid_stays_unverified_after_full_watch(tmp_path: Path) -> None:
    driver = CapabilityDriver(lambda tool, args: ok(window_data()))
    session = await session_for(driver, tmp_path)
    session._runner = FakeRunner()
    sleep = RecordingSleep()
    session._sleep = sleep
    result = await capability_tool(session, "launch_app", {"bundleId": "a" * 255})
    assert isinstance(result, dict)
    assert result["status"] == "unverified"
    assert sleep.delays == [RESTORE_CHECK_INTERVAL_S] * RESTORE_CHECKS
    await session.close()
