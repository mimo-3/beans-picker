from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from mcp import MCPError
from mcp.types import CONNECTION_CLOSED

from cua_jev.driver import mcp as mcp_module
from cua_jev.driver.mcp import (
    FOREGROUND_TOOLS,
    READ_TOOLS,
    CuaDriver,
    CuaDriverOptions,
    _base36,
    _Connection,
    is_transport_error,
    steals_focus,
)
from cua_jev.driver.sentinel import ActivationSentinel
from cua_jev.driver.types import ToolOk, ToolRefused
from cua_jev.errors import DriverError, ForegroundViolation

FAKE_DRIVER = Path(__file__).with_name("fake_driver.py")


class FakeBin:
    def __init__(self, root: Path) -> None:
        self.state = root / "state"
        self.state.mkdir()
        self.path = root / "cua-driver"
        self.path.write_text(
            "#!/bin/sh\n"
            f'echo x >> "{self.state}/attempts"\n'
            f'if [ -e "{self.state}/refuse" ]; then exit 1; fi\n'
            f'exec "{sys.executable}" "{FAKE_DRIVER}" --state "{self.state}" "$@"\n',
            encoding="utf-8",
        )
        self.path.chmod(0o755)

    def calls(self) -> list[dict[str, object]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def launches(self) -> int:
        return len((self.state / "launches").read_text(encoding="utf-8").splitlines())

    def attempts(self) -> int:
        return len((self.state / "attempts").read_text(encoding="utf-8").splitlines())

    def refuse(self, on: bool) -> None:
        flag = self.state / "refuse"
        if on:
            flag.touch()
        else:
            flag.unlink()


@pytest.fixture
def fake_bin(tmp_path: Path) -> FakeBin:
    return FakeBin(tmp_path)


@pytest.fixture
async def driver(fake_bin: FakeBin) -> AsyncIterator[CuaDriver]:
    d = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    try:
        yield d
    finally:
        await d.close()


class Front:
    def __init__(self, pid: int | None) -> None:
        self.pid = pid

    async def __call__(self) -> int | None:
        return self.pid


def test_tool_sets() -> None:
    assert {
        "get_window_state",
        "list_windows",
        "get_accessibility_tree",
        "list_apps",
        "get_config",
        "get_screen_size",
    } == READ_TOOLS
    assert {"bring_to_front", "activate", "activate_app", "invoke_menu", "move_cursor"} == FOREGROUND_TOOLS


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("activate_app", {}, True),
        ("move_cursor", {"x": 1}, True),
        ("click", {"delivery_mode": "foreground"}, True),
        ("click", {"delivery_mode": "background"}, False),
        ("click", {}, False),
    ],
)
def test_steals_focus(tool: str, args: dict[str, object], expected: bool) -> None:
    assert steals_focus(tool, args) is expected


def test_is_transport_error() -> None:
    assert is_transport_error(RuntimeError("stream closed"))
    assert is_transport_error(OSError("write EPIPE"))
    assert is_transport_error(ValueError("Not Connected"))
    assert is_transport_error(RuntimeError("read ECONNRESET"))
    assert is_transport_error(BrokenPipeError())
    assert is_transport_error(ConnectionResetError())
    assert is_transport_error(anyio.ClosedResourceError())
    assert is_transport_error(anyio.BrokenResourceError())
    assert is_transport_error(anyio.EndOfStream())
    assert is_transport_error(MCPError(CONNECTION_CLOSED, "gone"))
    assert is_transport_error(ExceptionGroup("tasks failed", [ValueError("x"), anyio.BrokenResourceError()]))
    assert is_transport_error(ExceptionGroup("outer", [ExceptionGroup("inner", [RuntimeError("pipe closed")])]))
    wrapped = RuntimeError("request failed")
    wrapped.__cause__ = BrokenPipeError()
    assert is_transport_error(wrapped)
    assert not is_transport_error(MCPError(-32602, "bad params"))
    assert not is_transport_error(ValueError("bad input"))
    assert not is_transport_error(FileNotFoundError(2, "No such file or directory"))
    long_s = chr(0x17F)  # folds to "s" only under Unicode case rules
    assert not is_transport_error(RuntimeError(f"clo{long_s}ed"))


def test_base36() -> None:
    assert [_base36(n) for n in (0, 35, 36, 1_700_000_000_000)] == ["0", "z", "10", "loyw3v28"]


async def test_connect_names_a_unique_session_and_reads_session_tools(
    fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUA_DRIVER_BIN", str(fake_bin.path))
    d = await CuaDriver.connect()
    try:
        assert re.fullmatch(rf"cua-jev-{os.getpid()}-[0-9a-z]+", d.session)
        assert d.session_tools == {"list_windows", "get_window_state", "click", "end_session"}
        assert d.generation == 0
    finally:
        await d.close()
    custom = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path), session="bench"))
    try:
        assert custom.session.startswith(f"bench-{os.getpid()}-")
    finally:
        await custom.close()


async def test_the_session_is_added_only_for_tools_that_take_it(driver: CuaDriver, fake_bin: FakeBin) -> None:
    await driver.call("echo", {"pid": 1})
    await driver.call("click", {"pid": 1, "element_token": "t:1"})
    await driver.call("click", {"session": "mine"})
    await driver.call("list_windows")
    assert [(c["tool"], c["args"]) for c in fake_bin.calls()] == [
        ("echo", {"pid": 1}),
        ("click", {"session": driver.session, "pid": 1, "element_token": "t:1"}),
        ("click", {"session": "mine"}),
        ("list_windows", {"session": driver.session}),
    ]


async def test_output_schemas_are_not_enforced(driver: CuaDriver) -> None:
    r = await driver.call("get_window_state", {"pid": 1, "window_id": 2})
    assert isinstance(r, ToolOk)
    assert r.data == {"snapshot_id": "s1", "launch": 1}
    assert r.text == "state"
    assert r.ms >= 0


async def test_refusals_carry_the_drivers_code_and_message(driver: CuaDriver) -> None:
    r = await driver.call("refuse")
    assert isinstance(r, ToolRefused)
    assert (r.code, r.message, r.text) == ("stale_element_token", "token gone", "refused text")
    assert r.data == {"refusal": {"code": "stale_element_token", "message": "token gone"}, "extra": 1}
    plain = await driver.call("fail_plain")
    assert isinstance(plain, ToolRefused)
    assert (plain.code, plain.message, plain.text, plain.data) == ("tool_error", "plain failure", "plain failure", {})
    with pytest.raises(DriverError, match=r"^refuse refused \(stale_element_token\): token gone$"):
        await driver.must("refuse")


async def test_text_joins_every_content_item(driver: CuaDriver) -> None:
    r = await driver.call("mixed")
    assert isinstance(r, ToolOk)
    assert (r.text, r.data) == ("a\n\nb", {})
    assert await driver.must("list_windows") == {
        "windows": [{"window_id": 10, "pid": 42, "title": "Main", "layer": 0, "z_index": 1}],
        "launch": 1,
    }


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("bring_to_front", {}, "bring_to_front would bring an app to the front; cua-jev runs background-only"),
        ("activate", {"pid": 1}, "activate would bring an app to the front; cua-jev runs background-only"),
        (
            "click",
            {"delivery_mode": "foreground"},
            "click (delivery_mode foreground) would bring an app to the front; cua-jev runs background-only",
        ),
        (
            "activate_app",
            {"delivery_mode": "background"},
            "activate_app (delivery_mode background) would bring an app to the front; cua-jev runs background-only",
        ),
        (
            "invoke_menu",
            {"delivery_mode": ""},
            "invoke_menu would bring an app to the front; cua-jev runs background-only",
        ),
        (
            "move_cursor",
            {"delivery_mode": 0},
            "move_cursor would bring an app to the front; cua-jev runs background-only",
        ),
        (
            "move_cursor",
            {"delivery_mode": float("nan")},
            "move_cursor would bring an app to the front; cua-jev runs background-only",
        ),
        (
            "activate",
            {"delivery_mode": []},
            "activate (delivery_mode ) would bring an app to the front; cua-jev runs background-only",
        ),
    ],
)
async def test_foreground_calls_are_refused_locally(tool: str, args: dict[str, object], message: str) -> None:
    offline = CuaDriver(_Connection("cua-driver-never-started"), "cua-driver-never-started", "cua-jev")
    r = await offline.call(tool, args)
    assert r == ToolRefused(code="foreground_disallowed", message=message, data={}, text=message, ms=0)
    with pytest.raises(DriverError, match=rf"^{tool} refused \(foreground_disallowed\): "):
        await offline.must(tool, args)
    await offline.close()


async def test_a_foreground_call_never_reaches_the_driver(driver: CuaDriver, fake_bin: FakeBin) -> None:
    r = await driver.call("click", {"pid": 1, "delivery_mode": "foreground"})
    assert isinstance(r, ToolRefused)
    assert fake_bin.calls() == []


async def test_a_read_is_retried_once_after_the_child_dies(driver: CuaDriver, fake_bin: FakeBin) -> None:
    session = driver.session
    r = await driver.call("list_windows", {"die_in_launch": 1})
    assert isinstance(r, ToolOk)
    assert r.data["launch"] == 2
    assert driver.generation == 1
    assert driver.session == session
    assert "echo" in driver.session_tools
    await driver.call("echo")
    assert [(c["launch"], c["tool"], c["args"]) for c in fake_bin.calls()] == [
        (1, "list_windows", {"session": session, "die_in_launch": 1}),
        (2, "list_windows", {"session": session, "die_in_launch": 1}),
        (2, "echo", {"session": session}),
    ]


async def test_an_action_is_never_replayed(driver: CuaDriver, fake_bin: FakeBin) -> None:
    r = await driver.call("click", {"die_in_launch": 1})
    message = "click: connection to cua-driver was lost; reconnected but did not retry the action"
    assert r == ToolRefused(code="transport_lost", message=message, data={}, text=message, ms=0)
    assert [(c["launch"], c["tool"]) for c in fake_bin.calls()] == [(1, "click")]
    assert fake_bin.launches() == 2
    ok = await driver.call("click")
    assert isinstance(ok, ToolOk)
    assert ok.data["launch"] == 2


async def test_two_reads_failing_together_share_one_reconnect(driver: CuaDriver, fake_bin: FakeBin) -> None:
    args = {"die_in_launch": 1, "die_after": 0.3}
    first, second = await asyncio.gather(driver.call("list_windows", args), driver.call("list_windows", args))
    assert isinstance(first, ToolOk)
    assert isinstance(second, ToolOk)
    assert (first.data["launch"], second.data["launch"]) == (2, 2)
    assert fake_bin.launches() == 2
    assert driver.generation == 1
    assert [c["launch"] for c in fake_bin.calls()] == [1, 1, 2, 2]


async def test_a_failed_reconnect_reaches_every_waiting_call(driver: CuaDriver, fake_bin: FakeBin) -> None:
    fake_bin.refuse(True)
    args = {"die_in_launch": 1, "die_after": 0.3}
    results = await asyncio.gather(
        driver.call("list_windows", args), driver.call("list_windows", args), return_exceptions=True
    )
    assert all(isinstance(r, Exception) for r in results)
    assert fake_bin.attempts() == 2
    assert driver.generation == 0
    fake_bin.refuse(False)
    r = await driver.call("list_windows")
    assert isinstance(r, ToolOk)
    assert r.data["launch"] == 2
    assert driver.generation == 1


async def test_other_errors_are_not_reconnects(driver: CuaDriver, fake_bin: FakeBin) -> None:
    with pytest.raises(Exception, match="object"):
        await driver.call("echo", {"x": object()})
    assert driver.generation == 0
    assert fake_bin.attempts() == 1


async def test_a_reconnect_already_done_by_another_call_is_not_repeated(driver: CuaDriver, fake_bin: FakeBin) -> None:
    driver.generation = 1
    await driver._reconnect(0)
    assert fake_bin.attempts() == 1


async def test_a_failing_tool_list_fails_the_connect(fake_bin: FakeBin) -> None:
    (fake_bin.state / "fail_list").touch()
    before = asyncio.all_tasks()
    with pytest.raises(MCPError, match="tools are not available"):
        await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    assert asyncio.all_tasks() == before


async def test_close_ends_the_session(fake_bin: FakeBin) -> None:
    d = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    await d.close()
    assert fake_bin.calls() == [{"launch": 1, "tool": "end_session", "args": {"session": d.session}}]


async def test_close_after_the_child_died_returns(fake_bin: FakeBin) -> None:
    d = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    pid = (await d.must("whoami"))["pid"]
    assert isinstance(pid, int)
    os.kill(pid, signal.SIGKILL)
    await asyncio.wait_for(d.close(), timeout=20)
    assert fake_bin.launches() == 1


async def test_close_stops_the_child_when_end_session_gets_no_answer(
    fake_bin: FakeBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_module, "END_SESSION_WAIT_S", 0.2)
    (fake_bin.state / "hold_end_session").touch()
    d = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    pid = (await d.must("whoami"))["pid"]
    assert isinstance(pid, int)
    await asyncio.wait_for(d.close(), timeout=20)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_cancelled_close_still_stops_the_child(fake_bin: FakeBin) -> None:
    (fake_bin.state / "hold_end_session").touch()
    d = await CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path)))
    pid = (await d.must("whoami"))["pid"]
    assert isinstance(pid, int)
    closing = asyncio.create_task(d.close())
    await asyncio.sleep(0.2)
    closing.cancel()
    await asyncio.wait_for(asyncio.wait({closing}), timeout=20)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_missing_binary_fails_to_connect(tmp_path: Path) -> None:
    before = asyncio.all_tasks()
    with pytest.raises(OSError, match="No such file") as info:
        await CuaDriver.connect(CuaDriverOptions(bin=str(tmp_path / "missing")))
    assert not is_transport_error(info.value)
    assert asyncio.all_tasks() == before


async def test_a_cancelled_connect_leaves_nothing_running(fake_bin: FakeBin) -> None:
    before = asyncio.all_tasks()
    task = asyncio.create_task(CuaDriver.connect(CuaDriverOptions(bin=str(fake_bin.path))))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.all_tasks() == before


async def test_an_action_after_which_the_app_is_in_front_raises(driver: CuaDriver, fake_bin: FakeBin) -> None:
    front = Front(1)
    driver.sentinel = ActivationSentinel(front)
    await driver.sentinel.watch(5)
    assert isinstance(await driver.call("click"), ToolOk)
    front.pid = 5
    with pytest.raises(ForegroundViolation, match=r"\(pid 5\) came to the front during after click at ") as info:
        await driver.call("click", {"pid": 5})
    assert info.value.activation.during == "after click"
    assert len(fake_bin.calls()) == 2
    with pytest.raises(ForegroundViolation):
        await driver.call("click")
    with pytest.raises(ForegroundViolation):
        await driver.must("type_text")
    assert len(fake_bin.calls()) == 2
    assert isinstance(await driver.call("list_windows"), ToolOk)
    assert isinstance(await driver.call("end_session"), ToolOk)
    driver.sentinel.stop()
    assert isinstance(await driver.call("click"), ToolOk)


async def test_a_locally_refused_action_is_guarded_too(driver: CuaDriver) -> None:
    front = Front(1)
    driver.sentinel = ActivationSentinel(front)
    await driver.sentinel.watch(5)
    front.pid = 5
    with pytest.raises(ForegroundViolation) as info:
        await driver.call("activate_app")
    assert info.value.activation.during == "after activate_app"
    driver.sentinel.stop()


async def test_a_cancelled_action_is_not_sampled(driver: CuaDriver) -> None:
    front = Front(1)
    driver.sentinel = ActivationSentinel(front)
    await driver.sentinel.watch(5)
    task = asyncio.create_task(driver.call("click", {"hold": 5}))
    await asyncio.sleep(0.3)
    front.pid = 5
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    driver.sentinel.stop()


async def test_a_violation_replaces_a_transport_loss(driver: CuaDriver) -> None:
    front = Front(1)
    driver.sentinel = ActivationSentinel(front)
    await driver.sentinel.watch(5)
    front.pid = 5
    with pytest.raises(ForegroundViolation):
        await driver.call("click", {"die_in_launch": 1})
    driver.sentinel.stop()
