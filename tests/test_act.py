from __future__ import annotations

from pathlib import Path

import pytest

from beans_picker._json import JsonValue
from beans_picker.candidates.build import BuildOptions, build_candidates
from beans_picker.candidates.types import ActionCandidate
from beans_picker.driver.types import ToolOk, ToolResult
from beans_picker.errors import JevUnavailable
from beans_picker.observe.snapshot import build_snapshot
from beans_picker.observe.types import Snapshot
from beans_picker.tools.act import act_tool
from tests.fakes import FakeDriver
from tests.helpers import snap_fixture
from tests.test_state import el
from tests.tool_fakes import FakeSession, JevPicking, jev_picking, shown


def _confirmed() -> ToolOk:
    return ToolOk(data={"effect": "confirmed"}, text="", ms=1)


def first(cands: list[ActionCandidate], kind: str, label: str | None = None) -> ActionCandidate:
    return next(
        c for c in cands if c.kind == kind and (label is None or (c.target is not None and c.target.label == label))
    )


def popup_window(value: str, is_open: bool) -> Snapshot:
    items = ["Small", "Medium", "Large"]
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Form"),
        el(1, "AXPopUpButton", 0, 1, label=value, value=value, actions=["press"]),
    ]
    md = ['- [0] AXWindow "Form"', f'  - [1] AXPopUpButton = "{value}" [actions=[press]]']
    if is_open:
        elements.append(el(2, "AXMenu", 1, 2))
        elements.extend(el(3 + i, "AXMenuItem", 2, 3, label=t, actions=["press"]) for i, t in enumerate(items))
        md.append("    - [2] AXMenu")
        md.extend(f'      - [{3 + i}] AXMenuItem "{t}" [actions=[press]]' for i, t in enumerate(items))
    snap = build_snapshot({"elements": elements, "tree_markdown": "\n".join(md), "window_title": "Form"}, 1, 1)
    snap.signature = f"{value}:{'true' if is_open else 'false'}"
    return snap


def _field_window(title: str, role: str, label: str, value: str, signature: str) -> Snapshot:
    elements: list[JsonValue] = [
        el(0, "AXWindow", title=title),
        el(1, "AXWebArea", 0, 1, label=title),
        el(2, role, 1, 2, label=label),
    ]
    md = f'- [0] AXWindow "{title}"\n  - [1] AXWebArea "{title}"\n    - [2] {role} "{label}"'
    snap = build_snapshot({"elements": elements, "tree_markdown": md, "window_title": title}, 1, 1)
    field = next(n for n in snap.nodes if n.role == role)
    field.raw_value = value
    field.value = value
    field.exact = True
    snap.signature = signature
    return snap


def number_window(value: str) -> Snapshot:
    return _field_window("Order", "AXIncrementor", "Quantity", value, f"qty:{value}")


def text_window(value: str) -> Snapshot:
    return _field_window("Tickets", "AXTextField", "Seats", value, f"seats:{value}")


def row_window() -> Snapshot:
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Team drive"),
        el(1, "AXOutline", 0, 1, label="Folders"),
        el(2, "AXRow", 1, 2, label="Draft SOW.docx"),
    ]
    md = '- [0] AXWindow "Team drive"\n  - [1] AXOutline "Folders"\n    - [2] AXRow "Draft SOW.docx"'
    return build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Team drive"}, 1, 1)


@pytest.fixture
def calc() -> Snapshot:
    return snap_fixture("calculator")


async def test_runs_jevs_clear_pick_once_and_reports_done_when_the_window_changes(
    calc: Snapshot, tmp_path: Path
) -> None:
    session = FakeSession([calc, shown(calc, "7")], jev_picking('"Seven"'), cache=tmp_path)
    out = await act_tool(session, {"app": "Calculator", "instruction": "press 7"})
    assert out["status"] == "done"
    assert "Seven" in out["action"]["does"]
    assert len(session.calls) == 1
    tool, payload = session.calls[0]
    assert tool == "click"
    assert payload["pid"] == calc.pid


async def test_reports_no_effect_after_the_fixed_number_of_unchanged_snapshots(calc: Snapshot, tmp_path: Path) -> None:
    session = FakeSession([calc], jev_picking('"Seven"'), cache=tmp_path)
    out = await act_tool(session, {"app": "Calculator", "instruction": "press 7"})
    assert out["status"] == "no_effect"
    assert out["verification"]["snapshots"] == 5


async def test_returns_the_shortlist_instead_of_acting_when_jev_has_no_clear_leader(
    calc: Snapshot, tmp_path: Path
) -> None:
    session = FakeSession([calc], jev_picking('"Seven"', '"Eight"'), cache=tmp_path)
    out = await act_tool(session, {"app": "Calculator", "instruction": "press a digit"})
    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) > 1
    assert session.calls == []


async def test_asks_before_an_action_that_may_not_be_undone_and_runs_it_only_with_allow_destructive(
    calc: Snapshot, tmp_path: Path
) -> None:
    close_all = next(
        c
        for c in build_candidates(calc)
        if c.menu is not None
        and " > ".join(c.menu.path) == "\u30a6\u30a4\u30f3\u30c9\u30a6 > \u3059\u3079\u3066\u3092\u9589\u3058\u308b"
    )
    session = FakeSession([calc], cache=tmp_path)
    out = await act_tool(session, {"app": "Calculator", "instruction": "close all", "candidateId": close_all.id})
    assert out["status"] == "needs_confirmation"
    assert session.calls == []


async def test_needs_a_text_for_a_text_candidate_and_rejects_ids_that_are_not_on_the_window(tmp_path: Path) -> None:
    te = snap_fixture("textedit")
    append = first(build_candidates(te, BuildOptions(list_text_kinds=True)), "append")
    session = FakeSession([te], cache=tmp_path)
    out = await act_tool(session, {"app": "TextEdit", "instruction": "append", "candidateId": append.id})
    assert out.get("code") == "text_required"
    out = await act_tool(session, {"app": "TextEdit", "instruction": "x", "candidateId": "cdeadbeef"})
    assert out["status"] == "not_found"
    assert session.calls == []


async def test_enters_the_callers_text_untouched(tmp_path: Path) -> None:
    te = snap_fixture("textedit")
    type_into = first(build_candidates(te, BuildOptions(list_text_kinds=True)), "type_into")
    session = FakeSession([te], cache=tmp_path)
    await act_tool(session, {"app": "TextEdit", "instruction": "type", "candidateId": type_into.id, "text": " a  b \n"})
    tool, payload = session.calls[0]
    assert tool == "type_text"
    assert payload["text"] == " a  b \n"


async def test_chooses_a_popup_item_by_opening_the_popup_and_pressing_the_item_with_that_exact_title(
    tmp_path: Path,
) -> None:
    closed, is_open, chosen = popup_window("Small", False), popup_window("Small", True), popup_window("Large", False)
    choose = first(build_candidates(closed, BuildOptions(list_text_kinds=True, text="Large")), "choose_option")
    session = FakeSession([closed, is_open, chosen], cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "choose Large", "candidateId": choose.id, "text": "Large"})
    assert session.fake_driver.tools == ["click", "click"]
    assert session.calls[1][1]["element_token"] == "t:5"  # noqa: S105 - an element token, not a secret
    assert out["status"] == "done"


async def test_retypes_a_web_pages_number_field_and_checks_its_exact_value(tmp_path: Path) -> None:
    was, now = number_window("2"), number_window("1")
    set_value = first(build_candidates(was, BuildOptions(list_text_kinds=True, text="1")), "set_value")
    session = FakeSession([was, now], cache=tmp_path)
    out = await act_tool(
        session, {"pid": 1, "instruction": "set quantity to 1", "candidateId": set_value.id, "text": "1"}
    )
    assert [(tool, payload.get("key")) for tool, payload in session.calls] == [
        ("press_key", "end"),
        ("press_key", "home"),
        ("type_text", None),
    ]
    assert session.calls[1][1]["modifiers"] == ["shift"]
    assert session.calls[2][1]["text"] == "1"
    assert out["status"] == "done"


async def test_closes_a_popups_menu_left_open_after_one_of_its_items_is_pressed_directly(tmp_path: Path) -> None:
    is_open = popup_window("Small", True)
    item = first(build_candidates(is_open), "click", "Large")
    session = FakeSession([is_open, is_open, popup_window("Large", False)], cache=tmp_path)
    await act_tool(session, {"pid": 1, "instruction": "pick Large", "candidateId": item.id})
    assert [(tool, payload.get("key")) for tool, payload in session.calls] == [("click", None), ("press_key", "escape")]


async def test_closes_the_popup_again_and_lists_its_items_when_none_has_the_title(tmp_path: Path) -> None:
    closed, is_open = popup_window("Small", False), popup_window("Small", True)
    choose = first(build_candidates(closed, BuildOptions(list_text_kinds=True, text="Huge")), "choose_option")
    session = FakeSession([closed, is_open], cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "choose Huge", "candidateId": choose.id, "text": "Huge"})
    assert (out["status"], out.get("code")) == ("failed", "option_not_found")
    assert '"Medium"' in out["message"]
    assert session.fake_driver.tools == ["click", "press_key"]


async def test_types_into_a_web_text_field_so_the_pages_own_copy_follows(tmp_path: Path) -> None:
    value = "120"

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal value
        if tool == "type_text":
            text = args.get("text")
            value = text if isinstance(text, str) else ""
        return _confirmed()

    session = FakeSession(lambda: text_window(value), driver=FakeDriver(on_call), cache=tmp_path)
    set_value = first(build_candidates(text_window("120"), BuildOptions(list_text_kinds=True, text="210")), "set_value")
    out = await act_tool(
        session, {"pid": 1, "instruction": "set seats to 210", "candidateId": set_value.id, "text": "210"}
    )
    assert session.fake_driver.tools == ["press_key", "press_key", "type_text"]
    assert out["status"] == "done"
    assert '"210"' in out["message"]


async def test_selects_a_whole_text_area_before_typing_over_it(tmp_path: Path) -> None:
    value = "old\nnotes"

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal value
        if tool == "type_text":
            text = args.get("text")
            value = text if isinstance(text, str) else ""
        return _confirmed()

    def window() -> Snapshot:
        return _field_window("Notes", "AXTextArea", "Memo", value, f"memo:{value}")

    session = FakeSession(window, driver=FakeDriver(on_call), cache=tmp_path)
    memo = first(build_candidates(window(), BuildOptions(list_text_kinds=True, text="new")), "set_value")
    out = await act_tool(session, {"pid": 1, "instruction": "memo", "candidateId": memo.id, "text": "new notes"})
    assert out["status"] == "done"
    keys = [(a["key"], a["modifiers"]) for t, a in session.calls if t == "press_key"]
    assert keys == [("down", ["cmd"]), ("up", ["shift", "cmd"])]


@pytest.mark.parametrize("role", ["AXTextField", "AXTextArea"])
async def test_writes_text_with_a_line_break_rather_than_pressing_return(tmp_path: Path, role: str) -> None:
    value = ""

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal value
        v = args.get("value")
        if tool == "set_value" and isinstance(v, str):
            value = v
        return _confirmed()

    def window() -> Snapshot:
        return _field_window("Chat", role, "Message", value, f"msg:{value}")

    session = FakeSession(window, driver=FakeDriver(on_call), cache=tmp_path)
    field = first(build_candidates(window(), BuildOptions(list_text_kinds=True, text="a")), "set_value")
    await act_tool(session, {"pid": 1, "instruction": "message", "candidateId": field.id, "text": "a\nb"})
    assert "type_text" not in session.fake_driver.tools
    assert "set_value" in session.fake_driver.tools


async def test_opens_a_rows_context_menu_with_axshowmenu_in_the_background(tmp_path: Path) -> None:
    win = row_window()
    open_menu = first(build_candidates(win), "context_menu")
    assert open_menu.summary == 'open the context menu of Row "Draft SOW.docx" (right-click)'
    session = FakeSession([win, shown(win, "Rename\u2026")], cache=tmp_path)
    out = await act_tool(session, {"pid": 1, "instruction": "right-click the file", "candidateId": open_menu.id})
    tool, payload = session.calls[0]
    assert tool == "right_click"
    assert {k: payload[k] for k in ("pid", "window_id", "element_token")} == {
        "pid": 1,
        "window_id": 1,
        "element_token": "t:2",
    }
    assert out["status"] == "done"


async def test_runs_the_steps_in_then_on_the_window_each_step_left_and_stops_at_the_first_that_is_not_done(
    tmp_path: Path,
) -> None:
    value = "1"

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal value
        # A web field is typed into; an AXValue write would carry `value` instead.
        v = args.get("text", args.get("value"))
        if isinstance(v, str) and v != "stuck":
            value = v
        return _confirmed()

    session = FakeSession(lambda: text_window(value), driver=FakeDriver(on_call), cache=tmp_path)
    cid = first(build_candidates(text_window("1"), BuildOptions(list_text_kinds=True, text="x")), "set_value").id
    out = await act_tool(
        session,
        {
            "pid": 1,
            "instruction": "seats 2",
            "candidateId": cid,
            "text": "2",
            "then": [{"instruction": "seats 3", "candidateId": cid, "text": "3"}],
        },
    )
    assert out["status"] == "done"
    assert [s["status"] for s in out["steps"]] == ["done", "done"]
    assert session.snapshots == 3
    stopped = await act_tool(
        session,
        {
            "pid": 1,
            "instruction": "seats 4",
            "candidateId": cid,
            "text": "4",
            "then": [
                {"instruction": "x", "candidateId": "cdeadbeef"},
                {"instruction": "seats 5", "candidateId": cid, "text": "5"},
            ],
        },
    )
    assert (stopped["status"], stopped.get("skipped")) == ("not_found", 1)
    assert value == "4"


async def test_a_step_that_raises_keeps_the_steps_before_it(tmp_path: Path) -> None:
    value = "1"

    def on_call(tool: str, args: dict[str, object]) -> ToolResult:
        nonlocal value
        v = args.get("text", args.get("value"))
        if isinstance(v, str):
            value = v
        return _confirmed()

    class NoJev(FakeSession):
        def jev(self) -> JevPicking:
            raise JevUnavailable("JEV_API_KEY is not set")

    session = NoJev(lambda: text_window(value), driver=FakeDriver(on_call), cache=tmp_path)
    cid = first(build_candidates(text_window("1"), BuildOptions(list_text_kinds=True, text="x")), "set_value").id
    out = await act_tool(
        session,
        {
            "pid": 1,
            "instruction": "seats 2",
            "candidateId": cid,
            "text": "2",
            "then": [
                {"instruction": "seats 3", "text": "3"},
                {"instruction": "seats 4", "candidateId": cid, "text": "4"},
            ],
        },
    )
    assert out["status"] == "failed"
    assert out["message"] == "stopped at step 2 of 3 (failed); the rest were not run"
    assert out.get("skipped") == 1
    steps = out["steps"]
    assert [s["status"] for s in steps] == ["done", "failed"]
    assert steps[1] == {"status": "failed", "code": "jev_unavailable", "message": "JEV_API_KEY is not set"}
    assert value == "2"


async def test_holds_modifier_keys_only_for_clicks_and_only_on_a_control_it_can_see(tmp_path: Path) -> None:
    win = row_window()
    cands = build_candidates(win)
    session = FakeSession([win], cache=tmp_path)
    key = first(cands, "key")
    out = await act_tool(session, {"pid": 1, "instruction": "shift", "candidateId": key.id, "modifiers": ["shift"]})
    assert out.get("code") == "modifiers_not_used"
    click = first(cands, "click")
    out = await act_tool(
        session, {"pid": 1, "instruction": "shift-click", "candidateId": click.id, "modifiers": ["shift"]}
    )
    assert out.get("code") == "not_visible"
    assert session.calls == []
