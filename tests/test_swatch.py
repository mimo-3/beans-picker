from __future__ import annotations

from pathlib import Path

import pytest

from beans_picker._json import JsonObject, JsonValue
from beans_picker.candidates.build import build_candidates
from beans_picker.candidates.describe import describe
from beans_picker.driver.types import Frame, ToolOk, ToolRefused, ToolResult, WindowBounds
from beans_picker.observe.png import Rgba
from beans_picker.observe.snapshot import build_snapshot
from beans_picker.observe.swatch import colour_at, colour_name, name_swatches, swatches
from beans_picker.observe.types import Snapshot
from beans_picker.observe.visual import Shot
from beans_picker.tools.session import Target
from tests.fakes import FakeDriver
from tests.helpers import encode_png
from tests.test_session import session_with
from tests.test_state import el

PURPLE = (0x8B, 0x5C, 0xF6)
GREEN = (0x10, 0xB9, 0x81)


def _fill(img: Rgba, x0: int, y0: int, x1: int, y1: int, rgb: tuple[int, int, int]) -> Rgba:
    data = bytearray(img.data)
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * img.width + x) * 4
            data[i : i + 3] = bytes(rgb)
    return Rgba(img.width, img.height, bytes(data))


def _shot(img: Rgba) -> Shot:
    return Shot(img=img, bounds=WindowBounds(x=0, y=0, width=img.width, height=img.height), scale=1, pointer=[])


def _palette_raw(n: int, *, labels: bool = False, side: int = 20) -> JsonObject:
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Labels"),
        el(1, "AXWebArea", 0, 1, label="Labels"),
        el(2, "AXGroup", 1, 2),
    ]
    md = ['- [0] AXWindow "Labels"', '  - [1] AXWebArea "Labels"', "    - [2] AXGroup"]
    for i in range(n):
        frame: JsonValue = {"x": 10 + i * 30, "y": 10, "w": side, "h": side}
        extra: dict[str, JsonValue] = {"label": f"Swatch {i}"} if labels else {}
        elements.append(el(3 + i, "AXButton", 2, 3, frame=frame, actions=["press"], **extra))
        md.append(f"      - [{3 + i}] AXButton" + (f' "Swatch {i}"' if labels else ""))
    return {"elements": elements, "tree_markdown": "\n".join(md), "window_title": "Labels"}


def _palette_window(n: int, *, labels: bool = False, side: int = 20) -> Snapshot:
    return build_snapshot(_palette_raw(n, labels=labels, side=side), 1, 1)


def _painted(colours: list[tuple[int, int, int]]) -> Rgba:
    img = Rgba(200, 60, bytes([255]) * (200 * 60 * 4))
    for i, rgb in enumerate(colours):
        img = _fill(img, 10 + i * 30, 10, 30 + i * 30, 30, rgb)
    return img


@pytest.mark.parametrize(
    ("rgb", "want"),
    [
        ((0x8B, 0x5C, 0xF6), "purple"),
        ((0x10, 0xB9, 0x81), "green"),
        ((0x3B, 0x82, 0xF6), "blue"),
        ((0xF5, 0x9E, 0x0B), "orange"),
        ((0xEF, 0x44, 0x44), "red"),
        ((0xEC, 0x48, 0x99), "pink"),
        ((0xFA, 0xCC, 0x15), "yellow"),
        ((0x14, 0xB8, 0xA6), "teal"),
        ((0xB8, 0xB8, 0xB8), "gray"),
        ((0xFF, 0xFF, 0xFF), "white"),
        ((0x10, 0x10, 0x10), "black"),
    ],
)
def test_names_a_colour_plainly(rgb: tuple[int, int, int], want: str) -> None:
    assert colour_name(*rgb) == want


def test_finds_unnamed_square_buttons_in_a_row_of_three_or_more() -> None:
    assert len(swatches(_palette_window(3))) == 3
    assert swatches(_palette_window(2)) == []
    assert swatches(_palette_window(3, labels=True)) == []
    assert swatches(_palette_window(3, side=60)) == []


def test_reads_the_colour_a_swatch_shows_around_its_check_mark() -> None:
    img = _fill(_painted([PURPLE]), 18, 18, 22, 22, (255, 255, 255))
    assert colour_at(_shot(img), Frame(x=10, y=10, w=20, h=20)) == "purple #8B5CF6"


def test_leaves_a_button_unnamed_when_its_spots_disagree_or_show_no_colour() -> None:
    half = _fill(_painted([PURPLE]), 10, 20, 30, 30, GREEN)
    assert colour_at(_shot(half), Frame(x=10, y=10, w=20, h=20)) is None
    assert colour_at(_shot(_painted([])), Frame(x=10, y=10, w=20, h=20)) is None
    assert colour_at(_shot(_painted([])), Frame(x=500, y=500, w=20, h=20)) is None


def test_a_named_swatch_says_its_colour_in_its_candidate_but_keeps_its_key() -> None:
    snap = _palette_window(3)
    before = [c.key for c in build_candidates(snap)]
    name_swatches(swatches(snap), _shot(_painted([PURPLE, GREEN, PURPLE])))
    cands = [c for c in build_candidates(snap) if c.kind == "click"]
    assert [c.key for c in build_candidates(snap)] == before
    assert any("showing purple #8B5CF6" in c.summary for c in cands)
    assert any("showing green #10B981" in c.summary for c in cands)


async def test_a_snapshot_names_the_swatches_of_its_window(tmp_path: Path) -> None:
    png = encode_png(_painted([PURPLE, GREEN, PURPLE]))

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_cursor_position":
            return ToolOk(data={"x": 150, "y": 150}, text="", ms=1)
        if tool != "get_window_state":
            return ToolRefused(code="unknown", message=tool, data={}, text="", ms=1)
        out = args.get("screenshot_out_file")
        if isinstance(out, str):
            Path(out).write_bytes(png)
            return ToolOk(data={"window_bounds": {"x": 0, "y": 0, "width": 200, "height": 60}}, text="", ms=1)
        return ToolOk(data=_palette_raw(3), text="", ms=1)

    driver = FakeDriver(on_call)
    snap = await session_with(driver, tmp_path).snapshot(Target(1, 1))
    assert [n.colour for n in swatches(snap)] == ["purple #8B5CF6", "green #10B981", "purple #8B5CF6"]
    # The capture leaves cua-driver with one element cached; the last look at the window refills it.
    states = [args for tool, args in driver.calls if tool == "get_window_state"]
    assert [("screenshot_out_file" in a) for a in states] == [False, True, False]


async def test_a_window_without_swatches_takes_no_screenshot(tmp_path: Path) -> None:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        assert "screenshot_out_file" not in args
        if tool != "get_window_state":
            return ToolRefused(code="unknown", message=tool, data={}, text="", ms=1)
        return ToolOk(data=_palette_raw(2), text="", ms=1)

    driver = FakeDriver(on_call)
    await session_with(driver, tmp_path).snapshot(Target(1, 1))
    assert "get_cursor_position" not in driver.tools


async def test_a_palette_that_moved_before_the_second_look_is_left_unnamed(tmp_path: Path) -> None:
    png = encode_png(_painted([PURPLE, GREEN, PURPLE]))
    looks = [_palette_raw(3), _palette_raw(3, side=22)]

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_cursor_position":
            return ToolOk(data={"x": 150, "y": 150}, text="", ms=1)
        if tool != "get_window_state":
            return ToolRefused(code="unknown", message=tool, data={}, text="", ms=1)
        out = args.get("screenshot_out_file")
        if isinstance(out, str):
            Path(out).write_bytes(png)
            return ToolOk(data={"window_bounds": {"x": 0, "y": 0, "width": 200, "height": 60}}, text="", ms=1)
        return ToolOk(data=looks.pop(0), text="", ms=1)

    snap = await session_with(FakeDriver(on_call), tmp_path).snapshot(Target(1, 1))
    assert [n.colour for n in swatches(snap)] == [None, None, None]


def test_jev_is_told_the_colour_a_swatch_shows() -> None:
    snap = _palette_window(3)
    name_swatches(swatches(snap), _shot(_painted([PURPLE, GREEN, PURPLE])))
    told = [describe(c, snap) for c in build_candidates(snap) if c.kind == "click"]
    assert [d.get("shows_colour") for d in told] == ["purple #8B5CF6", "green #10B981", "purple #8B5CF6"]
