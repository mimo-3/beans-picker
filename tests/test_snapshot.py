from __future__ import annotations

import copy
import re

from cua_jev._json import JsonObject, JsonValue
from cua_jev.driver.markdown import parse_tree_markdown
from cua_jev.observe.exacttext import apply_exact_text
from cua_jev.observe.snapshot import assign_keys, build_snapshot, in_web_area, is_descendant
from cua_jev.observe.types import UINode
from cua_jev.verify.effect import verify_effect
from tests.act_support import cand, snap
from tests.helpers import fixture_ids, raw_fixture, snap_fixture


def _markdown(name: str) -> str:
    md = raw_fixture("calculator" if name == "calculator" else "textedit")["tree_markdown"]
    assert isinstance(md, str)
    return md


def _elements(raw: JsonObject) -> list[JsonValue]:
    elements = raw["elements"]
    assert isinstance(elements, list)
    return elements


# parse_tree_markdown


def test_reads_identifiers_and_help_that_only_the_markdown_carries() -> None:
    md = parse_tree_markdown(_markdown("calculator"))
    delete = next(n for n in md if n.index == 1)
    assert (delete.role, delete.label, delete.identifier) == ("AXButton", "削除", "Delete")
    assert delete.help is not None
    assert "最後の数字" in delete.help


def test_keeps_unindexed_rows_such_as_the_display_text() -> None:
    md = parse_tree_markdown(_markdown("calculator"))
    assert any(n.index is None and n.role == "AXStaticText" for n in md)


# build_snapshot (Calculator)


def test_joins_elements_with_markdown_identifiers() -> None:
    snap = snap_fixture("calculator")
    assert next(n for n in snap.nodes if n.identifier == "Equals").label == "計算実行"
    assert re.search(":[0-9]+$", next(n for n in snap.nodes if n.identifier == "Seven").token)


def test_splits_the_menu_bar_into_paths() -> None:
    snap = snap_fixture("calculator")
    assert any(" > ".join(m.path) == "表示 > 科学計算" for m in snap.menu)
    assert all(not n.in_menu_bar for n in snap.nodes)


def test_keeps_unindexed_static_text_with_its_raw_form() -> None:
    snap = snap_fixture("calculator")
    display = next(t for t in snap.texts if t.value.endswith("0"))
    assert display.raw.endswith("0")


def test_produces_a_stable_signature() -> None:
    assert snap_fixture("calculator").signature == snap_fixture("calculator").signature
    assert re.fullmatch("[0-9a-f]{16}", snap_fixture("calculator").signature)


# build_snapshot (TextEdit)


def test_finds_the_document_text_area() -> None:
    snap = snap_fixture("textedit")
    assert any(n.role == "AXTextArea" for n in snap.nodes)
    assert snap.window_title == "te-fixture.txt"


def test_marks_unindexed_menu_rows_as_disabled() -> None:
    snap = snap_fixture("textedit")
    assert any(not m.enabled for m in snap.menu)
    copy_item = next(m for m in snap.menu if m.path == ["編集", "コピー"])
    assert copy_item.enabled is False
    assert copy_item.index is None
    assert copy_item.token is None


def test_fixture_menu_items_carry_their_element_and_identifier() -> None:
    snap = snap_fixture("calculator")
    item = next(m for m in snap.menu if m.path == ["ウインドウ", "すべてを閉じる"])
    assert item.index == 146
    assert item.token is not None
    assert item.token.endswith(":146")
    assert item.identifier == "closeAll:"
    assert item.key == "AXMenuItem|menu|ウインドウ > すべてを閉じる"


def test_fixture_nodes_have_keys_and_containers() -> None:
    snap = snap_fixture("calculator")
    seven = next(n for n in snap.nodes if n.identifier == "Seven")
    assert seven.label == "7"
    assert seven.within == ["AXWindow: 計算機"]
    assert seven.key == "AXButton|Seven|7|AXWindow: 計算機"
    assert seven.actions == ["press"]
    unnamed = [n.key for n in snap.nodes if n.key.startswith("AXButton|||AXToolbar")]
    assert len(unnamed) == len(set(unnamed))


# calculator display with a superscript exponent


def test_keeps_a_pending_power_apart_and_keeps_the_raw_text() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXWindow "Calculator" [id=main actions=[raise]]\n'
        '    - AXStaticText = "\u200e2\u200e\u200e10"\n'
        '    - AXStaticText = "\u200e1,024"',
        "elements": [
            {
                "element_index": 0,
                "element_token": "s00000001:0",
                "role": "AXWindow",
                "label": "Calculator",
                "depth": 0,
                "actions": ["AXRaise"],
            }
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert [t.value for t in snap.texts] == ["2 10", "1,024"]
    assert [t.raw for t in snap.texts] == ["\u200e2\u200e\u200e10", "\u200e1,024"]
    assert snap.window_title == ""
    assert [t.depth for t in snap.texts] == [2, 2]
    assert [t.parent_index for t in snap.texts] == [0, 0]


# a window the tree lists twice


def test_keeps_one_copy_of_each_control() -> None:
    raw: JsonObject = {
        "tree_markdown": "\n".join(
            [
                '- [0] AXWindow "計算機" [id=main actions=[raise]]',
                '  - [1] AXButton "7" [id=Seven actions=[press]]',
                '  - AXStaticText = "0"',
                "- [2] AXMenuBar [actions=[cancel]]",
                '- [3] AXWindow "計算機" [id=main actions=[raise]]',
                '  - [4] AXButton "7" [id=Seven actions=[press]]',
                '  - AXStaticText = "0"',
            ]
        ),
        "elements": [
            {
                "element_index": 0,
                "element_token": "s:0",
                "role": "AXWindow",
                "label": "計算機",
                "depth": 0,
                "frame": {"x": 1, "y": 1, "w": 9, "h": 9},
            },
            {
                "element_index": 1,
                "element_token": "s:1",
                "role": "AXButton",
                "label": "7",
                "parent_index": 0,
                "depth": 1,
            },
            {"element_index": 2, "element_token": "s:2", "role": "AXMenuBar", "depth": 0},
            {
                "element_index": 3,
                "element_token": "s:3",
                "role": "AXWindow",
                "label": "計算機",
                "depth": 0,
                "frame": {"x": 1, "y": 1, "w": 9, "h": 9},
            },
            {
                "element_index": 4,
                "element_token": "s:4",
                "role": "AXButton",
                "label": "7",
                "parent_index": 3,
                "depth": 1,
            },
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert [n.index for n in snap.nodes if n.label == "7"] == [1]
    assert len(snap.texts) == 1


def test_windows_with_other_frames_are_both_kept() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXWindow "A"\n  - [1] AXButton "7"\n- [2] AXWindow "A"\n  - [3] AXButton "7"',
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "A", "frame": {"x": 1, "y": 1, "w": 9, "h": 9}},
            {"element_index": 1, "role": "AXButton", "label": "7", "parent_index": 0},
            {"element_index": 2, "role": "AXWindow", "label": "A", "frame": {"x": 2, "y": 1, "w": 9, "h": 9}},
            {"element_index": 3, "role": "AXButton", "label": "7", "parent_index": 2},
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert [n.index for n in snap.nodes if n.label == "7"] == [1, 3]
    # The second "7" is told apart by order: the containers carry the same name.
    assert [n.key for n in snap.nodes if n.label == "7"] == ["AXButton||7|AXWindow: A", "AXButton||7|AXWindow: A#2"]


# a table that lists its cells under columns too


def test_keeps_the_cells_under_their_rows_only() -> None:
    elements: list[JsonValue] = [
        {"element_index": 0, "element_token": "s:0", "role": "AXWindow", "label": "Order", "depth": 0},
        {"element_index": 1, "element_token": "s:1", "role": "AXTable", "parent_index": 0, "depth": 1},
        {"element_index": 2, "element_token": "s:2", "role": "AXRow", "parent_index": 1, "depth": 2},
        {"element_index": 3, "element_token": "s:3", "role": "AXCell", "parent_index": 2, "depth": 3},
        {
            "element_index": 4,
            "element_token": "s:4",
            "role": "AXIncrementor",
            "label": "Quantity",
            "parent_index": 3,
            "depth": 4,
        },
        {"element_index": 5, "element_token": "s:5", "role": "AXColumn", "parent_index": 1, "depth": 2},
        {"element_index": 6, "element_token": "s:6", "role": "AXCell", "parent_index": 5, "depth": 3},
        {
            "element_index": 7,
            "element_token": "s:7",
            "role": "AXIncrementor",
            "label": "Quantity",
            "parent_index": 6,
            "depth": 4,
        },
    ]
    md = "\n".join(
        [
            '- [0] AXWindow "Order"',
            "  - [1] AXTable",
            "    - [2] AXRow",
            "      - [3] AXCell",
            '        - [4] AXIncrementor "Quantity"',
            '        - AXStaticText = "Teapot"',
            "    - [5] AXColumn",
            "      - [6] AXCell",
            '        - [7] AXIncrementor "Quantity"',
            '        - AXStaticText = "Teapot"',
        ]
    )
    snap = build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Order"}, 1, 1)
    assert [n.index for n in snap.nodes if n.role == "AXIncrementor"] == [4]
    assert not any(n.role == "AXColumn" for n in snap.nodes)
    assert len(snap.texts) == 1
    # The unnamed row is named after its first text, and the controls in it say which row they are on.
    row = next(n for n in snap.nodes if n.role == "AXRow")
    assert row.label == "Teapot"
    assert row.raw_label is None
    quantity = next(n for n in snap.nodes if n.role == "AXIncrementor")
    assert quantity.within == ["AXRow: Teapot", "AXWindow: Order"]


# a field whose placeholder cua-driver shows as its value


def _key_with(shown: str, exact: str) -> str:
    elements: list[JsonValue] = [
        {"element_index": 0, "element_token": "s:0", "role": "AXWindow", "label": "Ledger", "depth": 0},
        {
            "element_index": 1,
            "element_token": "s:1",
            "role": "AXTextField",
            "label": "Search",
            "value": shown,
            "parent_index": 0,
            "depth": 1,
        },
    ]
    md = "\n".join(['- [0] AXWindow "Ledger"', f'  - [1] AXTextField "Search" = "{shown}"'])
    snap = build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Ledger"}, 1, 1)
    apply_exact_text(snap.nodes, [{"window": "Ledger", "role": "AXTextField", "value": exact}], "Ledger")
    assign_keys(snap.nodes)
    return next(n for n in snap.nodes if n.role == "AXTextField").key


def test_keeps_one_key_before_and_after_it_is_typed_in() -> None:
    assert _key_with("Search", "") == _key_with("Harbor", "Harbor")


# a field named only by its placeholder: cua-driver gives the placeholder as its label and value while
# it is empty, and its content once it is typed in; the exact-text reader reports no title for it


def _placeholder_field(label: str, shown: str, exact: str, title: str) -> UINode:
    elements: list[JsonValue] = [
        {"element_index": 0, "element_token": "s:0", "role": "AXWindow", "label": "Form", "depth": 0},
        {
            "element_index": 1,
            "element_token": "s:1",
            "role": "AXTextField",
            "label": label,
            "value": shown,
            "parent_index": 0,
            "depth": 1,
        },
    ]
    md = "\n".join(['- [0] AXWindow "Form"', f'  - [1] AXTextField "{label}" = "{shown}"'])
    snap = build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Form"}, 1, 1)
    field = {"window": "Form", "role": "AXTextField", "title": title, "value": exact}
    apply_exact_text(snap.nodes, [field], "Form")
    assign_keys(snap.nodes)
    return next(n for n in snap.nodes if n.role == "AXTextField")


def test_a_placeholder_is_no_part_of_the_key() -> None:
    empty = _placeholder_field("Full name", "Full name", "", "")
    typed = _placeholder_field("Ada  L", "Ada  L", " Ada  L ", "")
    assert empty.label == "Full name"
    assert empty.placeholder == "Full name"
    assert empty.key == typed.key == "AXTextField|||AXWindow: Form"


def test_a_field_titled_like_its_placeholder_keeps_its_name() -> None:
    empty = _placeholder_field("Search", "Search", "", "Search")
    typed = _placeholder_field("Search", "Harbor", "Harbor", "Search")
    assert empty.placeholder is None
    assert empty.key == typed.key == "AXTextField||Search|AXWindow: Form"


def test_text_typed_into_a_placeholder_field_is_verified_in_that_field() -> None:
    before = _placeholder_field("Full name", "Full name", "", "")
    after = _placeholder_field("Ada  L", "Ada  L", " Ada  L ", "")
    c = cand("type_into", before, text=" Ada  L ")
    got = verify_effect(c, snap([before]), snap([after]))
    assert (got.effect, got.exact) == ("ok", True)


# exact state from the native reader


def _node(role: str, raw_value: str | None, raw_label: str | None = None) -> UINode:
    n = UINode(
        index=0,
        token="",
        role=role,
        label=raw_label if raw_label is not None else raw_value if raw_value is not None else "",
        enabled=True,
        actions=[],
        depth=1,
        in_menu_bar=False,
        within=[],
        key=role,
    )
    if raw_label:
        n.raw_label = raw_label
    if raw_value is not None:
        n.raw_value = raw_value
        n.value = raw_value.strip()
    return n


def test_pairs_fields_by_order_taking_an_empty_field_over_the_placeholder() -> None:
    search = _node("AXTextField", "Search notes")
    name = _node("AXTextField", "Ada  Lovelace")
    box = _node("AXCheckBox", None, "Send newsletter")
    apply_exact_text(
        [search, name, box],
        [
            {"window": "w", "role": "AXTextField", "value": ""},
            {"window": "w", "role": "AXTextField", "value": "  Ada  Lovelace "},
            {"window": "w", "role": "AXCheckBox", "title": "Send newsletter", "value": "1"},
        ],
        "w",
    )
    assert (search.raw_value, search.value, search.exact) == ("", "", True)
    assert (name.raw_value, name.exact) == ("  Ada  Lovelace ", True)
    assert (box.value, box.exact) == ("1", True)


def test_gives_a_number_field_the_value_cua_driver_leaves_out() -> None:
    nodes = [
        _node("AXTextField", "Mei"),
        _node("AXIncrementor", None, "Quantity"),
        _node("AXIncrementor", None, "Lids"),
    ]
    apply_exact_text(
        nodes,
        [
            {"window": "w", "role": "AXTextField", "value": "Mei"},
            {"window": "w", "role": "AXIncrementor", "title": "Quantity", "value": "2"},
            {"window": "w", "role": "AXIncrementor", "title": "Lids", "value": ""},
        ],
        "w",
    )
    _, qty, lids = nodes
    assert (qty.raw_value, qty.exact) == ("2", True)
    assert (lids.raw_value, lids.exact) == ("", True)


def test_leaves_a_number_field_alone_when_the_order_does_not_line_up() -> None:
    nodes = [_node("AXIncrementor", None, "Quantity"), _node("AXIncrementor", None, "Lids")]
    apply_exact_text(nodes, [{"window": "w", "role": "AXIncrementor", "value": ""}], "w")
    assert not any(n.exact for n in nodes)


def test_leaves_values_alone_when_the_lists_disagree() -> None:
    nodes = [_node("AXTextField", "one"), _node("AXTextField", "two")]
    apply_exact_text(nodes, [{"window": "w", "role": "AXTextField", "value": "three"}], "w")
    assert not any(n.exact for n in nodes)


# field identity


def test_keeps_an_unlabeled_fields_key_when_its_content_changes() -> None:
    raw = raw_fixture("textedit")
    edited = copy.deepcopy(raw)
    area = next(e for e in _elements(edited) if isinstance(e, dict) and e.get("role") == "AXTextArea")
    assert isinstance(area, dict)
    area["value"] = "ALPHA BETA GAMMA"
    if area.get("label"):
        area["label"] = "ALPHA BETA GAMMA"
    a = next(n for n in build_snapshot(raw, *fixture_ids(raw)).nodes if n.role == "AXTextArea")
    b = next(n for n in build_snapshot(edited, *fixture_ids(raw)).nodes if n.role == "AXTextArea")
    assert b.key == a.key


# node fields


def test_a_node_takes_its_fields_from_the_element_and_the_markdown_row() -> None:
    raw: JsonObject = {
        "snapshot_id": "s7",
        "app_name": " Notes\u00a0App ",
        "window_title": "Doc",
        "tree_markdown": "\n".join(
            [
                '- [0] AXWindow "Doc"',
                '  - [1] AXButton [id=AllClear help="Clears  everything"]',
                '  - [2] AXCheckBox "Bold"',
                '  - [3] AXButton [id=_NS:9 help="Short help"]',
                "  - [4] AXButton [id=_NS:10]",
                '  - [5] AXTextField = "hello"',
            ]
        ),
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "Doc", "depth": 0},
            {
                "element_index": 1,
                "element_token": "s7:1",
                "role": "AXButton",
                "subrole": "AXCloseButton",
                "enabled": False,
                "actions": ["AXPress", "press", "AXShowMenu"],
                "frame": {"x": 1, "y": 2, "w": 3, "h": 4},
                "parent_index": 0,
                "depth": 1,
            },
            {"element_index": 2, "role": "AXCheckBox", "value": 1, "selected": True, "parent_index": 0},
            {"element_index": 3, "role": "AXButton", "parent_index": 0, "depth": 1},
            {"element_index": 4, "role": "AXButton", "value": 0.5, "parent_index": 0, "depth": 1},
            {"element_index": 5, "role": "AXTextField", "label": "", "parent_index": 0, "depth": 1},
        ],
    }
    snap = build_snapshot(raw, 3, 9)
    assert (snap.id, snap.pid, snap.window_id, snap.app_name, snap.window_title) == ("s7", 3, 9, "Notes App", "Doc")
    assert snap.frontmost is None
    assert snap.app_windows is None
    clear, bold, short, number, field = snap.nodes[1:]
    assert clear.label == "All Clear"
    assert clear.raw_label is None
    assert clear.identifier == "AllClear"
    assert clear.help == "Clears everything"
    assert clear.enabled is False
    assert clear.actions == ["press", "showmenu"]
    assert clear.subrole == "AXCloseButton"
    assert clear.frame is not None
    assert (clear.frame.x, clear.frame.h) == (1, 4)
    assert (clear.parent, clear.token) == (0, "s7:1")
    assert bold.label == "Bold"
    assert bold.raw_label == "Bold"
    assert bold.title == "Bold"
    assert (bold.value, bold.raw_value) == ("1", "1")
    assert bold.selected is True
    assert bold.depth == 1  # from the markdown row, as the element has no depth
    assert bold.token == ""
    assert short.label == "Short help"
    assert number.label == "0.5"
    assert (number.value, number.raw_value) == ("0.5", "0.5")
    # An empty label from the element wins over the markdown; the value names the field for display.
    assert field.label == "hello"
    assert field.raw_label is None
    assert (field.value, field.raw_value) == ("hello", "hello")
    assert field.key == "AXTextField|||AXWindow: Doc"


def test_the_markdown_depth_is_used_when_the_element_has_none() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXWindow "W"\n   - [1] AXButton "B"',
        "elements": [
            {"element_index": 0, "role": "AXWindow"},
            {"element_index": 1, "role": "AXButton", "parent_index": 0},
        ],
    }
    nodes = build_snapshot(raw, 1, 1).nodes
    assert [n.depth for n in nodes] == [0, 1.5]
    assert nodes[0].label == "W"


def test_a_long_help_does_not_name_a_node_and_a_value_is_cut_to_forty() -> None:
    long_help = "h" * 81
    value = "v" * 50
    raw: JsonObject = {
        "tree_markdown": f'- [0] AXButton [help="{long_help}"]\n- [1] AXStaticText',
        "elements": [
            {"element_index": 0, "role": "AXButton", "value": value},
            {"element_index": 1, "role": "AXStaticText"},
        ],
    }
    button, text = build_snapshot(raw, 1, 1).nodes
    assert button.help == long_help
    assert button.label == "v" * 39 + "\u2026"
    assert text.label == ""


def test_an_absent_element_value_falls_back_to_the_markdown_value() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXTextField = "  from markdown "',
        "elements": [{"element_index": 0, "role": "AXTextField", "value": None}],
    }
    (node,) = build_snapshot(raw, 1, 1).nodes
    assert node.value == "from markdown"
    assert node.raw_value == "  from markdown "


# keys


def test_unlabeled_fields_side_by_side_are_numbered_by_order() -> None:
    nodes = [_node("AXTextField", None), _node("AXTextField", None), _node("AXTextField", None)]
    assign_keys(nodes)
    assert [n.key for n in nodes] == ["AXTextField|||", "AXTextField|||#2", "AXTextField|||#3"]


def test_a_field_named_after_its_value_keys_with_an_empty_label() -> None:
    field = _node("AXTextField", "Harbor")
    popup = _node("AXPopUpButton", "Medium")
    labeled = _node("AXTextField", "Harbor", "Marina")
    button = _node("AXButton", "Harbor")
    assign_keys([field, popup, labeled, button])
    assert field.key == "AXTextField|||"
    assert popup.key == "AXPopUpButton|||"
    assert labeled.key == "AXTextField||Marina|"
    assert button.key == "AXButton||Harbor|"


def test_a_field_whose_raw_label_is_its_value_keys_with_an_empty_label() -> None:
    text = ("long text " * 10).strip()
    field = _node("AXTextArea", text, text)
    field.label = "Notes"
    assign_keys([field])
    assert field.key == "AXTextArea|||"


# modal and open menus


def test_a_sheet_is_the_modal() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXWindow "W"\n  - [1] AXSheet "Save"\n    - [2] AXButton "OK"',
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "W"},
            {"element_index": 1, "role": "AXSheet", "label": "Save", "parent_index": 0},
            {"element_index": 2, "role": "AXButton", "label": "OK", "parent_index": 1},
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert snap.modal is not None
    assert (snap.modal.role, snap.modal.label, snap.modal.index) == ("AXSheet", "Save", 1)
    ok = snap.nodes[2]
    assert ok.key == "AXButton||OK|AXSheet: Save>AXWindow: W"
    assert is_descendant(snap, ok, 1)
    assert is_descendant(snap, ok, 2)
    assert not is_descendant(snap, snap.nodes[0], 1)


def test_an_open_menu_with_items_is_the_modal() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXWindow "W"\n  - [1] AXMenu\n    - AXMenuItem "Copy"\n    - [2] AXMenuItem "Paste"',
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "W"},
            {"element_index": 1, "role": "AXMenu", "parent_index": 0},
            {"element_index": 2, "role": "AXMenuItem", "label": "Paste", "parent_index": 1},
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert snap.modal is not None
    assert (snap.modal.role, snap.modal.label, snap.modal.index) == ("AXMenu", "open menu", 1)
    # Items of an open context menu are not menu-bar items.
    assert snap.menu == []


def test_an_empty_menu_is_not_a_modal() -> None:
    raw: JsonObject = {
        "tree_markdown": '- [0] AXWindow "W"\n  - [1] AXMenu "Tools"',
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "W"},
            {"element_index": 1, "role": "AXMenu", "label": "Tools", "parent_index": 0},
        ],
    }
    assert build_snapshot(raw, 1, 1).modal is None


def test_menu_bar_elements_are_kept_out_of_the_nodes() -> None:
    raw: JsonObject = {
        "tree_markdown": "\n".join(
            [
                '- [0] AXWindow "W"',
                "- [1] AXMenuBar",
                '  - [2] AXMenuBarItem "File"',
                "    - [3] AXMenu",
                '      - [4] AXMenuItem "Open" [id=openDocument:]',
                '      - AXMenuItem "Close"',
                '      - AXMenuItem ""',
            ]
        ),
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "W"},
            {"element_index": 1, "role": "AXMenuBar"},
            {"element_index": 2, "role": "AXMenuBarItem", "label": "File", "parent_index": 1},
            {"element_index": 3, "role": "AXMenu", "parent_index": 2},
            {"element_index": 4, "role": "AXMenuItem", "label": "Open", "parent_index": 3, "element_token": "t4"},
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert [n.index for n in snap.nodes] == [0]
    assert snap.modal is None
    assert [(m.path, m.enabled, m.index, m.token, m.identifier) for m in snap.menu] == [
        (["File", "Open"], True, 4, "t4", "openDocument:"),
        (["File", "Close"], False, None, None, None),
    ]


def test_in_web_area_looks_at_ancestors_only() -> None:
    raw: JsonObject = {
        "tree_markdown": "",
        "elements": [
            {"element_index": 0, "role": "AXWindow"},
            {"element_index": 1, "role": "AXWebArea", "parent_index": 0},
            {"element_index": 2, "role": "AXGroup", "parent_index": 1},
            {"element_index": 3, "role": "AXButton", "parent_index": 2},
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    web, _, button = snap.nodes[1:]
    assert in_web_area(snap, button)
    assert not in_web_area(snap, web)


def test_containers_nearest_first_at_most_three() -> None:
    elements: list[JsonValue] = [{"element_index": 0, "role": "AXWindow", "label": "W"}]
    for i, role in enumerate(["AXGroup", "AXButton", "AXToolbar", "AXScrollArea", "AXGroup"], start=1):
        elements.append({"element_index": i, "role": role, "label": f"{role[2:]}{i}", "parent_index": i - 1})
    elements.append({"element_index": 6, "role": "AXButton", "label": "Go", "parent_index": 5})
    snap = build_snapshot({"elements": elements}, 1, 1)
    assert snap.nodes[6].within == ["AXGroup: Group5", "AXScrollArea: ScrollArea4", "AXToolbar: Toolbar3"]


def test_elements_that_are_not_records_are_skipped() -> None:
    raw: JsonObject = {"elements": [{"element_index": 0, "role": "AXWindow"}, 5, {"role": "AXButton"}, None]}
    assert [n.index for n in build_snapshot(raw, 1, 1).nodes] == [0]


def test_empty_state() -> None:
    snap = build_snapshot({}, 4, 5)
    assert (snap.id, snap.nodes, snap.texts, snap.menu, snap.modal, snap.ms) == ("", [], [], [], None, 0)
    assert snap.taken_at > 0


def test_a_row_is_named_after_its_first_nonempty_text_only() -> None:
    raw: JsonObject = {
        "tree_markdown": "\n".join(
            [
                '- [0] AXWindow "W"',
                "  - [1] AXTable",
                "    - [2] AXRow",
                '      - AXStaticText = " "',
                '      - AXStaticText = "INV-1043"',
                '      - AXStaticText = "Paid"',
                '    - [3] AXRow "Named"',
                '      - AXStaticText = "Other"',
                "  - AXStaticText (outside)",
            ]
        ),
        "elements": [
            {"element_index": 0, "role": "AXWindow", "label": "W"},
            {"element_index": 1, "role": "AXTable", "parent_index": 0},
            {"element_index": 2, "role": "AXRow", "parent_index": 1},
            {"element_index": 3, "role": "AXRow", "parent_index": 1},
        ],
    }
    snap = build_snapshot(raw, 1, 1)
    assert [n.label for n in snap.nodes] == ["W", "", "INV-1043", "Named"]
    assert [t.value for t in snap.texts] == ["INV-1043", "Paid", "Other", "outside"]
