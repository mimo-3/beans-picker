from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest

from beans_picker._json import JsonObject, JsonValue
from beans_picker._proc import Completed
from beans_picker.driver.mcp import Driver
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult
from beans_picker.errors import BeansPickerError, JevUnavailable, ToolError
from beans_picker.paths import Paths
from beans_picker.tools.session import Session, Target
from tests.fakes import FakeDriver, FakeRunner, RecordingSleep
from tests.helpers import raw_fixture
from tests.tool_fakes import JevPicking


def window(window_id: int, pid: int, title: str = "Main") -> JsonObject:
    return {
        "window_id": window_id,
        "pid": pid,
        "app_name": "App",
        "title": title,
        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        "z_index": 1,
        "is_on_screen": True,
        "layer": 0,
    }


def windows_driver(*windows: JsonObject, launch: JsonObject | None = None) -> FakeDriver:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "list_windows":
            pid = args.get("pid")
            shown: list[JsonValue] = [w for w in windows if pid is None or w["pid"] == pid]
            return ToolOk(data={"windows": shown}, text="", ms=1)
        if tool == "launch_app" and launch is not None:
            return ToolOk(data=launch, text="", ms=1)
        return ToolRefused(code="unknown", message=f"no {tool}", data={}, text="", ms=1)

    return FakeDriver(on_call)


def session_with(driver: Driver, tmp_path: Path) -> Session:
    async def connect() -> Driver:
        return driver

    return Session(connect, paths=Paths(cache=tmp_path / "cache"), runner=FakeRunner(), sleep=RecordingSleep())


async def test_target_by_pid_picks_the_apps_window(tmp_path: Path) -> None:
    s = session_with(windows_driver(window(5, 42), window(6, 43)), tmp_path)
    assert await s.target({"pid": 42}) == Target(42, 5)
    assert await s.target({"pid": 42, "windowId": 5}) == Target(42, 5)
    with pytest.raises(ToolError, match=r"^pid 42 has no window 6$") as err:
        await s.target({"pid": 42, "windowId": 6})
    assert err.value.code == "window_not_found"
    with pytest.raises(ToolError, match=r"^pid 7 has no usable window$"):
        await s.target({"pid": 7})


async def test_target_by_window_id_alone(tmp_path: Path) -> None:
    s = session_with(windows_driver(window(5, 42), window(6, 43)), tmp_path)
    assert await s.target({"windowId": 6}) == Target(43, 6)
    with pytest.raises(ToolError, match=r"^no window with id 9$"):
        await s.target({"windowId": 9})


async def test_target_needs_app_pid_or_window_id(tmp_path: Path) -> None:
    s = session_with(windows_driver(), tmp_path)
    with pytest.raises(ToolError, match=r"^give app, pid or windowId$") as err:
        await s.target({})
    assert err.value.code == "bad_target"


async def test_target_by_app_launches_it_in_the_background(tmp_path: Path) -> None:
    launched: JsonObject = {"pid": 42, "name": "Calculator", "windows": [window(5, 42)]}
    driver = windows_driver(window(5, 42), window(8, 42, "Other"), launch=launched)
    s = session_with(driver, tmp_path)
    assert await s.target({"app": "Calculator"}) == Target(42, 5)
    assert driver.calls[0] == ("launch_app", {"bundle_id": "com.apple.calculator"})
    assert await s.target({"app": "Some App", "windowId": 8}) == Target(42, 8)
    assert ("launch_app", {"name": "Some App"}) in driver.calls


async def test_a_window_list_without_windows_is_an_internal_failure(tmp_path: Path) -> None:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        return ToolOk(data={}, text="", ms=1)

    s = session_with(FakeDriver(on_call), tmp_path)
    with pytest.raises(BeansPickerError, match="no window list"):
        await s.target({"pid": 1})
    with pytest.raises(ToolError, match="no window with id 3"):
        await s.target({"windowId": 3})


async def test_snapshot_observes_the_target_window(tmp_path: Path) -> None:
    raw = raw_fixture("calculator")

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_window_state":
            return ToolOk(data=raw, text="", ms=1)
        if tool == "list_windows":
            return ToolOk(data={"windows": [window(567, 1001, "\u8a08\u7b97\u6a5f")]}, text="", ms=1)
        return ToolRefused(code="unknown", message=tool, data={}, text="", ms=1)

    driver = FakeDriver(on_call)
    s = session_with(driver, tmp_path)
    snap = await s.snapshot(Target(1001, 567))
    assert (snap.pid, snap.window_id, snap.window_title) == (1001, 567, "\u8a08\u7b97\u6a5f")
    assert "get_window_state" in driver.tools
    assert s.menu_keys.table_for(1001) is None


async def test_exclusive_runs_calls_one_at_a_time_in_order(tmp_path: Path) -> None:
    s = session_with(FakeDriver(), tmp_path)
    events: list[str] = []

    async def body(name: str) -> str:
        events.append(f"start {name}")
        await asyncio.sleep(0.005)
        events.append(f"end {name}")
        return name

    results = await asyncio.gather(*(s.exclusive(functools.partial(body, n)) for n in "abc"))
    assert results == ["a", "b", "c"]
    assert events == ["start a", "end a", "start b", "end b", "start c", "end c"]


async def test_an_error_reaches_only_its_own_caller(tmp_path: Path) -> None:
    s = session_with(FakeDriver(), tmp_path)

    async def fail() -> str:
        raise ValueError("first")

    async def ok() -> str:
        return "second"

    first = asyncio.ensure_future(s.exclusive(fail))
    second = asyncio.ensure_future(s.exclusive(ok))
    with pytest.raises(ValueError, match="first"):
        await first
    assert await second == "second"


async def test_a_cancelled_callers_body_still_finishes_before_the_next_call(tmp_path: Path) -> None:
    s = session_with(FakeDriver(), tmp_path)
    release = asyncio.Event()
    events: list[str] = []

    async def slow() -> None:
        events.append("slow start")
        await release.wait()
        events.append("slow end")

    async def fast() -> None:
        events.append("fast")

    caller = asyncio.ensure_future(s.exclusive(slow))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    nxt = asyncio.ensure_future(s.exclusive(fast))
    await asyncio.sleep(0.01)
    assert events == ["slow start"]
    release.set()
    await nxt
    assert events == ["slow start", "slow end", "fast"]


async def test_a_call_cancelled_while_queued_never_runs(tmp_path: Path) -> None:
    s = session_with(FakeDriver(), tmp_path)
    release = asyncio.Event()
    events: list[str] = []

    async def slow() -> None:
        events.append("slow start")
        await release.wait()
        events.append("slow end")

    async def queued() -> None:
        events.append("queued")

    async def last() -> None:
        events.append("last")

    first = asyncio.ensure_future(s.exclusive(slow))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(s.exclusive(queued))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    third = asyncio.ensure_future(s.exclusive(last))
    release.set()
    await first
    await third
    assert events == ["slow start", "slow end", "last"]


async def test_the_driver_is_opened_once_and_retried_after_a_failure(tmp_path: Path) -> None:
    attempts = 0
    driver = FakeDriver()

    async def connect() -> Driver:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("cua-driver: could not start")
        return driver

    s = Session(connect, paths=Paths(cache=tmp_path))
    with pytest.raises(OSError, match="could not start"):
        await s.driver()
    assert await s.driver() is driver
    assert await s.driver() is driver
    assert attempts == 2


async def test_jev_is_created_on_first_use_and_a_failure_is_not_kept(tmp_path: Path) -> None:
    made: list[JevPicking] = []

    def factory() -> JevPicking:
        if not made:
            made.append(JevPicking(None))
            raise JevUnavailable("no key")
        made.append(JevPicking(None))
        return made[-1]

    s = Session(paths=Paths(cache=tmp_path), jev_factory=factory)
    with pytest.raises(JevUnavailable):
        s.jev()
    j = s.jev()
    assert s.jev() is j
    assert len(made) == 2


async def test_close_cancels_the_running_call_drops_queued_ones_and_closes_everything(tmp_path: Path) -> None:
    driver = FakeDriver()
    jev = JevPicking(None)

    async def connect() -> Driver:
        return driver

    s = Session(connect, paths=Paths(cache=tmp_path), jev_factory=lambda: jev)
    s.jev()
    await s.driver()
    started: list[str] = []

    async def forever() -> None:
        started.append("forever")
        await asyncio.Event().wait()

    async def queued() -> None:
        started.append("queued")

    running = asyncio.ensure_future(s.exclusive(forever))
    waiting = asyncio.ensure_future(s.exclusive(queued))
    await asyncio.sleep(0.01)
    await s.close()
    for caller in (running, waiting):
        with pytest.raises(asyncio.CancelledError):
            await caller
    assert started == ["forever"]
    assert driver.closed
    assert jev.closed
    assert not driver.sentinel.watching
    await s.close()
    with pytest.raises(BeansPickerError, match="server is shutting down"):
        await s.exclusive(queued)


async def test_close_during_driver_startup_leaves_nobody_waiting(tmp_path: Path) -> None:
    async def connect() -> Driver:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    s = Session(connect, paths=Paths(cache=tmp_path))
    caller = asyncio.ensure_future(s.exclusive(s.driver))
    await asyncio.sleep(0.01)
    await asyncio.wait_for(s.close(), 1)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 1)


async def test_close_after_a_failed_connect_is_quiet(tmp_path: Path) -> None:
    async def connect() -> Driver:
        raise OSError("no binary")

    s = Session(connect, paths=Paths(cache=tmp_path))
    with pytest.raises(OSError, match="no binary"):
        await s.driver()
    await s.close()


async def test_close_diagnostics_do_not_log_exception_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    jev = JevPicking(None)

    async def fail_close() -> None:
        raise RuntimeError("SENTINEL-private-request-body")

    monkeypatch.setattr(jev, "aclose", fail_close)
    s = Session(paths=Paths(cache=tmp_path), jev_factory=lambda: jev)
    s.jev()
    with caplog.at_level(logging.DEBUG, logger="beans_picker"):
        await s.close()
    assert "RuntimeError" in caplog.text
    assert "SENTINEL" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_close_cancels_menu_learning_in_progress(tmp_path: Path) -> None:
    started = asyncio.Event()
    stopped: list[bool] = []

    async def runner(argv: Sequence[str], /, **_: object) -> Completed:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            raise
        raise AssertionError("unreachable")

    s = Session(paths=Paths(cache=tmp_path), runner=runner)
    caller = asyncio.ensure_future(s.exclusive(lambda: s.menu_keys.learn(99)))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(s.close(), 1)
    assert stopped == [True]
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_the_sentinel_of_a_connected_driver_can_put_the_previous_app_back(tmp_path: Path) -> None:
    driver = FakeDriver()
    s = session_with(driver, tmp_path)
    assert driver.sentinel.restore is None
    assert await s.driver() is driver
    assert driver.sentinel.restore is not None
