from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from cua_jev.driver.types import Frame, ToolOk, ToolRefused, ToolResult, WindowBounds
from cua_jev.observe import visual
from cua_jev.observe.png import Rgba, crop, decode_png
from cua_jev.observe.visual import (
    Shot,
    capture_window,
    pixel_change,
    pointer_mask,
    region_change,
    region_of,
    window_bounds,
)
from cua_jev.paths import Paths
from tests.fakes import FakeDriver
from tests.helpers import blank, encode_png, paint

# decode_png


def test_round_trips_an_rgb_image_through_the_row_filters() -> None:
    img = paint(blank(9, 7), 2, 1, 6, 5, 40)
    out = decode_png(encode_png(img))
    assert out is not None
    assert (out.width, out.height) == (9, 7)
    assert out.data == img.data


def test_returns_none_for_anything_that_is_not_a_png() -> None:
    assert decode_png(bytes([1, 2, 3])) is None


def test_crops_within_bounds() -> None:
    c = crop(paint(blank(10, 10), 5, 5, 10, 10, 0), 4, 4, 100, 100)
    assert (c.width, c.height) == (6, 6)
    assert c.data[0] == 255
    assert c.data[(1 * 6 + 1) * 4] == 0


# pixel_change


def test_a_caret_blink_is_not_an_effect_a_selection_highlight_is() -> None:
    a = blank(200, 60)
    caret = pixel_change(a, paint(blank(200, 60), 50, 10, 52, 40, 0))
    assert caret is not None
    assert (caret.caret_only, caret.significant) == (True, False)
    highlight = pixel_change(a, paint(blank(200, 60), 0, 10, 180, 40, 180))
    assert highlight is not None
    assert (highlight.caret_only, highlight.significant) == (False, True)


def test_ignores_the_pointers_box_and_refuses_different_sizes() -> None:
    a = blank(200, 60)
    pointer = paint(blank(200, 60), 100, 10, 130, 50, 0)
    change = pixel_change(a, pointer, [(95, 5, 135, 55)])
    assert change is not None
    assert (change.changed, change.significant) == (0, False)
    assert pixel_change(a, blank(100, 60)) is None


def test_pixel_change_counts_and_thresholds() -> None:
    a = blank(20, 20)
    # 50 pixels in 3 columns: a caret, however tall.
    narrow = pixel_change(a, paint(blank(20, 20), 0, 0, 3, 17, 0))
    assert narrow is not None
    assert (narrow.changed, narrow.columns, narrow.caret_only, narrow.significant) == (51, 3, True, False)
    wide = pixel_change(a, paint(blank(20, 20), 0, 0, 10, 4, 0))
    assert wide is not None
    assert (wide.changed, wide.columns, wide.caret_only, wide.significant) == (40, 10, False, True)
    few = pixel_change(a, paint(paint(blank(20, 20), 0, 0, 10, 3, 0), 0, 3, 9, 4, 0))
    assert few is not None
    assert (few.changed, few.significant) == (39, False)
    # A brightness step of exactly 24 is not a change; 25 is.
    assert pixel_change(a, blank(20, 20, 231)) == pixel_change(a, a)
    faint = pixel_change(a, blank(20, 20, 230))
    assert faint is not None
    assert faint.changed == 400
    assert pixel_change(blank(0, 5), blank(0, 5)) is None


def test_unchanged_regions_report_nothing() -> None:
    change = pixel_change(blank(5, 5), blank(5, 5))
    assert change is not None
    assert (change.changed, change.columns, change.caret_only, change.significant) == (0, 0, False, False)


# regions of shots


def _shot(
    img_w: int = 200, img_h: int = 120, *, x: float = 100, scale: float = 2, pointer: list[Frame] | None = None
) -> Shot:
    bounds = WindowBounds(x=x, y=50, width=img_w / scale, height=img_h / scale)
    return Shot(img=blank(img_w, img_h), bounds=bounds, scale=scale, pointer=pointer or [])


def test_region_of_maps_screen_points_to_pixels() -> None:
    shot = Shot(
        img=paint(blank(200, 120), 20, 20, 40, 30, 0),
        bounds=WindowBounds(x=100, y=50, width=100, height=60),
        scale=2,
        pointer=[],
    )
    region = region_of(shot, Frame(x=110, y=60, w=10, h=5))
    assert (region.width, region.height) == (20, 10)
    assert set(region.data[0::4]) == {0}


def test_pointer_mask_is_in_the_regions_pixels() -> None:
    p = Frame(x=112.25, y=61, w=40, h=48)
    shot = _shot(pointer=[p])
    frame = Frame(x=110, y=60, w=10, h=5)
    assert pointer_mask(frame, shot) == [(4, 2, 85, 98)]
    assert pointer_mask(frame, shot, _shot(pointer=[p, p])) == [(4, 2, 85, 98)] * 3
    # A frame left of the window starts the region at its edge.
    assert pointer_mask(Frame(x=90, y=40, w=10, h=5), shot) == [(24, 22, 105, 118)]


def test_region_change_needs_the_same_place_and_scale() -> None:
    frame = Frame(x=110, y=60, w=10, h=5)
    assert region_change(_shot(), _shot(x=101), frame) is None
    assert region_change(_shot(), _shot(img_w=300, scale=3), frame) is None
    change = region_change(_shot(), _shot(), frame)
    assert change is not None
    assert change.changed == 0


def test_region_change_leaves_out_both_pointers() -> None:
    frame = Frame(x=100, y=50, w=50, h=30)
    before = _shot(pointer=[Frame(x=100, y=50, w=10, h=10)])
    after = Shot(
        img=paint(blank(200, 120), 0, 0, 20, 20, 0),
        bounds=before.bounds,
        scale=2,
        pointer=[Frame(x=200, y=200, w=1, h=1)],
    )
    change = region_change(before, after, frame)
    assert change is not None
    assert change.changed == 0
    assert region_change(_shot(), after, frame) is not None


# capture_window


def _png_file(path: Path, width: int = 20, height: int = 10) -> None:
    path.write_bytes(encode_png(blank(width, height)))


def _capture_driver(
    *,
    state: ToolResult | None = None,
    cursor: ToolResult | None = None,
    windows: ToolResult | None = None,
    write_to: Path | None = None,
) -> FakeDriver:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_cursor_position":
            return cursor or ToolOk(data={"x": 10, "y": 20}, text="", ms=1)
        if tool == "list_windows":
            return windows or ToolRefused(code="x", message="no", data={}, text="", ms=1)
        out = args["screenshot_out_file"]
        assert isinstance(out, str)
        _png_file(write_to or Path(out))
        return state or ToolOk(data={"window_bounds": {"x": 5, "y": 6, "width": 10, "height": 5}}, text="", ms=1)

    return FakeDriver(on_call)


def _leftovers(paths: Paths) -> list[str]:
    return sorted(p.name for p in paths.shots.iterdir())


async def test_capture_window_reads_the_screenshot_and_the_pointer(paths: Paths) -> None:
    driver = _capture_driver()
    shot = await capture_window(driver, 7, 9, paths=paths)
    assert shot is not None
    assert (shot.img.width, shot.img.height, shot.scale) == (20, 10, 2)
    assert shot.bounds == WindowBounds(x=5, y=6, width=10, height=5)
    assert shot.pointer == [Frame(x=-4, y=2, w=40, h=48)] * 2
    assert driver.tools == ["get_cursor_position", "get_window_state", "get_cursor_position"]
    args = driver.calls[1][1]
    out = args.pop("screenshot_out_file")
    assert args == {"pid": 7, "window_id": 9, "max_elements": 1}
    assert isinstance(out, str)
    name = Path(out).name
    assert name.startswith(f"7-9-{os.getpid()}-")
    assert name.endswith(".png")
    assert Path(out).parent == paths.shots
    assert _leftovers(paths) == []


async def test_capture_window_takes_the_bounds_from_the_window_list(paths: Paths) -> None:
    windows = ToolOk(
        data={
            "windows": [
                {"window_id": 8, "pid": 7},
                {"window_id": 9, "pid": 7, "bounds": {"x": 0, "y": 0, "width": 20, "height": 10}},
            ]
        },
        text="",
        ms=1,
    )
    driver = _capture_driver(state=ToolOk(data={}, text="", ms=1), windows=windows)
    shot = await capture_window(driver, 7, 9, paths=paths)
    assert shot is not None
    assert shot.scale == 1
    assert driver.calls[3] == ("list_windows", {"pid": 7})


async def test_capture_window_removes_a_file_written_elsewhere(paths: Paths, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.png"
    state = ToolOk(
        data={"screenshot_file_path": str(elsewhere), "window_bounds": {"x": 0, "y": 0, "width": 20, "height": 10}},
        text="",
        ms=1,
    )
    shot = await capture_window(_capture_driver(state=state, write_to=elsewhere), 1, 2, paths=paths)
    assert shot is not None
    assert not elsewhere.exists()
    assert _leftovers(paths) == []


async def test_capture_window_answers_none_when_anything_is_missing(paths: Paths) -> None:
    refused = ToolRefused(code="no", message="no", data={}, text="", ms=1)
    no_cursor = ToolOk(data={"x": "1", "y": 2}, text="", ms=1)
    zero_width = ToolOk(data={"window_bounds": {"x": 0, "y": 0, "width": 0, "height": 10}}, text="", ms=1)
    no_bounds = ToolOk(data={}, text="", ms=1)
    bad_path = ToolOk(
        data={"screenshot_file_path": 5, "window_bounds": {"x": 0, "y": 0, "width": 9, "height": 9}}, text="", ms=1
    )
    assert await capture_window(_capture_driver(state=refused), 1, 2, paths=paths) is None
    assert await capture_window(_capture_driver(cursor=no_cursor), 1, 2, paths=paths) is None
    assert await capture_window(_capture_driver(cursor=refused), 1, 2, paths=paths) is None
    assert await capture_window(_capture_driver(state=zero_width), 1, 2, paths=paths) is None
    assert await capture_window(_capture_driver(state=no_bounds), 1, 2, paths=paths) is None
    assert await capture_window(_capture_driver(state=bad_path), 1, 2, paths=paths) is None
    assert _leftovers(paths) == []


async def test_capture_window_answers_none_for_an_unreadable_image(paths: Paths) -> None:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_window_state":
            out = args["screenshot_out_file"]
            assert isinstance(out, str)
            Path(out).write_bytes(b"not a png")
            return ToolOk(data={"window_bounds": {"x": 0, "y": 0, "width": 9, "height": 9}}, text="", ms=1)
        return ToolOk(data={"x": 1, "y": 1}, text="", ms=1)

    assert await capture_window(FakeDriver(on_call), 1, 2, paths=paths) is None
    assert _leftovers(paths) == []


async def test_capture_window_answers_none_when_the_driver_fails(paths: Paths) -> None:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        raise RuntimeError("stream closed")

    assert await capture_window(FakeDriver(on_call), 1, 2, paths=paths) is None
    # No file was written, so reading the requested path fails too.
    assert (
        await capture_window(
            FakeDriver(
                lambda t, a: ToolOk(
                    data={"x": 0, "y": 0, "window_bounds": {"x": 0, "y": 0, "width": 1, "height": 1}}, text="", ms=1
                )
            ),
            1,
            2,
            paths=paths,
        )
        is None
    )


async def test_window_bounds_reads_the_window_list() -> None:
    windows = ToolOk(
        data={"windows": [{"window_id": 3, "pid": 1, "bounds": {"x": 1, "y": 2, "width": 3, "height": 4}}]},
        text="",
        ms=1,
    )
    driver = FakeDriver(lambda tool, args: windows)
    assert await window_bounds(driver, 1, 3) == WindowBounds(x=1, y=2, width=3, height=4)
    assert await window_bounds(driver, 1, 4) is None
    assert await window_bounds(FakeDriver(lambda t, a: ToolOk(data={}, text="", ms=1)), 1, 3) is None
    no_bounds = ToolOk(data={"windows": [{"window_id": 3, "pid": 1}]}, text="", ms=1)
    assert await window_bounds(FakeDriver(lambda t, a: no_bounds), 1, 3) is None


async def test_capture_window_answers_none_when_the_shot_directory_cannot_be_made(paths: Paths) -> None:
    paths.shots.parent.mkdir(parents=True, exist_ok=True)
    paths.shots.write_bytes(b"a file where the directory should be")
    driver = _capture_driver()
    assert await capture_window(driver, 7, 9, paths=paths) is None
    assert driver.calls == []


async def test_capture_window_decodes_off_the_event_loop(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[bool] = []

    def decode(data: bytes) -> Rgba | None:
        threads.append(threading.current_thread() is threading.main_thread())
        return decode_png(data)

    monkeypatch.setattr(visual, "decode_png", decode)
    assert await capture_window(_capture_driver(), 7, 9, paths=paths) is not None
    assert threads == [False]
