from __future__ import annotations

import asyncio

import pytest

from beans_picker._json import JsonObject, JsonValue
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult, Window
from beans_picker.errors import DriverError
from beans_picker.observe.signature import state_signature
from beans_picker.observe.snapshot import desktop_facts, observe
from beans_picker.observe.types import Snapshot
from tests.fakes import FakeDriver


def _window(window_id: int, pid: int = 1, **fields: object) -> Window:
    return Window(window_id=window_id, pid=pid, **fields)  # type: ignore[arg-type]


def test_the_front_ordinary_window_and_the_apps_other_windows() -> None:
    windows = [
        _window(10, title="Doc", z_index=5),
        _window(11, title="  Fonts ", z_index=7),
        _window(12, title="", z_index=6),
        _window(20, pid=2, title="Other app", z_index=4),
    ]
    facts = desktop_facts(windows, 1, 10)
    assert facts.frontmost is False
    assert facts.app_windows == ["Fonts", "(untitled window)"]
    assert facts.own == windows[0]
    assert desktop_facts(windows, 1, 11).frontmost is True


def test_overlays_hidden_windows_and_other_layers_are_left_out() -> None:
    windows = [
        _window(1, pid=9, app_name="Cua Driver", z_index=99),
        _window(2, pid=9, app_name="CUA DRIVER", z_index=98),
        _window(3, title="Hidden", is_on_screen=False, z_index=50),
        _window(4, title="Menu", layer=25, z_index=40),
        _window(5, title="Doc", z_index=10),
        _window(6, pid=9, app_name="Cua Driver Helper", z_index=5),
    ]
    facts = desktop_facts(windows, 1, 5)
    assert facts.frontmost is True
    assert facts.app_windows == []
    assert desktop_facts(windows, 9, 6).frontmost is False


def test_windows_without_z_index_sort_last_in_list_order() -> None:
    windows = [
        _window(1, title="A"),
        _window(2, title="B"),
        _window(3, title="C", z_index=0),
        _window(4, title="D", layer=0, is_on_screen=True),
    ]
    facts = desktop_facts(windows, 1, 99)
    assert facts.app_windows == ["C", "A", "B", "D"]
    assert facts.own is None
    assert desktop_facts([], 1, 1).frontmost is False


_STATE: JsonObject = {
    "snapshot_id": "s1",
    "tree_markdown": '- [0] AXWindow "Doc"\n  - [1] AXTextField "Name" = "Ada"\n  - [2] AXCheckBox "Bold"',
    "elements": [
        {"element_index": 0, "role": "AXWindow", "label": "Doc"},
        {"element_index": 1, "role": "AXTextField", "label": "Name", "value": "Ada", "parent_index": 0},
        {"element_index": 2, "role": "AXCheckBox", "label": "Bold", "parent_index": 0},
    ],
}


def _windows(*items: JsonValue) -> ToolResult:
    return ToolOk(data={"windows": list(items)}, text="", ms=1)


def _driver(state: JsonObject, windows: ToolResult) -> FakeDriver:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_window_state":
            return ToolOk(data=state, text="", ms=1)
        return windows

    return FakeDriver(on_call)


class _Exact:
    def __init__(self, fields: list[object] | None) -> None:
        self.fields = fields
        self.pids: list[int] = []

    async def __call__(self, pid: int) -> list[object] | None:
        self.pids.append(pid)
        return self.fields


async def _front(pid: int | None) -> int | None:
    return pid


async def _observe(driver: FakeDriver, exact: _Exact, front: int | None = 7) -> Snapshot:
    return await observe(driver, 7, 3, front_pid=lambda: _front(front), read_exact=exact)


async def test_observe_reads_state_and_windows_and_the_exact_text() -> None:
    windows = _windows(
        {"window_id": 3, "pid": 7, "app_name": "Notes", "title": "Doc", "z_index": 2},
        {"window_id": 4, "pid": 7, "title": "Fonts", "z_index": 1},
    )
    driver = _driver(_STATE, windows)
    exact = _Exact(
        [
            {"window": "Doc", "role": "AXTextField", "value": "Ada "},
            {"window": "Doc", "role": "AXCheckBox", "title": "Bold", "value": "1"},
        ]
    )
    snap = await _observe(driver, exact)
    assert driver.calls[:2] == [
        (
            "get_window_state",
            {"pid": 7, "window_id": 3, "include_screenshot": False, "max_elements": 1500, "max_depth": 25},
        ),
        ("list_windows", {}),
    ]
    assert exact.pids == [7]
    assert (snap.frontmost, snap.app_windows, snap.app_name, snap.window_title) == (True, ["Fonts"], "Notes", "Doc")
    name, bold = snap.nodes[1:]
    assert (name.raw_value, name.exact) == ("Ada ", True)
    assert (bold.value, bold.exact) == ("1", True)
    assert snap.ms >= 0
    assert snap.signature == state_signature(snap)


async def test_observe_keeps_the_names_get_window_state_gives() -> None:
    state = {**_STATE, "app_name": "Mine", "window_title": "Title"}
    windows = _windows({"window_id": 3, "pid": 7, "app_name": "Notes", "title": "Doc"})
    snap = await _observe(_driver(state, windows), _Exact([]))
    assert (snap.app_name, snap.window_title) == ("Mine", "Title")


async def test_the_window_is_not_frontmost_when_another_app_is_active() -> None:
    windows = _windows({"window_id": 3, "pid": 7, "z_index": 2})
    snap = await _observe(_driver(_STATE, windows), _Exact(None), front=8)
    assert snap.frontmost is False
    unknown_front = await _observe(_driver(_STATE, windows), _Exact(None), front=None)
    assert unknown_front.frontmost is True


async def test_desktop_facts_stay_unknown_when_the_window_list_fails() -> None:
    refused = ToolRefused(code="x", message="no", data={}, text="", ms=1)
    snap = await _observe(_driver(_STATE, refused), _Exact(None), front=8)
    assert (snap.frontmost, snap.app_windows) == (None, None)
    no_list = await _observe(_driver(_STATE, ToolOk(data={}, text="", ms=1)), _Exact(None))
    assert no_list.frontmost is None
    empty = await _observe(_driver(_STATE, _windows()), _Exact(None))
    assert (empty.frontmost, empty.app_windows) == (False, [])


async def test_windows_without_fields_or_toggles_skip_the_exact_reader() -> None:
    state: JsonObject = {"elements": [{"element_index": 0, "role": "AXButton", "label": "OK"}]}
    exact = _Exact([])
    await _observe(_driver(state, _windows()), exact)
    assert exact.pids == []


async def test_the_signature_includes_the_apps_other_windows() -> None:
    one = await _observe(_driver(_STATE, _windows({"window_id": 4, "pid": 7, "title": "A"})), _Exact(None))
    two = await _observe(_driver(_STATE, _windows({"window_id": 4, "pid": 7, "title": "B"})), _Exact(None))
    assert one.signature != two.signature


async def test_exact_text_rekeys_the_nodes() -> None:
    state: JsonObject = {
        "window_title": "Doc",
        "tree_markdown": '- [0] AXWindow "Doc"\n  - [1] AXTextField = "Search"',
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "Doc"},
            {"element_index": 1, "role": "AXTextField", "label": "Search", "value": "Search", "parent_index": 0},
        ],
    }
    without = await _observe(_driver(state, _windows()), _Exact(None))
    assert without.nodes[1].key == '["AXTextField",null,"",["AXWindow: Doc"]]'
    typed = await _observe(_driver(state, _windows()), _Exact([{"window": "Doc", "role": "AXTextField", "value": ""}]))
    assert typed.nodes[1].key == '["AXTextField",null,"Search",["AXWindow: Doc"]]'


async def test_observe_fails_when_the_window_state_cannot_be_read() -> None:
    reads: list[str] = []

    async def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        reads.append(tool)
        if tool == "get_window_state":
            return ToolRefused(code="no_window", message="gone", data={}, text="", ms=1)
        await asyncio.sleep(0)
        reads.append("list_windows done")
        return _windows()

    with pytest.raises(DriverError, match="no_window"):
        await observe(FakeDriver(on_call), 7, 3, front_pid=lambda: _front(None), read_exact=_Exact(None))
    assert reads == ["get_window_state", "list_windows", "list_windows done"]


async def test_observe_treats_a_failed_exact_text_read_as_not_read() -> None:
    async def broken(pid: int) -> list[object] | None:
        raise NotADirectoryError("cache is a file")

    driver = _driver(_STATE, _windows({"window_id": 3, "pid": 7, "title": "Doc"}))
    snap = await observe(driver, 7, 3, front_pid=lambda: _front(7), read_exact=broken)
    assert [n.role for n in snap.nodes] == ["AXWindow", "AXTextField", "AXCheckBox"]
