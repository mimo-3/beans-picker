from __future__ import annotations

import copy

from beans_picker._json import JsonObject, JsonValue, dumps
from beans_picker.jev.state import Change, build_state, change_of, changed, controls_of, fields_of, screen_text
from beans_picker.observe.snapshot import build_snapshot
from beans_picker.observe.types import MenuItem, Modal, Snapshot, TextNode, UINode
from tests.web_list import web_list


def el(index: int, role: str, parent: int | None = None, depth: int = 0, **extra: JsonValue) -> JsonObject:
    out: JsonObject = {"element_index": index, "element_token": f"t:{index}", "role": role, "depth": depth}
    if parent is not None:
        out["parent_index"] = parent
    out.update(extra)
    return out


def test_keeps_a_table_rows_texts_on_one_line_so_a_cell_that_repeats_another_rows_is_not_lost() -> None:
    rows = [["Room B", "240", "Yes"], ["Room D", "260", "Yes"]]
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Rooms"),
        el(1, "AXTable", 0, 1),
        el(2, "AXRow", 1, 2),
        el(3, "AXRow", 1, 2),
    ]
    md = "\n".join(
        [
            '- [0] AXWindow "Rooms"',
            '  - AXStaticText = "Rooms"',
            "  - [1] AXTable",
            *[
                line
                for i, r in enumerate(rows)
                for line in [f"    - [{2 + i}] AXRow", *(f'      - AXStaticText = "{t}"' for t in r)]
            ],
            '  - AXStaticText = "Rooms"',
        ]
    )
    snap = build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Rooms"}, 1, 1)
    assert screen_text(snap) == ["Rooms", "Room B | 240 | Yes", "Room D | 260 | Yes"]


def drive() -> Snapshot:
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Drive"),
        el(1, "AXTable", 0, 1),
        el(2, "AXRow", 1, 2),
        el(3, "AXCell", 2, 3),
        el(4, "AXCheckBox", 3, 4, label="Select Invoice.pdf"),
        el(5, "AXCell", 2, 3),
        el(6, "AXButton", 5, 4, label="Invoice.pdf"),
        el(7, "AXCell", 2, 3),
    ]
    md = "\n".join(
        [
            '- [0] AXWindow "Drive"',
            "  - [1] AXTable",
            "    - [2] AXRow",
            "      - [3] AXCell",
            "        - [4] AXCheckBox (Select Invoice.pdf)",
            "      - [5] AXCell",
            "        - [6] AXButton (Invoice.pdf)",
            "      - [7] AXCell",
            '        - AXStaticText = "142 KB"',
        ]
    )
    return build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Drive"}, 1, 1)


def test_puts_a_rows_name_only_button_in_its_place() -> None:
    assert screen_text(drive()) == ["Invoice.pdf | 142 KB"]


def node(index: int, role: str, label: str = "", **kw: object) -> UINode:
    n = UINode(
        index=index,
        token=f"t:{index}",
        role=role,
        label=label,
        enabled=True,
        actions=[],
        depth=1,
        in_menu_bar=False,
        within=[],
        key=f"{role}:{label}:{index}",
    )
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def snap(nodes: list[UINode], texts: list[TextNode] | None = None, **kw: object) -> Snapshot:
    s = Snapshot(
        id="s1",
        pid=7,
        window_id=9,
        app_name="Tickets",
        window_title="Order",
        nodes=nodes,
        texts=texts if texts is not None else [],
        menu=[],
        signature="sig",
        taken_at=0.0,
        ms=0,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def text(value: str, parent: int | None = None) -> TextNode:
    return TextNode(role="AXStaticText", value=value, raw=value, depth=2, parent_index=parent)


def test_build_state_keeps_the_key_order_and_an_empty_text() -> None:
    s = snap([node(0, "AXWindow", "Order"), node(1, "AXTextField", "", raw_label="Seats", value="2")], [text("Hi")])
    state = build_state("fill seats", s, "")
    assert dumps(state) == (
        '{"instruction":"fill seats","text":"","app":"Tickets","window":"Order","modal":null,'
        '"screen_text":["Hi"],"fields":[{"role":"AXTextField","label":"Seats","value":"2"}]}'
    )
    assert "text" not in build_state("x", s)


def test_build_state_names_the_modal_and_the_other_windows() -> None:
    s = snap([], modal=Modal(role="AXSheet", label="Save?", index=3), app_windows=["Draft", "Notes"])
    state = build_state("x", s)
    assert state["modal"] == {"kind": "sheet", "title": "Save?"}
    assert list(state)[-1] == "other_windows"
    assert state["other_windows"] == ["Draft", "Notes"]
    assert "other_windows" not in build_state("x", snap([], app_windows=[]))


def test_fields_are_named_by_their_label_unless_it_is_the_value_then_by_identifier() -> None:
    s = snap(
        [
            node(1, "AXTextField", "", raw_label="Seats", value="2"),
            node(2, "AXTextField", "", raw_label="2", value="2", identifier="qty"),
            node(3, "AXSearchField"),
            node(4, "AXButton", "OK"),
        ]
    )
    assert fields_of(s) == [
        {"role": "AXTextField", "label": "Seats", "value": "2"},
        {"role": "AXTextField", "label": "qty", "value": "2"},
        {"role": "AXSearchField", "label": "", "value": ""},
    ]
    assert len(fields_of(snap([node(i, "AXTextField") for i in range(20)]))) == 12


def test_controls_show_their_identifier_and_state() -> None:
    s = snap(
        [
            node(0, "AXWindow", "Order"),
            node(1, "AXCheckBox", "Gift", value="1"),
            node(2, "AXRadioButton", "Small", selected=True),
            node(3, "AXTab", "One", value="true"),
            node(4, "AXButton", "Go", identifier="go-button"),
            node(5, "AXButton", "Go", identifier="_private"),
            node(6, "AXButton", ""),
            node(7, "AXTextField", "Name"),
            node(8, "AXSwitch", "Wifi", value="0"),
        ]
    )
    assert controls_of(s) == [
        "CheckBox Gift (on)",
        "RadioButton Small (selected)",
        "Tab One (selected)",
        "Button Go (go-button)",
        "Button Go",
        "Switch Wifi",
    ]
    assert controls_of(s, 2) == ["CheckBox Gift (on)", "RadioButton Small (selected)"]


def test_change_of_lists_controls_fields_texts_and_title() -> None:
    field = node(1, "AXTextField", "", raw_label="Seats", value="120", raw_value="120")
    before = snap([node(0, "AXButton", "Save"), field], [text("a")], app_windows=["Draft"])
    after = copy.deepcopy(before)
    after.nodes[0] = node(0, "AXButton", "Saved")
    after.nodes[1].raw_value = 'a"b\n'
    after.nodes[1].value = 'a"b'
    after.texts = [text("a"), text("b"), text("b")]
    after.window_title = ""
    after.app_windows = ["Notes"]
    c = change_of(before, after)
    assert c == {
        "appeared": ["Button Saved", "Window Notes"],
        "disappeared": ["Button Save", "Window Draft"],
        "fields_changed": ['TextField "Seats": "120" \u2192 "a\\"b\\n"'],
        "texts_changed": ["b"],
        "window_title": "",
    }
    assert list(c) == ["appeared", "disappeared", "fields_changed", "texts_changed", "window_title"]
    assert changed(c)


def test_change_of_compares_menu_states_only_when_both_windows_were_in_front() -> None:
    before = snap([], menu=[MenuItem(path=["Edit", "Copy"], enabled=False, key="Edit > Copy")])
    after = snap([], menu=[MenuItem(path=["Edit", "Copy"], enabled=True, key="Edit > Copy")])
    assert "menu_items_enabled" not in change_of(before, after)
    before.frontmost = after.frontmost = True
    assert change_of(before, after)["menu_items_enabled"] == ["Copy"]


def test_changed_counts_an_empty_title_but_not_empty_lists() -> None:
    nothing: Change = {"appeared": [], "disappeared": []}
    assert not changed(nothing)
    assert changed({"appeared": [], "disappeared": [], "window_title": ""})


def test_a_flat_web_list_reads_one_row_per_line() -> None:
    snap = build_snapshot(web_list("a", [("Designer", "Open"), ("Analyst", "Closed")]), 1, 1)
    assert screen_text(snap) == ["Designer | Open", "Analyst | Closed"]
