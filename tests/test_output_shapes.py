"""Key order and key omission of everything the tools return."""

from __future__ import annotations

from pathlib import Path

from cua_jev._json import dumps
from cua_jev._numbers import round3
from cua_jev.candidates.types import ActionCandidate
from cua_jev.menus.keyequiv import KeyEquivalent
from cua_jev.observe.types import Modal, UINode
from cua_jev.tools.observe import observe_tool
from cua_jev.tools.present import show_candidate, show_screen, show_window
from tests.helpers import snap_fixture
from tests.test_act import popup_window
from tests.test_state import node, snap, text
from tests.tool_fakes import FakeSession, jev_picking


def cand(**kw: object) -> ActionCandidate:
    c = ActionCandidate(id="c1", kind="click", key="k", summary="press Button OK")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def target(value: str | None) -> UINode:
    return node(1, "AXTextField", "Name", value=value)


def test_show_candidate_writes_only_what_applies_in_a_fixed_order() -> None:
    assert dumps(show_candidate(cand())) == '{"id":"c1","kind":"click","does":"press Button OK"}'
    full = cand(
        kind="set_value",
        target=target("x" * 70),
        destructive=True,
        shortcut=KeyEquivalent(keys=["cmd", "shift", "s"], source="standard"),
        needs_foreground=True,
    )
    assert dumps(show_candidate(full, 0.12345)) == (
        '{"id":"c1","kind":"set_value","does":"press Button OK","value":"'
        + "x" * 59
        + '\\u2026","needsText":true,"destructive":true,"shortcut":"cmd+shift+s","needsForeground":true,"p":0.123}'
    ).replace("\\u2026", "\u2026")


def test_show_candidate_leaves_out_empty_values_menu_values_and_given_texts() -> None:
    assert "value" not in show_candidate(cand(target=target("")))
    assert "value" not in show_candidate(cand(target=target(None)))
    assert "value" not in show_candidate(cand(kind="menu", target=target("v")))
    assert "needsText" not in show_candidate(cand(kind="type_into", text=""))
    assert "needsText" not in show_candidate(cand(kind="click"))
    assert "destructive" not in show_candidate(cand(destructive=False))
    assert "needsForeground" not in show_candidate(cand(needs_foreground=False))
    assert show_candidate(cand(), 0.0005)["p"] == 0.001
    assert show_candidate(cand(), 0)["p"] == 0


def test_show_window_with_a_modal_and_other_windows() -> None:
    s = snap([], modal=Modal(role="AXSheet", label="Save?", index=4), app_windows=["Draft", "Notes"])
    assert dumps(show_window(s)) == (
        '{"app":"Tickets","pid":7,"windowId":9,"title":"Order","modal":"Sheet \\"Save?\\"",'
        '"otherWindows":["Draft","Notes"]}'
    )
    assert list(show_window(snap([], app_windows=[]))) == ["app", "pid", "windowId", "title"]


def test_show_screen_reads_thirty_lines() -> None:
    s = snap([], [text(f"line {i}") for i in range(40)])
    assert len(show_screen(s)["screenText"]) == 30
    assert list(show_screen(s)) == ["screenText", "fields"]


async def test_observe_without_an_instruction_lists_candidates_without_asking_jev(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    session = FakeSession([calc], cache=tmp_path)
    out = await observe_tool(session, {"app": "Calculator", "limit": 3})
    assert list(out) == ["window", "screenText", "fields", "total", "candidates"]
    assert len(out["candidates"]) == 3
    assert out["total"] > 3
    assert all("p" not in c for c in out["candidates"])
    assert session.jev_calls == 0
    everything = await observe_tool(session, {"app": "Calculator"})
    assert len(everything["candidates"]) == min(80, everything["total"])


async def test_observe_with_an_instruction_ranks_and_reports_usage(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    session = FakeSession([calc], jev_picking('"Seven"'), cache=tmp_path)
    out = await observe_tool(session, {"app": "Calculator", "instruction": "press 7"})
    assert list(out) == ["window", "screenText", "fields", "total", "pNone", "candidates", "jev"]
    # More candidates than one question holds: the shards' leaders meet in a runoff.
    assert out["total"] > 60
    assert [c["does"] for c in out["candidates"]] == ['click Button "7" (Seven)']
    assert out["candidates"][0]["p"] == 0.95
    assert out["jev"] == {"calls": 1, "inputTokens": 10, "ms": 1}
    assert list(session._jev.states[0])[:2] == ["instruction", "app"]


async def test_observe_with_an_instruction_returns_ten_by_default(tmp_path: Path) -> None:
    win = popup_window("Small", False)
    session = FakeSession([win], jev_picking("PopUpButton"), cache=tmp_path)
    out = await observe_tool(session, {"pid": 1, "instruction": "size"})
    assert len(out["candidates"]) == min(10, out["total"])
    assert out["candidates"][0]["kind"] == "choose_option"
    assert out["candidates"][0].get("needsText") is True
    assert out["pNone"] == round3(0.05 / (out["total"] + 1))
    assert len((await observe_tool(session, {"pid": 1, "instruction": "x", "limit": 2}))["candidates"]) == 2
