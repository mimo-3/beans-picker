from __future__ import annotations

from pathlib import Path

from beans_picker._json import JsonValue
from beans_picker.candidates.build import BuildOptions, build_candidates
from beans_picker.driver.types import Activation, ToolOk, ToolRefused, ToolResult
from beans_picker.errors import ForegroundViolation
from beans_picker.observe.png import Rgba
from beans_picker.observe.snapshot import build_snapshot
from beans_picker.observe.types import Snapshot
from beans_picker.tools.act import act_tool
from tests.fakes import FakeDriver
from tests.helpers import blank, encode_png, paint, snap_fixture
from tests.test_act import first, popup_window, row_window, text_window
from tests.test_state import el
from tests.tool_fakes import FakeSession, jev_picking, shown

CLOSE_ALL = "\u3059\u3079\u3066\u3092\u9589\u3058\u308b"


def _ok() -> ToolOk:
    return ToolOk(data={"effect": "confirmed"}, text="", ms=1)


async def test_a_foreground_violation_ends_the_step_without_a_route(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        raise ForegroundViolation(Activation(pid=calc.pid, during="click", at="10:00:00.000"))

    driver = FakeDriver(on_call)
    session = FakeSession([calc, shown(calc, "7")], jev_picking('"Seven"'), driver=driver, cache=tmp_path)
    out = await act_tool(session, {"app": "Calculator", "instruction": "press 7"})
    assert list(out) == ["status", "code", "window", "message", "action", "jev"]
    assert out["code"] == "foreground_violation"
    assert out["message"].startswith(f"foreground_violation: the app under test (pid {calc.pid}) came to the front")
    assert "route" not in out["action"]
    assert not driver.sentinel.watching


async def test_a_clear_pick_reports_the_pick_and_usage_last(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    session = FakeSession([calc, shown(calc, "7")], jev_picking('"Seven"'), cache=tmp_path)
    out = await act_tool(session, {"app": "Calculator", "instruction": "press 7"})
    assert list(out) == ["status", "window", "message", "action", "verification", "change", "pick", "jev"]
    assert out["pick"] == {"p": 0.95, "rule": "strict"}
    assert out["action"]["route"]
    assert out["change"]["texts_changed"] == ["7"]


async def test_a_candidate_id_step_reports_no_usage_and_never_opens_jev(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    seven = next(c for c in build_candidates(calc) if "Seven" in c.summary)
    session = FakeSession([calc, shown(calc, "7")], cache=tmp_path)
    out = await act_tool(session, {"pid": calc.pid, "instruction": "7", "candidateId": seven.id})
    assert out["status"] == "done"
    assert "jev" not in out
    assert "pick" not in out
    assert session.jev_calls == 0


async def test_then_stops_at_a_middle_ambiguous_step(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    seven = next(c for c in build_candidates(calc) if "Seven" in c.summary)
    after = shown(calc, "7")
    session = FakeSession([calc, after], jev_picking('"Seven"', '"Eight"'), cache=tmp_path)
    out = await act_tool(
        session,
        {
            "pid": calc.pid,
            "instruction": "7",
            "candidateId": seven.id,
            "then": [{"instruction": "a digit"}, {"instruction": "8", "candidateId": seven.id}],
        },
    )
    assert list(out) == ["status", "message", "window", "steps", "skipped"]
    assert out["status"] == "ambiguous"
    assert out["message"] == "stopped at step 2 of 3 (ambiguous); the rest were not run"
    assert out["skipped"] == 1
    assert [s["status"] for s in out["steps"]] == ["done", "ambiguous"]
    assert all("window" not in s for s in out["steps"])
    assert "jev" not in out["steps"][0]
    assert out["steps"][1]["jev"] == {"calls": 1, "inputTokens": 10, "ms": 1}
    assert len(session.calls) == 1


async def test_a_sequence_that_runs_through_has_no_skipped_count(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    seven = next(c for c in build_candidates(calc) if "Seven" in c.summary)
    snaps = [calc, shown(calc, "7"), shown(calc, "77")]
    session = FakeSession(snaps, cache=tmp_path)
    out = await act_tool(
        session, {"pid": calc.pid, "instruction": "7", "candidateId": seven.id, "then": [{"instruction": "7"}]}
    )
    assert out["status"] == "not_found"
    out = await act_tool(
        FakeSession(snaps, cache=tmp_path),
        {"pid": calc.pid, "instruction": "7", "candidateId": seven.id, "then": []},
    )
    assert "steps" not in out
    session = FakeSession(snaps, cache=tmp_path)
    out = await act_tool(
        session,
        {
            "pid": calc.pid,
            "instruction": "7",
            "candidateId": seven.id,
            "then": [{"instruction": "7", "candidateId": seven.id}],
        },
    )
    assert out["message"] == "ran all 2 steps"
    assert "skipped" not in out


async def test_a_failing_first_step_still_answers_as_a_sequence(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    session = FakeSession([calc], cache=tmp_path)
    out = await act_tool(
        session,
        {"pid": calc.pid, "instruction": "x", "candidateId": "cdeadbeef", "then": [{"instruction": "y"}] * 3},
    )
    assert (out["status"], out["skipped"]) == ("not_found", 3)
    assert out["message"] == "stopped at step 1 of 4 (not_found); the rest were not run"


async def test_pre_checks_text_not_used_and_foreground_required(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    cands = build_candidates(calc)
    seven = next(c for c in cands if "Seven" in c.summary)
    session = FakeSession([calc], cache=tmp_path)
    out = await act_tool(session, {"pid": calc.pid, "instruction": "7", "candidateId": seven.id, "text": "7"})
    assert list(out) == ["status", "code", "window", "message", "action"]
    assert out["code"] == "text_not_used"
    assert out["message"] == "click enters no text; leave `text` out or pick a text candidate"
    background_less = [c for c in cands if c.needs_foreground]
    if background_less:
        out = await act_tool(
            session,
            {"pid": calc.pid, "instruction": "m", "candidateId": background_less[0].id, "allowDestructive": True},
        )
        assert out["code"] == "foreground_required"
    assert session.calls == []


async def test_a_destructive_pick_by_jev_asks_first_and_carries_the_pick(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    session = FakeSession([calc], jev_picking(CLOSE_ALL), cache=tmp_path)
    out = await act_tool(session, {"pid": calc.pid, "instruction": "close all windows"})
    assert list(out) == ["status", "window", "message", "action", "pick", "jev"]
    assert out["status"] == "needs_confirmation"
    assert out["action"].get("destructive") is True
    assert session.calls == []


async def test_allow_destructive_passes_the_confirmation_and_meets_the_next_check(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    close_all = next(c for c in build_candidates(calc) if c.menu is not None and c.menu.path[-1] == CLOSE_ALL)
    assert close_all.destructive
    assert close_all.needs_foreground
    session = FakeSession([calc], cache=tmp_path)
    out = await act_tool(
        session, {"pid": calc.pid, "instruction": "close all", "candidateId": close_all.id, "allowDestructive": True}
    )
    assert list(out) == ["status", "code", "window", "message", "action"]
    assert out["code"] == "foreground_required"
    assert out["message"] == "this menu command has no keyboard shortcut, so it cannot run in the background"
    assert out["action"] == {
        "id": close_all.id,
        "kind": "menu",
        "does": close_all.summary,
        "destructive": True,
        "needsForeground": True,
    }
    assert session.calls == []


async def test_jev_not_found_lists_the_best_five_with_p(tmp_path: Path) -> None:
    win = popup_window("Small", False)
    session = FakeSession([win], jev_picking("no such control"), cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "fly"})
    assert list(out) == ["status", "window", "message", "candidates", "jev"]
    assert out["message"] == "Jev found no action for this instruction (p(none)=0.95)"
    assert len(out["candidates"]) == 5
    assert all("p" in c for c in out["candidates"])


async def test_a_text_with_nowhere_to_go_is_not_found(tmp_path: Path) -> None:
    session = FakeSession([row_window()], cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "type", "text": "x"})
    assert out == {
        "status": "not_found",
        "window": {"app": "", "pid": 1, "windowId": 1, "title": "Team drive"},
        "message": "no field, pop-up or keypad on this window can take text",
        "jev": {"calls": 1, "inputTokens": 10, "ms": 1},
    }


async def test_a_refusal_is_reported_with_cua_drivers_own_code(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    seven = next(c for c in build_candidates(calc) if "Seven" in c.summary)

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        return ToolRefused(code="brand_new_code", message="the daemon said no", data={}, text="", ms=1)

    session = FakeSession([calc], driver=FakeDriver(on_call), cache=tmp_path)
    out = await act_tool(session, {"pid": calc.pid, "instruction": "7", "candidateId": seven.id})
    assert list(out) == ["status", "code", "window", "message", "action"]
    assert out["status"] == "failed"
    assert out["code"] == "brand_new_code"
    assert "route" in out["action"]


async def test_a_rows_unchanged_click_is_unverified_not_no_effect(tmp_path: Path) -> None:
    win = row_window()
    row = next(
        c for c in build_candidates(win) if c.kind == "click" and c.target is not None and c.target.role == "AXRow"
    )
    session = FakeSession([win], cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "select", "candidateId": row.id})
    assert session.fake_driver.tools == ["click"]
    assert out["status"] == "unverified"
    assert out["message"].startswith("nothing on the window changed, but a row's selection")
    assert out["verification"] == {"snapshots": 5}


async def test_the_retype_after_an_ignored_write_is_judged_against_the_first_before(tmp_path: Path) -> None:
    value = "120"

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal value
        if tool == "set_value":
            value = "120"
        if tool == "type_text":
            value = "wrong"
        return _ok()

    session = FakeSession(lambda: text_window(value), driver=FakeDriver(on_call), cache=tmp_path)
    set_value = first(build_candidates(text_window("120"), BuildOptions(list_text_kinds=True, text="9")), "set_value")
    out = await act_tool(session, {"pid": 1, "instruction": "seats 9", "candidateId": set_value.id, "text": "9"})
    assert out["status"] == "mismatch"
    assert out["verification"]["snapshots"] == 10
    assert out["action"]["route"] == ["set_value", "press_key", "press_key", "type_text"]
    assert 1 not in session.types_into_web_fields


async def test_a_failed_retype_reports_the_window_it_left(tmp_path: Path) -> None:
    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "press_key":
            return ToolRefused(code="key_refused", message="no keys today", data={}, text="", ms=1)
        return _ok()

    session = FakeSession([text_window("120")], driver=FakeDriver(on_call), cache=tmp_path)
    set_value = first(build_candidates(text_window("120"), BuildOptions(list_text_kinds=True, text="9")), "set_value")
    out = await act_tool(session, {"pid": 1, "instruction": "seats 9", "candidateId": set_value.id, "text": "9"})
    assert list(out) == ["status", "code", "window", "message", "action"]
    assert (out["status"], out["code"], out["message"]) == ("failed", "key_refused", "no keys today")
    assert out["action"]["route"] == ["set_value", "press_key"]
    assert session.snapshots == 6


def _checkbox_window(value: str | None) -> Snapshot:
    frame: JsonValue = {"x": 10, "y": 10, "w": 20, "h": 20}
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Prefs", frame={"x": 0, "y": 0, "w": 100, "h": 100}),
        el(1, "AXCheckBox", 0, 1, label="Gift", frame=frame, actions=["press"]),
    ]
    md = '- [0] AXWindow "Prefs"\n  - [1] AXCheckBox (Gift) [actions=[press]]'
    snap = build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Prefs"}, 1, 1)
    if value is not None:
        snap.nodes[1].value = value
    return snap


async def test_a_toggle_whose_state_is_not_read_is_judged_by_its_pixels(tmp_path: Path) -> None:
    shots: list[Rgba] = [blank(100, 100), paint(blank(100, 100), 10, 10, 30, 30, 0)]

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "get_cursor_position":
            return ToolOk(data={"x": 90, "y": 90}, text="", ms=1)
        if tool == "get_window_state":
            out = args.get("screenshot_out_file")
            assert isinstance(out, str)
            Path(out).write_bytes(encode_png(shots.pop(0) if len(shots) > 1 else shots[0]))
            return ToolOk(data={"window_bounds": {"x": 0, "y": 0, "width": 100, "height": 100}}, text="", ms=1)
        return _ok()

    win = _checkbox_window(None)
    toggle = first(build_candidates(win), "toggle")
    session = FakeSession([win], driver=FakeDriver(on_call), cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "tick gift", "candidateId": toggle.id})
    assert out["status"] == "done"
    assert out["message"] == 'CheckBox "Gift" changed its appearance (its state is not in the accessibility tree)'
    assert out["verification"] == {"snapshots": 1}


async def test_a_popup_choice_via_jev_enters_the_text(tmp_path: Path) -> None:
    closed, is_open, chosen = popup_window("Small", False), popup_window("Small", True), popup_window("Large", False)
    session = FakeSession([closed, is_open, chosen], jev_picking("PopUpButton"), cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "size large", "text": "Large"})
    assert out["status"] == "done"
    assert session._jev.states[0]["text"] == "Large"
