from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from beans_picker._json import JsonObject
from beans_picker._proc import DEFAULT_MAX_BYTES, Completed
from beans_picker.driver.app import (
    WINDOW_CHECK_INTERVAL_S,
    WINDOW_CHECKS,
    AppContext,
    AppTarget,
    ensure_app,
    pick_window,
    resolve_bundle,
    running_pid,
)
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult, Window, WindowBounds
from beans_picker.errors import AppLaunchError, DriverError, ProcessError
from tests.fakes import FakeDriver, RecordingSleep, fake_runner

FIND_CALC = ("lsappinfo", "find", "bundleid=com.apple.calculator")
ASN = "ASN:0x0-0x1d01d"
INFO = ("lsappinfo", "info", "-only", "pid", ASN)
CALC = AppTarget(bundle_id="com.apple.calculator")


class SeqRunner:
    def __init__(self, answers: Mapping[tuple[str, ...], Sequence[Completed | Exception]]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        check: bool = True,
        cwd: Path | None = None,
    ) -> Completed:
        key = tuple(argv)
        self.calls.append(key)
        queue = self.answers.get(key)
        if not queue:
            raise ProcessError(f"no answer for {key!r}")
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(out, Exception):
            raise out
        return out


def found(pid: int) -> dict[tuple[str, ...], list[Completed | Exception]]:
    return {FIND_CALC: [Completed(f'{ASN}:"Calculator"\n', 0)], INFO: [Completed(f'"pid"={pid}\n', 0)]}


NOT_FOUND: dict[tuple[str, ...], list[Completed | Exception]] = {FIND_CALC: [Completed("", 0)]}


def win(window_id: int = 10, pid: int = 42, **fields: object) -> JsonObject:
    out: JsonObject = {"window_id": window_id, "pid": pid, "title": "Main", "layer": 0, "z_index": 1}
    for k, v in fields.items():
        assert v is None or isinstance(v, str | int | bool)
        out[k] = v
    return out


def ok(data: JsonObject) -> ToolOk:
    return ToolOk(data=data, text="", ms=1)


def w(**fields: object) -> Window:
    base: dict[str, object] = {"window_id": 1, "pid": 1}
    base.update(fields)
    window_id, pid = base.pop("window_id"), base.pop("pid")
    assert isinstance(window_id, int)
    assert isinstance(pid, int)
    title = base.get("title")
    z = base.get("z_index")
    layer = base.get("layer")
    shown = base.get("is_on_screen")
    assert title is None or isinstance(title, str)
    assert z is None or isinstance(z, int)
    assert layer is None or isinstance(layer, int)
    assert shown is None or isinstance(shown, bool)
    return Window(window_id=window_id, pid=pid, title=title, z_index=z, layer=layer, is_on_screen=shown)


@pytest.mark.parametrize(
    ("app", "expected"),
    [
        (None, None),
        ("", None),
        ("Calculator", "com.apple.calculator"),
        ("TEXTEDIT", "com.apple.TextEdit"),
        ("com.apple.TextEdit", "com.apple.TextEdit"),
        ("org.my-app.Tool_2", "org.my-app.Tool_2"),
        ("com.foo", None),
        ("unknown", None),
        ("constructor", None),
        ("com..apple.x", None),
        ("com.apple.TextEdit\n", None),
        ("com.äpple.x", None),
    ],
)
def test_resolve_bundle(app: str | None, expected: str | None) -> None:
    assert resolve_bundle(app) == expected


def test_pick_window_prefers_titled_then_front_most() -> None:
    a = w(window_id=1, layer=1, title="A")
    untitled = w(window_id=2, title="", z_index=5)
    b = w(window_id=3, title="B", z_index=1)
    assert pick_window([a, untitled, b]) is b
    b_off = w(window_id=3, title="B", z_index=1, is_on_screen=False)
    assert pick_window([a, untitled, b_off]) is untitled


def test_pick_window_with_a_title_hint_ignores_screen_and_order() -> None:
    a = w(window_id=1, layer=1, title="A")
    b_off = w(window_id=3, title="Big B", z_index=1, is_on_screen=False)
    b2 = w(window_id=4, title="B2", z_index=9)
    assert pick_window([a, b_off, b2], "B") is b_off
    assert pick_window([a, b_off], "A") is None
    assert pick_window([a, b_off], "") is None


def test_pick_window_ranks_a_missing_z_index_as_minus_one() -> None:
    x = w(window_id=1, title="X")
    y = w(window_id=2, title="Y", z_index=0)
    assert pick_window([x, y]) is y
    y_low = w(window_id=2, title="Y", z_index=-2)
    assert pick_window([x, y_low]) is x
    same = w(window_id=3, title="Z")
    assert pick_window([x, same]) is x


def test_pick_window_counts_a_missing_layer_as_zero() -> None:
    assert pick_window([w(window_id=1, title="T")]) is not None
    assert pick_window([w(window_id=1, title="T", layer=3)]) is None
    assert pick_window([]) is None


async def test_running_pid_asks_launch_services_by_bundle() -> None:
    runner = SeqRunner(found(77))
    assert await running_pid(CALC, runner=runner) == 77
    assert runner.calls == [FIND_CALC, INFO]


@pytest.mark.parametrize(
    ("target", "query"),
    [
        (AppTarget(name="Calc"), "name=Calc"),
        (AppTarget(), "name="),
        (AppTarget(bundle_id="", name="Calc"), "bundleid="),
    ],
)
async def test_running_pid_query(target: AppTarget, query: str) -> None:
    runner = fake_runner({("lsappinfo", "find", query): Completed("", 0)})
    assert await running_pid(target, runner=runner) is None
    assert runner.calls == [("lsappinfo", "find", query)]


async def test_running_pid_is_none_on_failure_or_without_a_pid() -> None:
    assert await running_pid(CALC, runner=fake_runner({FIND_CALC: Completed("", 1)})) is None
    no_pid = SeqRunner({FIND_CALC: [Completed(f"{ASN} x", 0)], INFO: [Completed("{}", 0)]})
    assert await running_pid(CALC, runner=no_pid) is None
    failing_info = SeqRunner({FIND_CALC: [Completed(ASN, 0)], INFO: [ProcessError("boom")]})
    assert await running_pid(CALC, runner=failing_info) is None


async def test_a_running_app_with_a_window_is_not_launched() -> None:
    driver = FakeDriver(lambda tool, args: ok({"windows": [win(app_name="Calculator")]}))
    sleep = RecordingSleep()
    ctx = await ensure_app(driver, CALC, runner=SeqRunner(found(42)), sleep=sleep)
    assert ctx == AppContext(pid=42, window_id=10, app_name="Calculator", launched_by_us=False)
    assert driver.calls == [("list_windows", {"pid": 42})]
    assert sleep.delays == []


async def test_an_app_not_running_is_launched_in_the_background() -> None:
    driver = FakeDriver(lambda tool, args: ok({"pid": 7, "name": "Calc", "windows": [win(pid=7)]}))
    ctx = await ensure_app(driver, CALC, runner=SeqRunner(NOT_FOUND), sleep=RecordingSleep())
    assert ctx == AppContext(pid=7, window_id=10, app_name="Calc", launched_by_us=True)
    assert driver.calls == [("launch_app", {"bundle_id": "com.apple.calculator"})]


async def test_a_running_app_without_a_window_is_launched_but_not_owned() -> None:
    def answer(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "list_windows":
            return ok({"windows": [win(is_on_screen=False)]})
        return ok({"pid": 42, "windows": [win()]})

    driver = FakeDriver(answer)
    ctx = await ensure_app(driver, CALC, runner=SeqRunner(found(42)), sleep=RecordingSleep())
    assert ctx.launched_by_us is False
    assert driver.tools == ["list_windows", "launch_app"]


async def test_the_window_list_is_read_again_until_a_window_shows() -> None:
    lists = [[], [win(pid=7, title="Late", app_name="Late App")]]

    def answer(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "launch_app":
            return ok({"pid": 7})
        windows: list[JsonObject] = lists.pop(0)
        return ok({"windows": list(windows)})

    driver = FakeDriver(answer)
    sleep = RecordingSleep()
    ctx = await ensure_app(driver, AppTarget(name="Late"), runner=fake_runner({}), sleep=sleep)
    assert ctx == AppContext(pid=7, window_id=10, app_name="Late App", launched_by_us=True)
    assert driver.calls == [
        ("launch_app", {"name": "Late"}),
        ("list_windows", {"pid": 7}),
        ("list_windows", {"pid": 7}),
    ]
    assert sleep.delays == [WINDOW_CHECK_INTERVAL_S] * 2


async def test_no_window_after_the_fixed_number_of_checks() -> None:
    def answer(tool: str, args: dict[str, object]) -> ToolResult:
        return ok({"pid": 7}) if tool == "launch_app" else ok({"windows": []})

    driver = FakeDriver(answer)
    sleep = RecordingSleep()
    with pytest.raises(AppLaunchError, match=r"^no window for pid 7$"):
        await ensure_app(driver, CALC, runner=SeqRunner(NOT_FOUND), sleep=sleep)
    assert WINDOW_CHECKS == 34
    assert driver.tools == ["launch_app"] + ["list_windows"] * 34
    assert sleep.delays == [0.15] * 34


async def test_a_refused_launch_that_still_landed_goes_on() -> None:
    runner = SeqRunner(
        {
            FIND_CALC: [Completed("", 0), Completed(ASN, 0)],
            INFO: [Completed('"pid" = 9', 0)],
        }
    )

    def answer(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "launch_app":
            return ToolRefused(code="timeout", message="no reply", data={}, text="", ms=1)
        return ok({"windows": [win(pid=9)]})

    driver = FakeDriver(answer)
    ctx = await ensure_app(driver, CALC, runner=runner, sleep=RecordingSleep())
    assert ctx == AppContext(pid=9, window_id=10, app_name="", launched_by_us=True)
    assert driver.calls == [("launch_app", {"bundle_id": "com.apple.calculator"}), ("list_windows", {"pid": 9})]
    assert runner.calls == [FIND_CALC, FIND_CALC, INFO]


async def test_a_refused_launch_of_an_app_that_is_not_running() -> None:
    driver = FakeDriver(lambda tool, args: ToolRefused(code="x", message="not installed", data={}, text="", ms=1))
    with pytest.raises(AppLaunchError, match=r"^could not launch com\.apple\.calculator: not installed$"):
        await ensure_app(driver, CALC, runner=SeqRunner(NOT_FOUND), sleep=RecordingSleep())


async def test_a_launch_without_a_pid() -> None:
    driver = FakeDriver(lambda tool, args: ok({"pid": "7"}))
    with pytest.raises(AppLaunchError, match=r"^could not launch Foo: no pid$"):
        await ensure_app(driver, AppTarget(name="Foo"), runner=fake_runner({}), sleep=RecordingSleep())


async def test_a_launch_with_neither_bundle_nor_name() -> None:
    driver = FakeDriver(lambda tool, args: ok({}))
    with pytest.raises(AppLaunchError, match=r"^could not launch \(unknown app\): no pid$"):
        await ensure_app(driver, AppTarget(), runner=fake_runner({}), sleep=RecordingSleep())
    assert driver.calls == [("launch_app", {})]


async def test_an_empty_bundle_id_is_asked_for_but_launched_by_name() -> None:
    query = ("lsappinfo", "find", "bundleid=")
    runner = fake_runner({query: Completed("", 0)})
    driver = FakeDriver(lambda tool, args: ok({"pid": 3, "windows": [win(pid=3)]}))
    ctx = await ensure_app(driver, AppTarget(bundle_id="", name="Notes"), runner=runner, sleep=RecordingSleep())
    assert driver.calls == [("launch_app", {"name": "Notes"})]
    assert runner.calls == [query]
    assert ctx.app_name == "Notes"


async def test_a_refused_window_list_raises() -> None:
    driver = FakeDriver(lambda tool, args: ToolRefused(code="denied", message="no access", data={}, text="", ms=1))
    with pytest.raises(DriverError, match=r"^list_windows refused \(denied\): no access$"):
        await ensure_app(driver, CALC, runner=SeqRunner(found(42)), sleep=RecordingSleep())


async def test_a_window_list_without_windows_raises() -> None:
    driver = FakeDriver(lambda tool, args: ok({}))
    with pytest.raises(AppLaunchError, match="no window list"):
        await ensure_app(driver, CALC, runner=SeqRunner(found(42)), sleep=RecordingSleep())


def test_pick_window_passes_over_the_control_on_a_window_being_recorded() -> None:
    main = Window(
        window_id=1, pid=1, title="Calculator", z_index=5, bounds=WindowBounds(x=0, y=0, width=230, height=408)
    )
    control = Window(
        window_id=2, pid=1, title="Window", z_index=9, bounds=WindowBounds(x=16, y=16, width=66, height=20)
    )
    narrow = Window(window_id=3, pid=1, title="Strip", z_index=9, bounds=WindowBounds(x=0, y=0, width=40, height=400))
    assert pick_window([control, narrow, main]) is main
    assert pick_window([control]) is control


class Fronts:
    def __init__(self, *pids: int | None) -> None:
        self.pids = list(pids)

    async def __call__(self) -> int | None:
        return self.pids.pop(0)


class Restored:
    def __init__(self) -> None:
        self.pids: list[int] = []

    async def __call__(self, pid: int) -> bool:
        self.pids.append(pid)
        return True


def _launching() -> FakeDriver:
    return FakeDriver(lambda tool, args: ok({"pid": 7, "name": "Calc", "windows": [win(pid=7)]}))


async def test_an_app_that_took_the_front_while_starting_is_put_behind_the_app_that_was_there() -> None:
    restore = Restored()
    await ensure_app(
        _launching(), CALC, runner=SeqRunner(NOT_FOUND), sleep=RecordingSleep(), front=Fronts(3, 7), restore=restore
    )
    assert restore.pids == [3]


@pytest.mark.parametrize("fronts", [(3, 3), (None, 7), (7, 7)])
async def test_a_launch_that_left_the_front_alone_restores_nothing(fronts: tuple[int | None, int]) -> None:
    restore = Restored()
    await ensure_app(
        _launching(), CALC, runner=SeqRunner(NOT_FOUND), sleep=RecordingSleep(), front=Fronts(*fronts), restore=restore
    )
    assert restore.pids == []


async def test_a_running_app_is_never_restored_over() -> None:
    driver = FakeDriver(lambda tool, args: ok({"windows": [win(app_name="Calculator")]}))
    restore = Restored()
    front = Fronts()
    await ensure_app(driver, CALC, runner=SeqRunner(found(42)), sleep=RecordingSleep(), front=front, restore=restore)
    assert restore.pids == []
