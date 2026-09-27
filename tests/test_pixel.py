from __future__ import annotations

import pytest

from beans_picker._json import JsonObject
from beans_picker.act.pixel import GEOMETRY_TTL_S, PixelMapper, Point, window_shape
from beans_picker.observe.types import Modal
from tests.act_support import frame, geometry_driver, node, ok, refused, snap, window, windows_data
from tests.fakes import FakeClock

WIN = snap(title="Doc")


async def test_maps_the_centre_to_a_screenshot_pixel_rounding_half_up() -> None:
    driver = geometry_driver()
    px = PixelMapper(driver)
    # Centre (160, 110) is (60, 60) points into the window, 2 px per point.
    assert await px.point(WIN, node(1, "AXButton", frame=frame(150, 100))) == Point(120, 120)
    # Centre (160.25, 110.25): 120.5 px rounds up.
    assert await px.point(WIN, node(2, "AXButton", frame=frame(150.25, 100.25))) == Point(121, 121)
    # A centre 0.25 points in from the left edge: 0.5 px rounds up to 1.
    assert await px.point(WIN, node(3, "AXButton", frame=frame(90.25, 50, 20, 20))) == Point(1, 20)
    assert driver.calls == [
        ("list_windows", {}),
        ("get_window_state", {"pid": 1, "window_id": 1, "max_elements": 1}),
    ]


async def test_needs_a_frame_larger_than_one_point() -> None:
    driver = geometry_driver()
    px = PixelMapper(driver)
    assert await px.point(WIN, node(1, "AXButton")) is None
    assert await px.point(WIN, node(1, "AXButton", frame=frame(150, 100, 1, 20))) is None
    assert await px.point(WIN, node(1, "AXButton", frame=frame(150, 100, 20, 1))) is None
    assert driver.calls == []


async def test_scale_comes_from_the_bounds_reported_with_the_screenshot() -> None:
    driver = geometry_driver(window_bounds={"x": 100, "y": 50, "width": 200, "height": 150})
    assert await PixelMapper(driver).point(WIN, node(1, "AXButton", frame=frame(150, 100))) == Point(240, 240)


async def test_a_point_outside_the_window_is_refused() -> None:
    px = PixelMapper(geometry_driver())
    assert await px.point(WIN, node(1, "AXButton", frame=frame(600, 100))) is None
    assert await px.point(WIN, node(2, "AXButton", frame=frame(490, 340))) == Point(800, 600)


@pytest.mark.parametrize(
    ("other", "covered"),
    [
        (window(2, z=11, bounds=(140, 90, 50, 50)), True),  # our app, above us
        (window(2, z=9, bounds=(140, 90, 50, 50)), False),  # our app, below us
        (window(2, z=None, bounds=(140, 90, 50, 50)), False),  # no z-index ranks as -1
        (window(2, z=1, layer=3, bounds=(140, 90, 50, 50)), True),  # a higher level (a pop-up list)
        (window(2, z=11, bounds=(300, 300, 50, 50)), False),  # not over the point
        (window(2, z=11, on_screen=False, bounds=(140, 90, 50, 50)), False),
        (window(2, z=11, bounds=None), False),
        (window(2, pid=2, z=99, bounds=(140, 90, 50, 50)), False),  # another app never receives the click
        (window(2, z=99, layer=-1, bounds=(140, 90, 50, 50)), False),  # below the normal level
    ],
)
async def test_only_our_apps_windows_above_the_point_block_it(other: JsonObject, covered: bool) -> None:
    got = await PixelMapper(geometry_driver(other)).point(WIN, node(1, "AXButton", frame=frame(150, 100)))
    assert got == (None if covered else Point(120, 120))


@pytest.mark.parametrize(
    "me",
    [
        window(1, on_screen=False),
        window(1, z=None),
        window(1, bounds=None),
        window(7),  # our window is not listed
    ],
)
async def test_no_pixel_path_for_a_window_that_is_not_measurable(me: JsonObject) -> None:
    assert await PixelMapper(geometry_driver(me=me)).point(WIN, node(1, "AXButton", frame=frame(150, 100))) is None


async def test_no_pixel_path_when_cua_driver_cannot_measure() -> None:
    target = node(1, "AXButton", frame=frame(150, 100))
    listing = geometry_driver(scripts={"list_windows": [refused("internal", "no")]})
    assert await PixelMapper(listing).point(WIN, target) is None
    assert listing.tools == ["list_windows"]
    shot = geometry_driver(scripts={"get_window_state": [refused("internal", "no")]})
    assert await PixelMapper(shot).point(WIN, target) is None
    no_width = geometry_driver(screenshot_width=0)
    assert await PixelMapper(no_width).point(WIN, target) is None
    raising = geometry_driver(scripts={"list_windows": [RuntimeError("boom")]})
    assert await PixelMapper(raising).point(WIN, target) is None
    malformed = geometry_driver(scripts={"list_windows": [ok({"windows": "none"})]})
    assert await PixelMapper(malformed).point(WIN, target) is None
    bad_bounds = geometry_driver(window_bounds={"x": 100, "y": 50})
    assert await PixelMapper(bad_bounds).point(WIN, target) is None


async def test_malformed_window_entries_are_skipped() -> None:
    junk: JsonObject = {"window_id": True, "pid": 1, "is_on_screen": True, "bounds": {"x": 0, "y": 0, "width": 9}}
    listed = ok({"windows": ["junk", junk, window(1)]})
    driver = geometry_driver(scripts={"list_windows": [listed]})
    assert await PixelMapper(driver).point(WIN, node(1, "AXButton", frame=frame(150, 100))) == Point(120, 120)
    half = window(1)
    half["bounds"] = {"x": 100, "y": 50, "width": 400}
    assert await PixelMapper(geometry_driver(me=half)).point(WIN, node(1, "AXButton", frame=frame(150, 100))) is None


async def test_geometry_is_reused_while_the_window_looks_the_same_failures_included() -> None:
    clock = FakeClock()
    driver = geometry_driver(me=window(1, on_screen=False))
    px = PixelMapper(driver, clock=clock)
    target = node(1, "AXButton", frame=frame(150, 100))
    assert await px.point(WIN, target) is None
    clock.advance(GEOMETRY_TTL_S / 2)
    assert await px.point(WIN, target) is None
    assert driver.tools == ["list_windows"]
    clock.advance(GEOMETRY_TTL_S / 2)
    await px.point(WIN, target)
    assert driver.tools == ["list_windows", "list_windows"]
    await px.point(snap(title="Doc", modal=Modal(role="AXSheet", label="Save", index=9)), target)
    assert driver.tools == ["list_windows"] * 3


async def test_a_new_measurement_replaces_a_failed_one() -> None:
    clock = FakeClock()
    driver = geometry_driver(
        scripts={"list_windows": [refused("internal", "busy"), ok(windows_data(window(1)))]},
    )
    px = PixelMapper(driver, clock=clock)
    target = node(1, "AXButton", frame=frame(150, 100))
    assert await px.point(WIN, target) is None
    clock.advance(GEOMETRY_TTL_S)
    assert await px.point(WIN, target) == Point(120, 120)


def test_window_shape_names_title_modal_and_other_windows() -> None:
    assert window_shape(snap(title="Doc")) == "Doc\x01-"
    shaped = snap(title="Doc", modal=Modal(role="AXSheet", label="Save", index=3), app_windows=["A", "B"])
    assert window_shape(shaped) == "Doc\x01AXSheet:Save\x01A\x01B"
