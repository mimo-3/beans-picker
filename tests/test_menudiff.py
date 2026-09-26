from __future__ import annotations

import copy
import dataclasses

from cua_jev._json import JsonValue
from cua_jev.candidates.types import ActionCandidate
from cua_jev.jev.state import change_of
from cua_jev.observe.menudiff import MenuRelabel, enabled_changes, relabel_of, relabeled_menu_items
from cua_jev.observe.snapshot import build_snapshot
from cua_jev.observe.types import MenuItem, Snapshot
from cua_jev.verify.effect import EffectVerdict, verify_effect
from tests.helpers import fixture_ids, raw_fixture, snap_fixture


def item(*path: str, enabled: bool = True, identifier: str | None = None) -> MenuItem:
    return MenuItem(path=list(path), enabled=enabled, key=" > ".join(path), identifier=identifier)


def retitle(s: Snapshot, was: str, now: str) -> Snapshot:
    c = copy.deepcopy(s)
    c.menu = [dataclasses.replace(m, path=[*m.path[:-1], now]) if m.path[-1:] == [was] else m for m in c.menu]
    return c


BEFORE = [
    item("Format", "Font", "Show Fonts"),
    item("Format", "Font", "Bold", enabled=False),
    item("Edit", "Copy", enabled=False),
]
AFTER = [item("Format", "Font", "Hide Fonts"), item("Format", "Font", "Bold", enabled=False), item("Edit", "Copy")]


def test_finds_an_item_retitled_in_its_slot() -> None:
    assert relabel_of(["Format", "Font", "Show Fonts"], BEFORE, AFTER) == "Hide Fonts"
    assert relabel_of(["Format", "Font", "Bold"], BEFORE, AFTER) is None
    assert relabeled_menu_items(BEFORE, AFTER) == [
        MenuRelabel(parent=["Format", "Font"], from_="Show Fonts", to="Hide Fonts")
    ]


def test_reports_enabled_state_flips() -> None:
    assert enabled_changes(BEFORE, AFTER) == {"enabled": ["Copy"], "disabled": []}


def test_ignores_flips_in_the_window_menu_which_follow_window_focus() -> None:
    def win(enabled: bool) -> list[MenuItem]:
        return [
            item("Window", "Bring All to Front", identifier="arrangeInFront:"),
            item("Window", "Move & Resize", "Quarters", enabled=enabled),
        ]

    assert enabled_changes([*BEFORE, *win(True)], [*BEFORE, *win(False)]) == {"enabled": [], "disabled": []}


def test_counts_a_retitled_menu_item_as_the_effect_of_a_menu_command_that_changed_nothing_else() -> None:
    te = dataclasses.replace(snap_fixture("textedit"), frontmost=True)
    shown = next(m for m in te.menu if m.path[-1] == "フォントパネルを表示")
    c = ActionCandidate(id="c1", kind="menu", key=shown.key, summary="menu", menu=shown, destructive=False, lexical=0)
    assert verify_effect(c, te, te).effect == "none"
    assert verify_effect(c, te, retitle(te, "フォントパネルを表示", "フォントパネルを非表示")) == EffectVerdict(
        effect="ok", detail='menu item now reads "フォントパネルを非表示"'
    )


def test_reports_newly_enabled_menu_items_only_when_the_window_was_in_front_both_times() -> None:
    te = dataclasses.replace(snap_fixture("textedit"), frontmost=True)
    after = copy.deepcopy(te)
    after.menu = [dataclasses.replace(m, enabled=True) if m.path[-1] == "コピー" else m for m in after.menu]
    assert change_of(te, after).get("menu_items_enabled") == ["コピー"]
    assert change_of(dataclasses.replace(te, frontmost=False), after).get("menu_items_enabled") is None


def test_keeps_an_unlabeled_fields_key_when_its_content_changes() -> None:
    raw = raw_fixture("textedit")
    edited = copy.deepcopy(raw)
    elements = edited["elements"]
    assert isinstance(elements, list)
    area: JsonValue = next(e for e in elements if isinstance(e, dict) and e.get("role") == "AXTextArea")
    assert isinstance(area, dict)
    area["value"] = "ALPHA BETA GAMMA"
    if area.get("label"):
        area["label"] = "ALPHA BETA GAMMA"
    a = next(n for n in build_snapshot(raw, *fixture_ids(raw)).nodes if n.role == "AXTextArea")
    b = next(n for n in build_snapshot(edited, *fixture_ids(raw)).nodes if n.role == "AXTextArea")
    assert b.key == a.key


# edge cases


def test_a_submenu_whose_item_count_changed_is_not_compared() -> None:
    before = [item("Format", "Show Fonts"), item("Format", "Bold")]
    after = [item("Format", "Hide Fonts")]
    assert relabeled_menu_items(before, after) == []
    assert relabel_of(["Format", "Show Fonts"], before, after) is None
    assert relabel_of(["View", "Zoom"], before, after) is None


def test_a_title_that_is_not_unique_in_its_submenu_has_no_relabel() -> None:
    before = [item("Edit", "Copy"), item("Edit", "Copy")]
    after = [item("Edit", "Cut"), item("Edit", "Copy")]
    assert relabel_of(["Edit", "Copy"], before, after) is None
    assert relabeled_menu_items(before, after) == [MenuRelabel(parent=["Edit"], from_="Copy", to="Cut")]


def test_top_level_items_have_an_empty_parent() -> None:
    relabels = relabeled_menu_items([item("File")], [item("Archive")])
    assert relabels == [MenuRelabel(parent=[""], from_="File", to="Archive")]
    assert relabels[0].to_json() == {"parent": [""], "from": "File", "to": "Archive"}
    assert relabel_of(["File"], [item("File")], [item("Archive")]) == "Archive"


def test_relabels_are_capped_overall() -> None:
    before = [item("A", str(i)) for i in range(4)] + [item("B", str(i)) for i in range(4)]
    after = [item("A", f"x{i}") for i in range(4)] + [item("B", f"x{i}") for i in range(4)]
    out = relabeled_menu_items(before, after)
    assert [(r.parent, r.from_) for r in out] == [
        (["A"], "0"),
        (["A"], "1"),
        (["A"], "2"),
        (["A"], "3"),
        (["B"], "0"),
        (["B"], "1"),
    ]
    assert len(relabeled_menu_items(before, after, limit=2)) == 2


def test_enabled_changes_are_named_below_the_menu_title_and_capped() -> None:
    before = [item("Edit", "Transform", str(i), enabled=False) for i in range(8)] + [item("Edit", "Paste")]
    after = [item("Edit", "Transform", str(i)) for i in range(8)] + [item("Edit", "Paste", enabled=False)]
    changes = enabled_changes(before, after, limit=6)
    assert changes["enabled"] == [f"Transform > {i}" for i in range(6)]
    assert changes["disabled"] == ["Paste"]
    # Items new in `after` have no earlier state to compare.
    assert enabled_changes([], after) == {"enabled": [], "disabled": []}


def test_the_window_menu_is_recognized_by_any_stock_action_in_either_list() -> None:
    before = [item("ウインドウ", "Zoom", identifier="performZoom"), item("ウインドウ", "Tile", enabled=False)]
    after = [item("ウインドウ", "Zoom"), item("ウインドウ", "Tile")]
    assert enabled_changes(before, after) == {"enabled": [], "disabled": []}
    assert enabled_changes(
        after, [item("ウインドウ", "Zoom", identifier="zoomAll:"), item("ウインドウ", "Tile", enabled=False)]
    ) == {
        "enabled": [],
        "disabled": [],
    }
    not_stock = [item("View", "Zoom", identifier="performZoomX"), item("View", "Tile", enabled=False)]
    assert enabled_changes(not_stock, [item("View", "Zoom"), item("View", "Tile")]) == {
        "enabled": ["Tile"],
        "disabled": [],
    }
