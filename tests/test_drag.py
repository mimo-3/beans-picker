from __future__ import annotations

from dataclasses import replace

import pytest

from beans_picker._json import JsonObject, JsonValue
from beans_picker.candidates.build import build_candidates
from beans_picker.driver.app import WINDOW_CHECK_INTERVAL_S
from beans_picker.observe.types import Snapshot
from beans_picker.tools.act import _judge, act_tool
from beans_picker.tools.args import ActArgs, act_args, check_arguments
from tests.act_support import cand, frame, geometry_driver, node, snap, window
from tests.fakes import FakeClock, FakeDriver
from tests.tool_fakes import FakeSession, shown


def _window(source: str = "Source", target: str = "Target") -> Snapshot:
    return snap(
        [
            node(1, "AXRow", source, parent=0, frame=frame(150, 100)),
            node(2, "AXButton", target, parent=0, frame=frame(300, 200)),
        ]
    )


def _id(win: Snapshot, index: int) -> str:
    return next(c.id for c in build_candidates(win) if c.kind == "click" and c.target and c.target.index == index)


def _args(win: Snapshot, drag_to: JsonObject | None = None) -> ActArgs:
    return act_args(
        {
            "pid": win.pid,
            "instruction": "drag source to target",
            "candidateId": _id(win, 1),
            "dragTo": drag_to if drag_to is not None else {"candidateId": _id(win, 2)},
        }
    )


def _session(*snaps: Snapshot, others: tuple[JsonObject, ...] = ()) -> FakeSession:
    return FakeSession(snaps, driver=FakeDriver(geometry_driver(*others).call))


async def test_drag_onto_a_candidate_uses_both_centres_in_background_screenshot_pixels() -> None:
    before = _window()
    session = _session(before, shown(before, "Moved"))

    out = await act_tool(session, _args(before))

    assert out["status"] == "done"
    assert out["action"]["kind"] == "drag"
    assert out["action"]["route"] == ["drag"]
    assert session.calls == [
        ("list_windows", {}),
        ("get_window_state", {"pid": 1, "window_id": 1, "max_elements": 1}),
        (
            "drag",
            {
                "pid": 1,
                "window_id": 1,
                "from_x": 120,
                "from_y": 120,
                "to_x": 420,
                "to_y": 320,
                "delivery_mode": "background",
            },
        ),
    ]
    assert session.snapshots >= 2
    assert session.jev_calls == 0


async def test_drag_by_offset_adds_pixels_without_scaling_the_offset() -> None:
    before = _window()
    session = _session(before, shown(before, "Moved"))

    out = await act_tool(session, _args(before, {"dx": -50, "dy": 30}))

    assert out["status"] == "done"
    assert session.calls[-1] == (
        "drag",
        {
            "pid": 1,
            "window_id": 1,
            "from_x": 120,
            "from_y": 120,
            "to_x": 70,
            "to_y": 150,
            "delivery_mode": "background",
        },
    )


async def test_drag_in_then_uses_the_previous_steps_fresh_source_frame() -> None:
    before = _window()
    moved = snap([replace(before.nodes[0], frame=frame(200, 150)), before.nodes[1]], signature="moved")
    session = _session(before, moved, moved, moved, moved, shown(moved, "Dropped"))
    args = _args(before, {"dx": 100, "dy": 100})
    args["then"] = [_args(before)]

    out = await act_tool(session, args)

    assert out["status"] == "done"
    assert [s["status"] for s in out["steps"]] == ["done", "done"]
    drags = [args for tool, args in session.calls if tool == "drag"]
    assert [(d["from_x"], d["from_y"], d["to_x"], d["to_y"]) for d in drags] == [
        (120, 120, 220, 220),
        (220, 220, 420, 320),
    ]
    assert session.jev_calls == 0


@pytest.mark.parametrize(("source", "target"), [("Delete item", "Target"), ("Source", "Delete item")])
async def test_a_destructive_drag_endpoint_requires_confirmation(source: str, target: str) -> None:
    before = _window(source, target)
    session = _session(before)

    out = await act_tool(session, _args(before))

    assert out["status"] == "needs_confirmation"
    assert out["action"]["destructive"] is True
    assert session.calls == []


@pytest.mark.parametrize(("source", "target"), [("Delete item", "Target"), ("Source", "Delete item")])
async def test_allow_destructive_permits_either_drag_endpoint(source: str, target: str) -> None:
    before = _window(source, target)
    session = _session(before, shown(before, "Moved"))
    args = _args(before)
    args["allowDestructive"] = True

    out = await act_tool(session, args)

    assert out["status"] == "done"
    assert session.fake_driver.tools[-1] == "drag"


async def test_an_offset_onto_a_destructive_control_requires_confirmation() -> None:
    before = _window(target="Delete item")
    session = _session(before)

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200}))

    assert out["status"] == "needs_confirmation"
    assert "drag" not in session.fake_driver.tools


async def test_allow_destructive_permits_an_offset_onto_a_destructive_control() -> None:
    before = _window(target="Delete item")
    session = _session(before, shown(before, "Moved"))
    args = _args(before, {"dx": 300, "dy": 200})
    args["allowDestructive"] = True

    out = await act_tool(session, args)

    assert out["status"] == "done"
    assert session.fake_driver.tools[-1] == "drag"


@pytest.mark.parametrize("offset", [False, True])
async def test_drag_in_a_window_titled_shared_files_is_allowed(offset: bool) -> None:
    before = _window()
    before.window_title = "Shared files"
    before.nodes.insert(0, replace(node(0, "AXWindow", "Shared files", frame=frame(100, 50, 400, 300)), actions=[]))
    session = _session(before, shown(before, "Moved"))

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200} if offset else None))

    assert out["status"] == "done"
    assert session.fake_driver.tools.count("drag") == 1


@pytest.mark.parametrize("offset", [False, True])
@pytest.mark.parametrize("source", [False, True])
async def test_an_inert_group_titled_delete_report_at_either_drag_point_is_allowed(offset: bool, source: bool) -> None:
    before = _window()
    x, y = (150, 100) if source else (300, 200)
    before.nodes.append(replace(node(3, "AXGroup", "Delete report", frame=frame(x, y)), actions=[]))
    session = _session(before, shown(before, "Moved"))

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200} if offset else None))

    assert out["status"] == "done"
    assert session.fake_driver.tools.count("drag") == 1


@pytest.mark.parametrize("offset", [False, True])
@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("web", [False, True])
async def test_a_destructive_control_covering_either_drag_point_requires_confirmation(
    offset: bool, source: bool, web: bool
) -> None:
    before = _window()
    before.nodes[1] = replace(before.nodes[1], role="AXRow", key="AXRow:Target")
    x, y = (150, 100) if source else (300, 200)
    before.nodes.append(node(3, "AXButton", "Delete item", parent=0, frame=frame(x, y)))
    if web:
        before.nodes.insert(0, node(0, "AXWebArea", frame=frame(100, 50, 400, 300)))
    session = _session(before)

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200} if offset else None))

    assert out["status"] == "needs_confirmation"
    assert "drag" not in session.fake_driver.tools


@pytest.mark.parametrize("offset", [False, True])
@pytest.mark.parametrize("source", [False, True])
async def test_allow_destructive_permits_a_control_covering_either_drag_point(offset: bool, source: bool) -> None:
    before = _window()
    before.nodes[1] = replace(before.nodes[1], role="AXRow", key="AXRow:Target")
    x, y = (150, 100) if source else (300, 200)
    before.nodes.append(node(3, "AXButton", "Delete item", frame=frame(x, y)))
    session = _session(before, shown(before, "Moved"))
    args = _args(before, {"dx": 300, "dy": 200} if offset else None)
    args["allowDestructive"] = True

    out = await act_tool(session, args)

    assert out["status"] == "done"
    assert session.fake_driver.tools.count("drag") == 1


@pytest.mark.parametrize("index", [0, 1])
async def test_drag_refuses_an_endpoint_without_a_frame(index: int) -> None:
    before = _window()
    before.nodes[index].frame = None
    session = _session(before)

    out = await act_tool(session, _args(before))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


@pytest.mark.parametrize("index", [0, 1])
async def test_drag_refuses_a_candidate_outside_the_window(index: int) -> None:
    before = _window()
    before.nodes[index].frame = frame(600, 100)
    session = _session(before)

    out = await act_tool(session, _args(before))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


@pytest.mark.parametrize(("dx", "dy"), [(-121, 0), (681, 0), (0, -121), (0, 481)])
async def test_drag_refuses_an_offset_that_lands_outside_the_window(dx: int, dy: int) -> None:
    before = _window()
    session = _session(before)

    out = await act_tool(session, _args(before, {"dx": dx, "dy": dy}))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


@pytest.mark.parametrize(("x", "y"), [(140, 90), (290, 190)])
async def test_drag_refuses_either_endpoint_covered_by_an_app_window(x: int, y: int) -> None:
    before = _window()
    session = _session(before, others=(window(2, z=11, bounds=(x, y, 50, 50)),))

    out = await act_tool(session, _args(before))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


async def test_drag_refuses_an_offset_covered_by_an_app_window() -> None:
    before = _window()
    session = _session(before, others=(window(2, z=11, bounds=(290, 190, 50, 50)),))

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200}))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


@pytest.mark.parametrize(("x", "y"), [(140, 90), (290, 190)])
async def test_drag_refuses_either_endpoint_covered_by_a_web_control(x: int, y: int) -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWebArea", frame=frame(100, 50, 400, 300)))
    before.nodes.append(node(3, "AXButton", "Overlay", parent=0, frame=frame(x, y, 50, 50)))
    session = _session(before)

    out = await act_tool(session, _args(before))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


async def test_drag_refuses_an_offset_covered_by_a_web_control() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWebArea", frame=frame(100, 50, 400, 300)))
    before.nodes.append(node(3, "AXButton", "Overlay", parent=0, frame=frame(290, 190, 50, 50)))
    session = _session(before)

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200}))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


async def test_drag_by_offset_onto_an_uncovered_web_control_is_allowed() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWebArea", frame=frame(100, 50, 400, 300)))
    session = _session(before, shown(before, "Moved"))

    out = await act_tool(session, _args(before, {"dx": 300, "dy": 200}))

    assert out["status"] == "done"
    assert session.fake_driver.tools.count("drag") == 1


async def test_drag_refuses_an_offset_covered_at_the_target_edge_but_not_its_centre() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWebArea", frame=frame(100, 50, 400, 300)))
    before.nodes.append(node(3, "AXButton", "Overlay", parent=0, frame=frame(298, 198, 5, 5)))
    session = _session(before)

    out = await act_tool(session, _args(before, {"dx": 282, "dy": 182}))

    assert (out["status"], out["code"]) == ("failed", "not_visible")
    assert "drag" not in session.fake_driver.tools


@pytest.mark.parametrize("source", [True, False])
async def test_drag_refuses_a_stale_source_or_target_candidate(source: bool) -> None:
    before = _window()
    session = _session(before)
    args = _args(before, {"candidateId": "cstale"} if not source else None)
    if source:
        args["candidateId"] = "cstale"

    out = await act_tool(session, args)

    assert out["status"] == "not_found"
    assert session.calls == []


@pytest.mark.parametrize(
    ("extra", "code"),
    [({"text": "ignored"}, "text_not_used"), ({"modifiers": ["shift"]}, "modifiers_not_used")],
)
async def test_drag_refuses_arguments_it_cannot_apply(extra: JsonObject, code: str) -> None:
    before = _window()
    session = _session(before)
    args = act_args(
        {
            "instruction": "drag",
            "candidateId": _id(before, 1),
            "dragTo": {"dx": 20, "dy": 0},
            **extra,
        }
    )

    out = await act_tool(session, args)

    assert (out["status"], out["code"]) == ("failed", code)
    assert session.calls == []


async def test_drag_requires_a_source_candidate_instead_of_asking_jev() -> None:
    session = _session(_window())

    out = await act_tool(session, act_args({"instruction": "drag", "dragTo": {"dx": 20, "dy": 0}}))

    assert (out["status"], out["code"]) == ("failed", "candidate_required")
    assert session.calls == []
    assert session.jev_calls == 0


@pytest.mark.parametrize("source", [True, False])
async def test_a_window_key_candidate_cannot_be_a_drag_endpoint(source: bool) -> None:
    before = _window()
    key = next(c.id for c in build_candidates(before) if c.kind == "key" and c.keys == ["escape"])
    session = _session(before)
    args = _args(before, {"candidateId": key} if not source else None)
    if source:
        args["candidateId"] = key

    out = await act_tool(session, args)

    assert (out["status"], out["code"]) == ("failed", "drag_not_supported")
    assert session.calls == []


async def test_an_unchanged_row_drag_reports_no_effect() -> None:
    before = _window()
    session = _session(before)

    out = await act_tool(session, _args(before))

    assert out["status"] == "no_effect"
    assert out["verification"] == {"snapshots": 5}
    assert session.fake_driver.tools.count("drag") == 1


async def test_a_source_frame_change_verifies_a_drag_even_when_the_signature_is_unchanged() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWindow", frame=frame(100, 50, 400, 300)))
    after = snap([before.nodes[0], replace(before.nodes[1], frame=frame(300, 200)), before.nodes[2]])
    session = _session(before, after)

    out = await act_tool(session, _args(before))

    assert out["status"] == "done"
    assert session.fake_driver.tools.count("drag") == 1


async def test_a_frame_change_without_window_origin_does_not_verify_a_drag() -> None:
    before = _window()
    after = snap([replace(before.nodes[0], frame=frame(300, 200)), before.nodes[1]])
    session = _session(before, after)

    out = await act_tool(session, _args(before))

    assert out["status"] == "no_effect"
    assert session.fake_driver.tools.count("drag") == 1


async def test_moving_the_whole_window_does_not_verify_a_drag() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWindow", frame=frame(100, 50, 400, 300)))
    after = snap(
        [
            replace(before.nodes[0], frame=frame(150, 100, 400, 300)),
            replace(before.nodes[1], frame=frame(200, 150)),
            replace(before.nodes[2], frame=frame(350, 250)),
        ]
    )
    session = _session(before, after)

    out = await act_tool(session, _args(before))

    assert out["status"] == "no_effect"
    assert session.fake_driver.tools.count("drag") == 1


async def test_a_drag_that_moves_then_snaps_back_reports_no_effect() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWindow", frame=frame(100, 50, 400, 300)))
    moved = snap([before.nodes[0], replace(before.nodes[1], frame=frame(300, 200)), before.nodes[2]])
    session = _session(before, moved, before)

    out = await act_tool(session, _args(before))

    assert out["status"] == "no_effect"
    assert session.snapshots >= 3
    assert session.fake_driver.tools.count("drag") == 1


async def test_a_drag_that_snaps_back_after_two_equal_reads_reports_no_effect() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWindow", frame=frame(100, 50, 400, 300)))
    moved = snap([before.nodes[0], replace(before.nodes[1], frame=frame(300, 200)), before.nodes[2]])
    session = _session(before, moved, moved, before)

    out = await act_tool(session, _args(before))

    assert out["status"] == "no_effect"
    assert session.snapshots >= 4
    assert session.fake_driver.tools.count("drag") == 1


async def test_a_drag_waits_for_a_quiet_interval_before_accepting_its_effect() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWindow", frame=frame(100, 50, 400, 300)))
    moved = snap([before.nodes[0], replace(before.nodes[1], frame=frame(300, 200)), before.nodes[2]])
    clock = FakeClock()
    reads: list[float] = []

    async def snapshot() -> Snapshot:
        reads.append(clock())
        return moved

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    verdict, after, count = await _judge(cand("drag", before.nodes[1]), before, snapshot, clock=clock, sleep=sleep)

    assert verdict.effect == "ok"
    assert after is moved
    assert count == len(reads)
    assert reads[-1] - reads[0] >= 2 * WINDOW_CHECK_INTERVAL_S


async def test_a_continuously_moving_drag_stops_waiting_without_confirming_an_unsettled_effect() -> None:
    before = _window()
    before.nodes.insert(0, node(0, "AXWindow", frame=frame(100, 50, 400, 300)))
    clock = FakeClock()
    reads = 0

    async def snapshot() -> Snapshot:
        nonlocal reads
        reads += 1
        assert reads <= 6
        return snap([before.nodes[0], replace(before.nodes[1], frame=frame(160 + reads, 100)), before.nodes[2]])

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    verdict, _, count = await _judge(cand("drag", before.nodes[1]), before, snapshot, clock=clock, sleep=sleep)

    assert verdict.effect == "unverified"
    assert count == reads
    assert clock() <= 5 * WINDOW_CHECK_INTERVAL_S


async def test_enabling_submit_without_moving_the_source_verifies_a_drag() -> None:
    before = _window()
    submit = node(3, "AXButton", "Submit", frame=frame(400, 250))
    before.nodes.append(replace(submit, enabled=False))
    after = snap([*before.nodes[:2], submit])
    session = _session(before, after)

    out = await act_tool(session, _args(before))

    assert out["status"] == "done"
    assert session.fake_driver.tools.count("drag") == 1


@pytest.mark.parametrize(
    "drag_to",
    [
        {},
        {"candidateId": "c1", "dx": 1, "dy": 2},
        {"dx": 1},
        {"dy": 2},
        {"dx": True, "dy": 2},
        {"dx": 1, "dy": False},
        {"dx": 0.5, "dy": 2},
        {"dx": 1, "dy": 0.5},
        {"candidateId": 1},
        {"candidateId": "c1", "extra": 1},
        {"dx": 1, "dy": 2, "extra": 1},
        {"dx": -(2**53), "dy": 0},
        {"dx": 2**53, "dy": 0},
        None,
        "c1",
    ],
)
def test_drag_to_rejects_mixed_missing_or_invalid_destinations(drag_to: JsonValue) -> None:
    _, issues = check_arguments("act", {"instruction": "drag", "candidateId": "c0", "dragTo": drag_to})
    assert issues
    assert all(issue.path.startswith("dragTo") for issue in issues)


def test_drag_to_and_then_keep_candidate_ids_and_signed_pixel_offsets() -> None:
    args: JsonObject = {
        "instruction": "drag",
        "candidateId": "c0",
        "dragTo": {"candidateId": "c1"},
        "then": [{"instruction": "drag back", "candidateId": "c0", "dragTo": {"dx": -20, "dy": 0}}],
    }

    checked, issues = check_arguments("act", args)

    assert issues == []
    assert checked == args
    assert act_args(checked) == args


def test_an_invalid_drag_to_in_then_is_reported_at_that_step() -> None:
    _, issues = check_arguments(
        "act", {"instruction": "click", "then": [{"instruction": "drag", "dragTo": {"dx": True, "dy": 0}}]}
    )
    assert issues
    assert all(issue.path.startswith("then[0].dragTo") for issue in issues)
