from __future__ import annotations

from beans_picker.driver.types import (
    ActionResult,
    Element,
    Frame,
    ToolOk,
    ToolRefused,
    ToolResult,
    Window,
    WindowBounds,
    WindowState,
    action_result_of,
    as_obj,
    bounds_of,
    element_of,
    frame_of,
    get_bool,
    get_int,
    get_list,
    get_num,
    get_obj,
    get_str,
    get_str_list,
    window_of,
    window_state_of,
    windows_of,
)


def test_accessors_narrow_or_give_none() -> None:
    d: dict[str, object] = {"s": "x", "i": 3, "f": 2.0, "g": 2.5, "b": True, "l": [1, "a"], "o": {"k": 1}, "n": None}
    assert get_str(d, "s") == "x"
    assert get_str(d, "i") is None
    assert get_num(d, "i") == 3
    assert get_num(d, "g") == 2.5
    assert get_num(d, "b") is None
    assert get_int(d, "f") == 2
    assert isinstance(get_int(d, "f"), int)
    assert get_int(d, "g") is None
    assert get_int(d, "b") is None
    assert get_int({"x": float("inf")}, "x") is None
    assert get_bool(d, "b") is True
    assert get_bool(d, "i") is None
    assert get_list(d, "l") == [1, "a"]
    assert get_list(d, "o") is None
    assert get_obj(d, "o") == {"k": 1}
    assert get_obj(d, "l") is None
    assert get_str_list(d, "l") == ["a"]
    assert get_str_list(d, "missing") is None
    assert get_str(d, "missing") is None
    assert as_obj({1: "x"}) is None


def test_geometry_needs_every_coordinate() -> None:
    assert frame_of({"x": 1, "y": 2, "w": 3.5, "h": 4}) == Frame(x=1, y=2, w=3.5, h=4)
    assert frame_of({"x": 1, "y": 2, "w": 3}) is None
    assert frame_of("frame") is None
    assert bounds_of({"x": 0, "y": 0, "width": 800, "height": 600}) == WindowBounds(x=0, y=0, width=800, height=600)
    assert bounds_of({"x": 0, "y": 0, "width": "800", "height": 600}) is None
    assert bounds_of(None) is None


def test_windows_are_read_leniently() -> None:
    raw = {
        "windows": [
            {
                "window_id": 5,
                "pid": 9,
                "app_name": "Notes",
                "title": "T",
                "bounds": {"x": 1, "y": 2, "width": 3, "height": 4},
                "z_index": None,
                "is_on_screen": True,
                "layer": 0,
                "on_current_space": False,
            },
            {"window_id": 6},
            "junk",
        ]
    }
    assert windows_of(raw) == [
        Window(
            window_id=5,
            pid=9,
            app_name="Notes",
            title="T",
            bounds=WindowBounds(x=1, y=2, width=3, height=4),
            is_on_screen=True,
            layer=0,
            on_current_space=False,
        )
    ]
    assert windows_of({}) is None
    assert window_of({"window_id": 1, "pid": True}) is None


def test_window_state_and_elements() -> None:
    raw = {
        "snapshot_id": "s1",
        "app_name": "A",
        "window_title": "W",
        "window_bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
        "elements": [
            {
                "element_index": 0,
                "element_token": "s1:0",
                "role": "AXButton",
                "value": 3,
                "actions": ["AXPress", 7],
                "frame": {"x": 1, "y": 1, "w": 2, "h": 2},
                "parent_index": None,
                "depth": 1,
                "enabled": False,
            },
            {"element_index": 1},
            {"element_index": 2, "role": "AXGroup", "value": {"nested": True}},
        ],
        "tree_markdown": "- [0] AXButton",
        "screenshot_width": 20,
        "screenshot_scale": 2.0,
        "screenshot_file_path": "/shots/x.png",
    }
    state = window_state_of(raw)
    assert state == WindowState(
        snapshot_id="s1",
        app_name="A",
        window_title="W",
        window_bounds=WindowBounds(x=0, y=0, width=10, height=10),
        elements=[
            Element(
                element_index=0,
                role="AXButton",
                element_token="s1:0",  # noqa: S106 - an element token, not a secret
                value=3,
                actions=["AXPress"],
                frame=Frame(x=1, y=1, w=2, h=2),
                depth=1,
                enabled=False,
            ),
            Element(element_index=2, role="AXGroup"),
        ],
        tree_markdown="- [0] AXButton",
        screenshot_width=20,
        screenshot_scale=2.0,
        screenshot_file_path="/shots/x.png",
    )
    assert window_state_of({}) == WindowState()
    assert element_of(None) is None


def test_action_results_keep_the_drivers_words() -> None:
    raw = {
        "effect": "partially_confirmed",
        "route": "ax",
        "delivery": {"mode": "background"},
        "escalation": {"reason": "r", "target": "t"},
        "value_readback": [1],
    }
    assert action_result_of(raw) == ActionResult(
        effect="partially_confirmed",
        route="ax",
        delivery_mode="background",
        escalation_reason="r",
        escalation_target="t",
        value_readback=[1],
    )
    assert action_result_of({"delivery": "x"}) == ActionResult()


def test_results_narrow_on_ok() -> None:
    results: list[ToolResult] = [
        ToolOk(data={"a": 1}, text="t", ms=2),
        ToolRefused(code="c", message="m", data={}, text="t", ms=0),
    ]
    seen: list[object] = []
    for r in results:
        if r.ok:
            seen.append(r.data)
        else:
            seen.append(r.code)
    assert seen == [{"a": 1}, "c"]
