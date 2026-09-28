from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from beans_picker._json import JsonObject
from beans_picker.candidates.build import BuildOptions, build_candidates, candidate_id, describe_short
from beans_picker.candidates.describe import describe, may_only_open_popup
from beans_picker.candidates.keypad import compile_keypad, keypad_keys, keypad_parents
from beans_picker.candidates.menu import offerable_menu_items
from beans_picker.candidates.prune import MAX_OPTIONS, lexical_score, quoted_spans, shards
from beans_picker.candidates.safety import (
    ControlContext,
    clipboard_withheld,
    control_context,
    goal_replaces_text,
    goal_uses_clipboard,
    is_destructive_control,
    is_destructive_label,
    is_destructive_menu,
    writes_clipboard,
)
from beans_picker.candidates.types import TEXT_KINDS, ActionCandidate, ActionKind, ScrollDirection
from beans_picker.driver.types import Frame
from beans_picker.menus.keyequiv import KeyEquivalent
from beans_picker.menus.menukeys import RawMenuKey, table_from
from beans_picker.observe.snapshot import build_snapshot
from beans_picker.observe.types import MenuItem, Modal, Snapshot, UINode
from tests.helpers import snap_fixture

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"


def build(raw: JsonObject) -> Snapshot:
    return build_snapshot(raw, 1, 1)


def summaries(cands: list[ActionCandidate]) -> list[str]:
    return [c.summary for c in cands]


class TestBuildCandidates:
    def test_gives_a_control_the_same_id_in_every_snapshot_whatever_its_element_index(self) -> None:
        a = snap_fixture("calculator")
        b = copy.deepcopy(a)
        b.nodes = [replace(n, index=n.index + 100, token=f"x:{n.index + 100}") for n in b.nodes]
        assert [c.id for c in build_candidates(b)] == [c.id for c in build_candidates(a)]
        seven = next(c for c in build_candidates(a) if c.target is not None and c.target.identifier == "Seven")
        assert seven.id == candidate_id(seven.key)

    def test_lists_text_entry_actions_only_with_a_text_or_when_asked_to_list_them(self) -> None:
        te = snap_fixture("textedit")
        assert not any(c.kind in TEXT_KINDS for c in build_candidates(te))
        listed = [c for c in build_candidates(te, BuildOptions(list_text_kinds=True)) if c.kind in TEXT_KINDS]
        assert sorted(c.kind for c in listed) == ["append", "set_value", "type_into"]
        assert all(c.text is None for c in listed)
        with_text = [c for c in build_candidates(te, BuildOptions(text="  trailing  ")) if c.kind in TEXT_KINDS]
        assert all(c.text == "  trailing  " for c in with_text)
        assert [c.id for c in with_text] == [c.id for c in listed]

    def test_compiles_a_keypad_sequence_from_the_callers_text(self) -> None:
        calc = snap_fixture("calculator")
        k = next(
            c for c in build_candidates(calc, BuildOptions(text="12 \N{MULTIPLICATION SIGN} 7 =")) if c.kind == "keypad"
        )
        assert k.presses is not None
        assert [p.identifier for p in k.presses] == ["One", "Two", "Multiply", "Seven", "Equals"]
        assert not any(c.kind == "keypad" for c in build_candidates(calc, BuildOptions(text="12a")))

    def test_marks_destructive_actions_instead_of_hiding_them_and_keeps_the_keypads_delete_key_harmless(
        self,
    ) -> None:
        cands = build_candidates(snap_fixture("calculator"))
        delete = next(c for c in cands if c.target is not None and c.target.identifier == "Delete")
        assert delete.destructive is False
        by_path = {" > ".join(c.menu.path): c for c in cands if c.menu is not None}
        assert by_path["ウインドウ > すべてを閉じる"].destructive is True
        assert by_path["表示 > 桁区切り記号を非表示"].destructive is False

    def test_marks_menu_commands_without_a_known_shortcut_as_needing_the_foreground(self) -> None:
        menus = [c for c in build_candidates(snap_fixture("calculator")) if c.kind == "menu"]
        assert any(c.needs_foreground for c in menus)
        assert all(bool(c.needs_foreground) == (c.shortcut is None) for c in menus)


class TestSafetyHelpers:
    def test_treats_hide_in_the_application_menu_as_destructive_and_a_views_hide_as_not(self) -> None:
        assert is_destructive_menu(["TextEdit", "Hide TextEdit"], "TextEdit") is True
        assert is_destructive_menu(["View", "Hide Toolbar"], "TextEdit") is False
        assert is_destructive_menu(["File", "Close"], "TextEdit") is True

    def test_maps_keypad_glyphs_and_rejects_characters_without_a_key(self) -> None:
        assert keypad_keys("3\N{MINUS SIGN}1\N{DIVISION SIGN}2") == ["3", "-", "1", "/", "2"]
        assert keypad_keys("") is None
        assert keypad_keys("1,000") is None


class TestWebTables:
    def test_names_an_unnamed_row_by_its_first_text_and_says_which_row_a_look_alike_button_is_on(self) -> None:
        rows = ["INV-1034", "INV-1043"]
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "Invoices", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXWebArea",
                "label": "Invoices",
                "parent_index": 0,
                "depth": 1,
            },
            {"element_index": 2, "element_token": "t:2", "role": "AXTable", "parent_index": 1, "depth": 2},
        ]
        md = ['- [0] AXWindow "Invoices"', '  - [1] AXWebArea "Invoices"', "    - [2] AXTable"]
        for i, ident in enumerate(rows):
            b = 3 + i * 4
            elements += [
                {"element_index": b, "element_token": f"t:{b}", "role": "AXRow", "parent_index": 2, "depth": 3},
                {
                    "element_index": b + 1,
                    "element_token": f"t:{b + 1}",
                    "role": "AXCell",
                    "parent_index": b,
                    "depth": 4,
                },
                {
                    "element_index": b + 2,
                    "element_token": f"t:{b + 2}",
                    "role": "AXStaticText",
                    "value": ident,
                    "parent_index": b + 1,
                    "depth": 5,
                    "actions": ["AXShowMenu"],
                },
                {
                    "element_index": b + 3,
                    "element_token": f"t:{b + 3}",
                    "role": "AXButton",
                    "label": "Mark paid",
                    "parent_index": b,
                    "depth": 4,
                    "actions": ["AXPress"],
                },
            ]
            md += [
                f"      - [{b}] AXRow",
                f"        - [{b + 1}] AXCell",
                f'          - [{b + 2}] AXStaticText = "{ident}"',
                f'        - [{b + 3}] AXButton "Mark paid" [actions=[press]]',
            ]
        snap = build({"elements": list(elements), "tree_markdown": "\n".join(md), "window_title": "Invoices"})
        assert [t.value for t in snap.texts] == rows
        does = [c.summary for c in build_candidates(snap) if c.target is not None and c.target.label == "Mark paid"]
        assert does == ['click Button "Mark paid" in AXRow: INV-1034', 'click Button "Mark paid" in AXRow: INV-1043']

    def test_numbers_look_alike_buttons_in_order_when_no_named_container_tells_them_apart(self) -> None:
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "Cards", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXWebArea",
                "label": "Cards",
                "parent_index": 0,
                "depth": 1,
            },
            {
                "element_index": 2,
                "element_token": "t:2",
                "role": "AXButton",
                "label": "Remove",
                "parent_index": 1,
                "depth": 2,
                "actions": ["AXPress"],
            },
            {
                "element_index": 3,
                "element_token": "t:3",
                "role": "AXButton",
                "label": "Remove",
                "parent_index": 1,
                "depth": 2,
                "actions": ["AXPress"],
            },
        ]
        md = [
            '- [0] AXWindow "Cards"',
            '  - [1] AXWebArea "Cards"',
            '    - [2] AXButton "Remove" [actions=[press]]',
            '    - [3] AXButton "Remove" [actions=[press]]',
        ]
        snap = build({"elements": list(elements), "tree_markdown": "\n".join(md), "window_title": "Cards"})
        cands = [c for c in build_candidates(snap) if c.target is not None and c.target.label == "Remove"]
        assert summaries(cands) == [
            'click Button "Remove" in AXWindow: Cards (#1 of 2)',
            'click Button "Remove" in AXWindow: Cards (#2 of 2)',
        ]
        assert len({c.id for c in cands}) == 2


class TestSliders:
    def test_offers_one_step_up_and_one_step_down_each_as_an_arrow_key_sent_to_the_slider(self) -> None:
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "Sound", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXSlider",
                "label": "Volume",
                "parent_index": 0,
                "depth": 1,
            },
        ]
        md = '- [0] AXWindow "Sound"\n  - [1] AXSlider "Volume"'
        snap = build({"elements": list(elements), "tree_markdown": md, "window_title": "Sound"})
        steps = [c for c in build_candidates(snap) if c.target is not None and c.target.role == "AXSlider"]
        assert [(c.kind, c.keys) for c in steps] == [("key", ["right"]), ("key", ["left"])]


class TestNumberFields:
    def test_offers_setting_the_whole_number_and_one_step_up_or_down_with_the_arrow_keys(self) -> None:
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "Order", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXWebArea",
                "label": "Order",
                "parent_index": 0,
                "depth": 1,
            },
            {
                "element_index": 2,
                "element_token": "t:2",
                "role": "AXIncrementor",
                "label": "Quantity",
                "parent_index": 1,
                "depth": 2,
            },
        ]
        md = '- [0] AXWindow "Order"\n  - [1] AXWebArea "Order"\n    - [2] AXIncrementor "Quantity"'
        snap = build({"elements": list(elements), "tree_markdown": md, "window_title": "Order"})
        cands = [
            c
            for c in build_candidates(snap, BuildOptions(text="1"))
            if c.target is not None and c.target.role == "AXIncrementor"
        ]
        assert [(c.kind, c.keys if c.keys is not None else c.text) for c in cands] == [
            ("set_value", "1"),
            ("key", ["up"]),
            ("key", ["down"]),
        ]


class TestListBoxOptions:
    def test_offers_pressing_each_option_directly_under_a_list_and_not_the_text_inside_it(self) -> None:
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "PR", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXComboBox",
                "label": "Reviewers",
                "value": "Jordan",
                "parent_index": 0,
                "depth": 1,
            },
            {
                "element_index": 2,
                "element_token": "t:2",
                "role": "AXList",
                "label": "Suggested reviewers",
                "parent_index": 0,
                "depth": 1,
            },
            {
                "element_index": 3,
                "element_token": "t:3",
                "role": "AXStaticText",
                "value": "Jordan Lee \N{MIDDLE DOT} Mobile",
                "parent_index": 2,
                "depth": 2,
            },
            {
                "element_index": 4,
                "element_token": "t:4",
                "role": "AXStaticText",
                "value": "Jordan Lee",
                "parent_index": 3,
                "depth": 3,
            },
            {
                "element_index": 5,
                "element_token": "t:5",
                "role": "AXStaticText",
                "value": "Jordan Lee \N{MIDDLE DOT} Security",
                "parent_index": 2,
                "depth": 2,
            },
        ]
        md = "\n".join(
            [
                '- [0] AXWindow "PR"',
                '  - [1] AXComboBox "Reviewers" = "Jordan"',
                '  - [2] AXList "Suggested reviewers"',
                '    - [3] AXStaticText = "Jordan Lee \N{MIDDLE DOT} Mobile"',
                '      - [4] AXStaticText = "Jordan Lee"',
                '    - [5] AXStaticText = "Jordan Lee \N{MIDDLE DOT} Security"',
            ]
        )
        snap = build({"elements": list(elements), "tree_markdown": md, "window_title": "PR"})
        options = [c for c in build_candidates(snap) if c.target is not None and c.target.role == "AXStaticText"]
        assert [(c.kind, c.summary) for c in options] == [
            ("click", 'choose option "Jordan Lee \N{MIDDLE DOT} Mobile" in List "Suggested reviewers"'),
            ("click", 'choose option "Jordan Lee \N{MIDDLE DOT} Security" in List "Suggested reviewers"'),
        ]


class TestScrolling:
    def test_offers_a_page_down_and_up_on_areas_that_scroll_and_have_a_frame(self) -> None:
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "Ledger", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXTable",
                "label": "Transactions",
                "parent_index": 0,
                "depth": 1,
                "frame": {"x": 0, "y": 0, "w": 400, "h": 300},
            },
            {
                "element_index": 2,
                "element_token": "t:2",
                "role": "AXList",
                "label": "Tags",
                "parent_index": 0,
                "depth": 1,
            },
        ]
        md = '- [0] AXWindow "Ledger"\n  - [1] AXTable "Transactions"\n  - [2] AXList "Tags"'
        snap = build({"elements": list(elements), "tree_markdown": md, "window_title": "Ledger"})
        scrolls = [c for c in build_candidates(snap) if c.kind == "scroll"]
        assert [(c.summary, c.direction) for c in scrolls] == [
            ('scroll Table "Transactions" down one page', "down"),
            ('scroll Table "Transactions" up one page', "up"),
        ]


class TestContextMenus:
    def test_offers_opening_a_named_rows_context_menu_and_shift_f10_for_the_focused_item(self) -> None:
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "title": "Drive", "depth": 0},
            {
                "element_index": 1,
                "element_token": "t:1",
                "role": "AXOutline",
                "label": "Folders",
                "parent_index": 0,
                "depth": 1,
            },
            {
                "element_index": 2,
                "element_token": "t:2",
                "role": "AXRow",
                "label": "Clients",
                "parent_index": 1,
                "depth": 2,
            },
            {"element_index": 3, "element_token": "t:3", "role": "AXRow", "parent_index": 1, "depth": 2},
        ]
        md = '- [0] AXWindow "Drive"\n  - [1] AXOutline "Folders"\n    - [2] AXRow "Clients"\n    - [3] AXRow'
        snap = build({"elements": list(elements), "tree_markdown": md, "window_title": "Drive"})
        cands = build_candidates(snap)
        assert [c.summary for c in cands if c.kind == "context_menu"] == [
            'open the context menu of Row "Clients" (right-click)'
        ]
        shift_f10 = next(c for c in cands if c.summary.startswith("press Shift+F10"))
        assert shift_f10.keys == ["shift", "f10"]


def node(
    index: int,
    role: str,
    label: str = "",
    *,
    parent: int | None = None,
    identifier: str | None = None,
    raw_label: str | None = None,
    value: str | None = None,
    help: str | None = None,
    actions: list[str] | None = None,
    within: list[str] | None = None,
    depth: float = 2,
    enabled: bool = True,
    token: str | None = None,
    frame: Frame | None = None,
    subrole: str | None = None,
    in_menu_bar: bool = False,
) -> UINode:
    return UINode(
        index=index,
        token=token if token is not None else f"t:{index}",
        role=role,
        label=label,
        enabled=enabled,
        actions=actions if actions is not None else [],
        depth=depth,
        in_menu_bar=in_menu_bar,
        within=within if within is not None else [],
        key=f"{role}|{identifier or ''}|{label}",
        subrole=subrole,
        raw_label=raw_label if raw_label is not None else (label or None),
        value=value,
        identifier=identifier,
        help=help,
        frame=frame,
        parent=parent,
    )


def item(*path: str, enabled: bool = True, pressable: bool = True, identifier: str | None = None) -> MenuItem:
    return MenuItem(
        path=list(path),
        enabled=enabled,
        key="menu|" + "|".join(path),
        token="m" if pressable else None,
        identifier=identifier,
    )


def snap_of(nodes: list[UINode], menu: list[MenuItem] | None = None, *, modal: Modal | None = None) -> Snapshot:
    return Snapshot(
        id="s1",
        pid=7,
        window_id=1,
        app_name="App",
        window_title="Window",
        nodes=nodes,
        texts=[],
        menu=menu if menu is not None else [],
        signature="sig",
        taken_at=0,
        ms=0,
        modal=modal,
    )


WINDOW = node(0, "AXWindow", "Window", depth=0)


def keypad_nodes(parent: int = 1) -> list[UINode]:
    names = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
    return [node(10 + d, "AXButton", str(d), identifier=names[d], parent=parent, actions=["press"]) for d in range(10)]


class TestDestructiveLabels:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Delete", True),
            ("Resend", True),
            ("Log out", True),
            ("Logout", True),
            ("Don\N{RIGHT SINGLE QUOTATION MARK}t Save", True),
            ("Replace", True),
            (f"Replace{ELLIPSIS}", False),
            ("Replace...", False),
            ("Replacement", False),
            ("置換replace", True),
            ("\N{LATIN SMALL LETTER LONG S}hare", False),
            ("Hidden", False),
            ("保存しない", True),
            ("Open", False),
        ],
    )
    def test_is_destructive_label(self, text: str, expected: bool) -> None:
        assert is_destructive_label(text) is expected


class TestDestructiveControls:
    def test_a_views_own_hide_is_harmless_but_hide_alone_or_hide_others_is_not(self) -> None:
        assert is_destructive_control(node(1, "AXButton", "Hide Binary")) is False
        assert is_destructive_control(node(1, "AXButton", "バイナリを非表示")) is False
        assert is_destructive_control(node(1, "AXButton", "Hide")) is True
        assert is_destructive_control(node(1, "AXButton", "Hide Others")) is True
        assert is_destructive_control(node(1, "AXButton", "Toggle", help="Hide")) is True

    def test_closing_mark_keys_close_nothing(self) -> None:
        assert is_destructive_control(node(1, "AXButton", "Close Paren")) is False
        assert is_destructive_control(node(1, "AXButton", "Right Parenthesis")) is False
        assert is_destructive_control(node(1, "AXButton", "close-brackets")) is False
        assert is_destructive_control(node(1, "AXButton", "閉じ丸括弧")) is False
        assert is_destructive_control(node(1, "AXButton", "Close")) is True

    def test_text_fields_are_never_destructive(self) -> None:
        assert is_destructive_control(node(1, "AXTextField", "Delete")) is False
        assert is_destructive_control(node(1, "AXComboBox", "Replace")) is False

    def test_keypad_delete_and_clear_edit_only_the_display(self) -> None:
        ctx = ControlContext(keypad_parents=frozenset({1}))
        assert is_destructive_control(node(20, "AXButton", "Delete", parent=1), ctx) is False
        assert is_destructive_control(node(20, "AXButton", "AC clear", parent=1), ctx) is False
        assert is_destructive_control(node(20, "AXButton", "Delete", parent=2), ctx) is True
        assert is_destructive_control(node(20, "AXButton", "Clear and send", parent=1), ctx) is True

    def test_a_popover_close_button_only_dismisses_it(self) -> None:
        nodes = [WINDOW, node(1, "AXPopover", "Info", parent=0), node(2, "AXButton", " Close ", parent=1)]
        ctx = control_context(snap_of(nodes))
        assert is_destructive_control(nodes[2], ctx) is False
        assert is_destructive_control(replace(nodes[2], help="Discard changes"), ctx) is True
        assert is_destructive_control(replace(nodes[2], label="Close all"), ctx) is True
        sheet = [WINDOW, node(1, "AXSheet", "Save", parent=0), node(2, "AXButton", "Close", parent=1)]
        assert is_destructive_control(sheet[2], control_context(snap_of(sheet))) is True

    def test_replace_is_what_a_replace_goal_asks_for_outside_a_dialog(self) -> None:
        nodes = [
            WINDOW,
            node(1, "AXGroup", "Find bar", parent=0),
            node(2, "AXButton", "Replace All", parent=1),
            node(3, "AXSheet", "Save", parent=0),
            node(4, "AXGroup", "", parent=3),
            node(5, "AXButton", "Replace", parent=4),
            node(6, "AXGroup", "Panel", parent=0, subrole="AXSystemDialog"),
            node(7, "AXButton", "Replace", parent=6),
        ]
        snap = snap_of(nodes)
        goal = 'replace "cat" with "dog"'
        ctx = control_context(snap, goal)
        assert ctx.replaces_text is True
        assert ctx.in_dialog == frozenset({4, 5, 7})
        assert is_destructive_control(nodes[2], ctx) is False
        assert is_destructive_control(nodes[5], ctx) is True
        assert is_destructive_control(nodes[7], ctx) is True
        assert is_destructive_control(nodes[2], control_context(snap, "click the button")) is True
        assert is_destructive_control(nodes[2], control_context(snap)) is True
        assert control_context(snap, "click").in_dialog == frozenset()

    def test_is_destructive_menu_keeps_hide_in_the_application_menu(self) -> None:
        assert is_destructive_menu(["Safari", "Hide Safari"], "Safari") is True
        assert is_destructive_menu(["View", "Hide Sidebar"]) is False
        assert is_destructive_menu(["Window", "Hide Others"], "Safari") is True
        assert is_destructive_menu(["表示", "ツールバーを非表示"], "App") is False
        assert is_destructive_menu([]) is False


class TestGoals:
    @pytest.mark.parametrize(
        ("goal", "expected"),
        [
            ('replace "cat" with "dog"', True),
            ("Replacing the title", True),
            ("replaced", False),
            ('change "a" to "b"', True),
            ("change \N{LEFT DOUBLE QUOTATION MARK}a\N{RIGHT DOUBLE QUOTATION MARK} into 「b」", True),
            ("change the colour to blue", False),
            ("猫を犬に置き換えて", True),
            ("猫を犬に置換えて", True),
            ("置換して", True),
        ],
    )
    def test_goal_replaces_text(self, goal: str, expected: bool) -> None:
        assert goal_replaces_text(goal) is expected

    @pytest.mark.parametrize(
        ("goal", "expected"),
        [
            ("copy the address", True),
            ("Paste it", True),
            ("copyright notice", False),
            ("move the file", True),
            ("コピーして", True),
            ("save the file", False),
        ],
    )
    def test_goal_uses_clipboard(self, goal: str, expected: bool) -> None:
        assert goal_uses_clipboard(goal) is expected

    def test_writes_clipboard(self) -> None:
        assert writes_clipboard("Copy") is True
        assert writes_clipboard(f" cut{ELLIPSIS} ") is True
        assert writes_clipboard("Copy Style") is False

    def test_clipboard_withheld_uses_the_english_name(self) -> None:
        assert clipboard_withheld(["編集", "コピー"], "save the file") is True
        assert clipboard_withheld(["編集", "コピー"], "copy the title") is False
        assert clipboard_withheld(["Edit", "Copy Style"], "save") is False
        assert clipboard_withheld([], "save") is False
        learned = table_from(
            [RawMenuKey(path=["編集", "切り取り"], key="x", mods=["cmd"], top=2)],
            [RawMenuKey(path=["Edit", "Cut"], key="x", mods=["cmd"], top=2)],
        )
        assert clipboard_withheld(["編集", "切り取り"], "save", learned) is True
        assert clipboard_withheld(["編集", "切り取り"], "save") is False


class TestOfferableMenuItems:
    def test_skips_system_menus_submenu_openers_and_placeholders(self) -> None:
        menu = [
            item("Apple", "About This Mac"),
            item("App", "About App", identifier="orderFrontStandardAboutPanel:"),
            item("App", "Hide App", identifier="hide:"),
            item("App", "Quit App", identifier="terminate:"),
            item("App", "Settings"),
            item("File", "Open Recent", "Doc.txt"),
            item("File", "Open Recent"),
            item("File", "Export", "PDF"),
            item("File", "<<Placeholder>>"),
            item("File", "File"),
            item("Help", "Search"),
            item("ヘルプ", "Guide"),
            item("Help", "Topics", "One"),
            item("Edit", "Services", "Look Up"),
            item("Edit", "Speech", "Start Speaking"),
            item("Edit", "Undo"),
            item("Edit", "Delete"),
            item("Edit", "Paste", pressable=False),
            item("Edit", "Bold", enabled=False),
            item("Edit", "Italic", enabled=False, pressable=False),
        ]
        offered = [m.path for m in offerable_menu_items(menu)]
        assert offered == [
            ["App", "About App"],
            ["File", "Export", "PDF"],
            ["Help", "Topics", "One"],
            ["Edit", "Undo"],
        ]
        with_all = [m.path for m in offerable_menu_items(menu, allow_destructive=True, include_disabled=True)]
        assert with_all == [
            ["App", "About App"],
            ["File", "Export", "PDF"],
            ["Help", "Topics", "One"],
            ["Edit", "Undo"],
            ["Edit", "Delete"],
            ["Edit", "Bold"],
            ["Edit", "Italic"],
        ]

    def test_without_stock_identifiers_the_application_menu_is_kept(self) -> None:
        assert [m.path for m in offerable_menu_items([item("App", "Settings")])] == [["App", "Settings"]]


class TestKeypad:
    def test_keypad_keys(self) -> None:
        assert keypad_keys("3x4") == ["3", "*", "4"]
        assert keypad_keys("12\N{MULTIPLICATION SIGN}7=") == ["1", "2", "*", "7", "="]
        assert keypad_keys("\N{SQUARE ROOT}9") == ["\N{SQUARE ROOT}", "9"]
        assert keypad_keys("  \t") is None
        assert keypad_keys("abc") is None
        assert keypad_keys("3X4") is None
        assert keypad_keys("\N{FULLWIDTH DIGIT ONE}") is None

    def test_identifiers_win_over_labels_and_labels_match_ignoring_case(self) -> None:
        by_label = node(1, "AXButton", "Add", raw_label="ADD")
        by_id = node(2, "AXButton", "plus key", identifier="Plus")
        glyph = node(3, "AXButton", "\N{MULTIPLICATION SIGN}")
        digit = node(4, "AXButton", "5")
        snap = snap_of([by_label, by_id, glyph, digit])
        assert compile_keypad(["+", "*", "5"], snap) == [by_id, glyph, digit]
        assert compile_keypad(["+"], snap_of([by_label])) == [by_label]
        assert compile_keypad(["9"], snap) is None

    def test_disabled_and_menu_bar_buttons_are_not_pressed(self) -> None:
        assert compile_keypad(["5"], snap_of([node(1, "AXButton", "5", enabled=False)])) is None
        assert compile_keypad(["5"], snap_of([node(1, "AXButton", "5", in_menu_bar=True)])) is None
        assert compile_keypad(["5"], snap_of([node(1, "AXStaticText", "5")])) is None

    def test_keypad_parents_need_eight_distinct_digits(self) -> None:
        assert keypad_parents(snap_of(keypad_nodes())) == {1}
        assert keypad_parents(snap_of(keypad_nodes()[:7])) == set()
        by_label = [node(30 + d, "AXButton", str(d), parent=2) for d in range(8)]
        assert keypad_parents(snap_of(by_label)) == {2}
        blank = [node(30 + d, "AXButton", str(d), raw_label="", parent=2) for d in range(8)]
        assert keypad_parents(snap_of(blank)) == set()
        assert keypad_parents(snap_of([replace(n, parent=None) for n in keypad_nodes()])) == set()


def cand(
    kind: ActionKind = "click",
    *,
    key: str = "k",
    summary: str = "",
    lexical: int = 0,
    target: UINode | None = None,
    menu: MenuItem | None = None,
    keys: list[str] | None = None,
    presses: list[UINode] | None = None,
    direction: ScrollDirection | None = None,
    shortcut: KeyEquivalent | None = None,
    needs_foreground: bool | None = None,
) -> ActionCandidate:
    return ActionCandidate(
        id="",
        kind=kind,
        key=key,
        summary=summary,
        lexical=lexical,
        target=target,
        menu=menu,
        keys=keys,
        presses=presses,
        direction=direction,
        shortcut=shortcut,
        needs_foreground=needs_foreground,
    )


class TestPrune:
    def test_max_options(self) -> None:
        assert MAX_OPTIONS == 60

    def test_quoted_spans(self) -> None:
        assert quoted_spans('click "Save" and \N{LEFT DOUBLE QUOTATION MARK}Open\N{RIGHT DOUBLE QUOTATION MARK}') == [
            "save",
            "open",
        ]
        assert quoted_spans("押す「保存」") == ["保存"]
        assert quoted_spans("don't press it's") == ["t press it"]
        assert quoted_spans('empty "" quotes') == []

    def test_lexical_score_of_a_control(self) -> None:
        target = node(
            1,
            "AXButton",
            "Save file",
            identifier="saveButton",
            help="Save the file",
            within=["AXGroup: Save area"],
            value="save",
        )
        c = cand(target=target)
        # label 3*2 + identifier 2*0 ("savebutton") + help 2*2 + within 1 + value 1
        assert lexical_score(c, "save file") == 6 + 0 + 4 + 1 + 1
        assert lexical_score(c, '"Save file"') == 6 + 4 + 1 + 1 + 5
        assert lexical_score(c, "the") == 0
        assert lexical_score(c, "") == 0

    def test_lexical_score_of_a_menu_item_counts_its_english_name(self) -> None:
        c = cand("menu", menu=item("フォーマット", "標準テキストにする"))
        assert lexical_score(c, "make plain text") == 3 * 3
        assert lexical_score(c, "「標準テキストにする」") == 3 + 1 + 5
        english = cand("menu", menu=item("Format", "Make Plain Text"))
        assert lexical_score(english, "make plain text") == 3 * 3 + 3

    def test_lexical_score_counts_the_summary_of_keys_scrolls_and_context_menus(self) -> None:
        assert lexical_score(cand("key", summary="press Escape"), "press escape") == 2
        assert lexical_score(cand("scroll", summary="scroll List down one page"), "scroll down") == 2
        assert lexical_score(cand("click", summary="click Button"), "click") == 0

    def test_shards_order_by_score_keeping_ties_in_input_order(self) -> None:
        cands = [cand(key=str(i), lexical=i % 3) for i in range(7)]
        out = shards(cands, 3)
        assert [[c.key for c in s] for s in out] == [["2", "5", "1"], ["4", "0", "3"], ["6"]]
        assert shards([]) == []
        assert len(shards([cand(key=str(i)) for i in range(125)])) == 3


class TestDescribe:
    def test_a_control_in_key_order(self) -> None:
        target = node(
            2,
            "AXPopUpButton",
            "Size",
            identifier="sizePopUp",
            help="Pick a size",
            value="",
            within=["a", "b", "c", "d"],
            actions=["showmenu", "cancel"],
        )
        twin = node(3, "AXPopUpButton", "Size")
        c = cand(target=target)
        assert may_only_open_popup(c) is True
        assert describe(c, snap_of([WINDOW, twin, target])) == {
            "action": "click",
            "role": "AXPopUpButton",
            "name": "Size",
            "identifier": "sizePopUp",
            "help": "Pick a size",
            "current_value": "",
            "within": ["a", "b", "c"],
            "occurrence": "2 of 2",
            "pop_up": "pressing it may only open a pop-up menu whose choices are not in this list",
            "effect_hint": "presses this control",
        }
        assert list(describe(c, snap_of([target, twin]))) == [
            "action",
            "role",
            "name",
            "identifier",
            "help",
            "current_value",
            "within",
            "occurrence",
            "pop_up",
            "effect_hint",
        ]

    def test_private_identifiers_help_equal_to_the_name_and_long_names_are_trimmed(self) -> None:
        target = node(1, "AXButton", "x" * 100, identifier="_NS:12", help="x" * 100)
        d = describe(cand("toggle", target=target), snap_of([target]))
        assert d == {
            "action": "toggle",
            "role": "AXButton",
            "name": "x" * 79 + ELLIPSIS,
            "effect_hint": "switches this control to the other state",
        }
        assert may_only_open_popup(cand("toggle", target=node(1, "AXButton", actions=["showmenu", "cancel"]))) is False

    def test_a_menu_command(self) -> None:
        menu = [item("表示", "アイコン"), item("表示", "リスト"), item("編集", "ペースト")]
        views = cand("menu", menu=menu[0], needs_foreground=True)
        assert describe(views, snap_of([], menu)) == {
            "action": "menu",
            "path": "表示 > アイコン",
            "view": "one of the window's views (アイコン, リスト); switching views changes which controls are shown",
            "availability": "cannot run while the app stays in the background (no keyboard shortcut)",
            "effect_hint": "chooses this menu command",
        }
        paste = cand("menu", menu=menu[2], shortcut=KeyEquivalent(keys=["cmd", "v"], source="standard"))
        assert describe(paste, snap_of([], menu)) == {
            "action": "menu",
            "path": "編集 > ペースト",
            "standard_command": "Paste (the stock macOS command this localized item is)",
            "effect_hint": "chooses this menu command",
        }

    def test_a_browsers_menu_command_says_it_is_not_the_page(self) -> None:
        settings = item("Chrome", "設定…")
        d = describe(cand("menu", menu=settings), snap_of([node(1, "AXWebArea", "Profile")], [settings]))
        assert d["scope"] == "the browser's own menu bar, not the web page shown in the window"
        assert "scope" not in describe(cand("menu", menu=settings), snap_of([], [settings]))

    def test_the_view_hint_is_trimmed_to_200(self) -> None:
        menu = [item("View", f"Mode number {i:02d} with a long name") for i in range(9)]
        d = describe(cand("menu", menu=menu[0]), snap_of([], menu))
        view = d["view"]
        assert isinstance(view, str)
        assert len(view) == 200
        assert view.endswith(ELLIPSIS)

    def test_keypad_key_and_scroll(self) -> None:
        presses = [node(1, "AXButton", "1", identifier="One"), node(2, "AXButton", "\N{MULTIPLICATION SIGN}")]
        keypad = cand("keypad", keys=["1", "*"], presses=presses)
        assert describe(keypad, snap_of([])) == {
            "action": "keypad",
            "sequence": "1\N{MULTIPLICATION SIGN}",
            "via": ["One", "\N{MULTIPLICATION SIGN}"],
            "effect_hint": "presses the on-screen keys for `text`, in order",
        }
        assert describe(cand("keypad"), snap_of([])) == {
            "action": "keypad",
            "effect_hint": "presses the on-screen keys for `text`, in order",
        }
        assert describe(cand("key", keys=["shift", "f10"]), snap_of([]))["keys"] == "shift+f10"
        assert describe(cand("key"), snap_of([]))["keys"] == ""
        assert describe(cand("scroll"), snap_of([]))["direction"] == "down"
        assert describe(cand("scroll", direction="up"), snap_of([]))["direction"] == "up"

    def test_every_kind_has_an_effect_hint(self) -> None:
        kinds: list[ActionKind] = ["click", "toggle", "choose_option", "set_value", "type_into", "append"]
        for kind in [*kinds, "menu", "keypad", "key", "scroll", "context_menu"]:
            assert describe(cand(kind), snap_of([]))["effect_hint"]
        assert describe(cand("context_menu"), snap_of([]))["effect_hint"] == (
            "opens this item's context menu, as a right-click does; its commands (rename, star, move to trash "
            f"{ELLIPSIS}) then appear as menu items to press"
        )


class TestBuild:
    def test_describe_short(self) -> None:
        assert describe_short(node(1, "AXButton", "Save", identifier="saveBtn")) == 'Button "Save" (saveBtn)'
        assert describe_short(node(1, "AXTextField")) == "TextField"
        assert describe_short(node(1, "AXButton", "OK", identifier="OK")) == 'Button "OK"'
        assert describe_short(node(1, "Custom", "a" * 50)) == f'Custom "{"a" * 39}{ELLIPSIS}"'

    def test_candidate_id_hashes_the_key(self) -> None:
        assert candidate_id("click|x") == candidate_id("click|x")
        assert candidate_id("click|x") != candidate_id("click|x#2")
        assert candidate_id("k") == "c8254c329a92850f6d539dd376f4816ee"
        assert candidate_id("\ud800") == candidate_id("\N{REPLACEMENT CHARACTER}")

    def test_controls_in_a_fixed_order_with_safe_keys_last(self) -> None:
        nodes = [
            WINDOW,
            node(1, "AXButton", "", depth=1),
            node(2, "AXScrollArea", "Area", frame=Frame(x=0, y=0, w=10, h=10)),
            node(3, "AXCheckBox", "Bold", value="1"),
            node(4, "AXCheckBox", "Italic", value="0"),
            node(5, "AXCheckBox", "Other"),
            node(6, "AXImage", "Logo"),
            node(7, "AXImage", "Photo", actions=["press"]),
            node(8, "AXButton", "Off", enabled=False),
            node(9, "AXButton", "No token", token=""),
            node(10, "AXTextArea", "Body"),
            node(11, "AXTextField", "Name"),
        ]
        cands = build_candidates(snap_of(nodes))
        assert summaries(cands) == [
            'scroll ScrollArea "Area" down one page',
            'scroll ScrollArea "Area" up one page',
            'turn off CheckBox "Bold"',
            'turn on CheckBox "Italic"',
            'switch CheckBox "Other"',
            'open the context menu of Image "Logo" (right-click)',
            'click Image "Photo"',
            'open the context menu of Image "Photo" (right-click)',
            'click TextField "Name"',
            "press Return",
            "press Escape",
            "press Tab",
            "press Space",
            "press Up arrow",
            "press Down arrow",
            "press Left arrow",
            "press Right arrow",
            "press Shift+F10 (opens the focused item's context menu)",
        ]
        assert [c.key for c in cands[-9:]][::4] == ["key|return", "key|up", "key|shift+f10"]
        assert all(c.id == candidate_id(c.key) for c in cands)
        assert [c.summary for c in cands if c.destructive] == ["press Return", "press Space"]

    def test_text_kinds_carry_the_text_and_quote_it_as_json(self) -> None:
        nodes = [WINDOW, node(1, "AXTextField", "Name"), node(2, "AXPopUpButton", "Size")]
        cands = build_candidates(snap_of(nodes), BuildOptions(text='say "hi"\n'))
        assert [(c.kind, c.key.split("|")[0], c.summary) for c in cands[:5]] == [
            ("set_value", "set_value", 'replace the whole text of TextField "Name" with "say \\"hi\\"\\n"'),
            ("type_into", "type_into", 'type "say \\"hi\\"\\n" into TextField "Name" at its caret'),
            ("append", "append", 'add "say \\"hi\\"\\n" at the end of TextField "Name"'),
            ("click", "click", 'click TextField "Name"'),
            ("choose_option", "choose", 'choose "say \\"hi\\"\\n" in PopUpButton "Size"'),
        ]
        assert [c.text for c in cands[:5]] == ['say "hi"\n', 'say "hi"\n', 'say "hi"\n', None, 'say "hi"\n']
        long = build_candidates(snap_of(nodes), BuildOptions(text="y" * 70))[0]
        assert long.summary.endswith(f'"{"y" * 59}{ELLIPSIS}"')
        listed = build_candidates(snap_of(nodes), BuildOptions(list_text_kinds=True))
        assert listed[4].summary == 'choose the given text in PopUpButton "Size"'
        assert listed[4].text is None

    def test_an_empty_text_is_a_text(self) -> None:
        nodes = [WINDOW, node(1, "AXTextField", "Name")]
        cands = build_candidates(snap_of(nodes), BuildOptions(text=""))
        assert cands[0].kind == "set_value"
        assert cands[0].text == ""
        assert cands[0].summary == 'replace the whole text of TextField "Name" with ""'

    def test_a_modal_takes_the_input(self) -> None:
        nodes = [
            WINDOW,
            node(1, "AXButton", "Behind", parent=0),
            node(2, "AXTextField", "Search", parent=0),
            node(3, "AXSheet", "Save", parent=0),
            node(4, "AXButton", "Cancel", parent=3),
        ]
        menu = [item("Edit", "Undo")]
        cands = build_candidates(snap_of(nodes, menu, modal=Modal(role="AXSheet", label="Save", index=3)))
        assert summaries(cands)[:2] == ['click TextField "Search"', 'click Button "Cancel"']
        assert not any(c.kind == "menu" for c in cands)
        assert cands[-1].keys == ["shift", "f10"]

    def test_chrome_is_skipped(self) -> None:
        nodes = [
            WINDOW,
            node(1, "AXGroup", "Group"),
            node(2, "AXToolbar", "Toolbar"),
            node(3, "AXButton", "", depth=1),
            node(4, "AXButton", "", depth=1, identifier="close"),
            node(5, "AXButton", "", depth=2),
        ]
        assert summaries(build_candidates(snap_of(nodes)))[:2] == ["click Button (close)", "click Button"]

    def test_menu_commands_with_shortcuts_destructive_flags_and_withheld_clipboard(self) -> None:
        menu = [
            item("Apple", "About This Mac"),
            item("TextEdit", "Hide Others", identifier="hideOtherApplications:"),
            item("TextEdit", "Settings", identifier="showSettingsWindow:"),
            item("TextEdit", "Services"),
            item("File", "Close"),
            item("Edit", "Copy"),
            item("Edit", "Undo"),
            item("View", "Hide Toolbar"),
            item("Format", "Bigger"),
        ]
        learned = table_from([RawMenuKey(path=["Format", "Bigger"], key="+", mods=["cmd"], top=4)])
        cands = [c for c in build_candidates(snap_of([WINDOW], menu), BuildOptions(learned=learned)) if c.menu]
        assert [(c.summary, c.destructive, c.shortcut, c.needs_foreground) for c in cands] == [
            ("menu TextEdit > Settings", False, None, True),
            ("menu File > Close", True, None, True),
            ("menu Edit > Copy", False, KeyEquivalent(keys=["cmd", "c"], source="standard"), None),
            ("menu Edit > Undo", False, KeyEquivalent(keys=["cmd", "z"], source="standard"), None),
            ("menu View > Hide Toolbar", False, KeyEquivalent(keys=["cmd", "option", "t"], source="standard"), None),
            ("menu Format > Bigger", False, KeyEquivalent(keys=["cmd", "+"], source="learned"), None),
        ]
        assert cands[0].key == "menu|TextEdit|Settings"
        with_goal = build_candidates(snap_of([WINDOW], menu), BuildOptions(instruction="undo the edit"))
        assert "menu Edit > Copy" not in summaries(with_goal)
        assert "menu Edit > Copy" in summaries(
            build_candidates(snap_of([WINDOW], menu), BuildOptions(instruction="copy"))
        )

    def test_the_application_menu_is_the_first_after_apples(self) -> None:
        menu = [item("Apple", "About"), item("Safari", "Hide Safari"), item("View", "Hide Sidebar")]
        cands = [c for c in build_candidates(snap_of([WINDOW], menu)) if c.menu]
        assert [(c.summary, c.destructive) for c in cands] == [
            ("menu Safari > Hide Safari", True),
            ("menu View > Hide Sidebar", False),
        ]

    def test_a_keypad_listed_without_text(self) -> None:
        snap = snap_of([WINDOW, node(1, "AXGroup", "Keypad"), *keypad_nodes()])
        listed = [c for c in build_candidates(snap, BuildOptions(list_text_kinds=True)) if c.kind == "keypad"]
        assert len(listed) == 1
        assert listed[0].summary == "press the on-screen keys for the given text (digits and operators) in order"
        assert listed[0].keys is None
        assert listed[0].presses is None
        assert not any(c.kind == "keypad" for c in build_candidates(snap))
        typed = [c for c in build_candidates(snap, BuildOptions(text="42")) if c.kind == "keypad"]
        assert typed[0].summary == 'press the on-screen keys "42" in order'
        assert typed[0].keys == ["4", "2"]
        assert typed[0].text == "42"
        assert typed[0].presses is not None
        assert [p.identifier for p in typed[0].presses] == ["Four", "Two"]

    def test_look_alike_controls_get_their_own_keys_and_ids(self) -> None:
        nodes = [
            WINDOW,
            node(1, "AXButton", "Add", within=["AXRow: One"]),
            node(2, "AXButton", "Add", within=["AXRow: Two"]),
            node(3, "AXButton", "Add", within=["AXRow: Two"]),
        ]
        cands = build_candidates(snap_of(nodes))[:3]
        assert [c.key for c in cands] == ["click|AXButton||Add", "click|AXButton||Add#2", "click|AXButton||Add#3"]
        assert summaries(cands) == [
            'click Button "Add" in AXRow: One',
            'click Button "Add" in AXRow: Two (#1 of 2)',
            'click Button "Add" in AXRow: Two (#2 of 2)',
        ]
        assert len({c.id for c in cands}) == 3

    def test_lexical_scores_follow_the_instruction(self) -> None:
        nodes = [WINDOW, node(1, "AXButton", "Save"), node(2, "AXButton", "Open")]
        cands = build_candidates(snap_of(nodes), BuildOptions(instruction='press "Save"'))
        assert [(c.summary, c.lexical) for c in cands[:2]] == [('click Button "Save"', 8), ('click Button "Open"', 0)]
        assert all(c.lexical == 0 for c in build_candidates(snap_of(nodes))[:2])

    def test_destructive_controls_are_flagged(self) -> None:
        nodes = [WINDOW, node(1, "AXButton", "Delete"), node(2, "AXPopUpButton", "Remove")]
        cands = build_candidates(snap_of(nodes), BuildOptions(text="x"))
        assert [(c.kind, c.destructive) for c in cands[:3]] == [
            ("click", True),
            ("choose_option", True),
            ("click", True),
        ]


class TestPageTexts:
    def _page(self) -> Snapshot:
        f: JsonObject = {"x": 10, "y": 10, "w": 40, "h": 14}
        elements: list[JsonObject] = [
            {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "label": "Tags", "depth": 0},
            {"element_index": 1, "element_token": "t:1", "role": "AXWebArea", "parent_index": 0, "depth": 1},
            {
                "element_index": 2,
                "element_token": "t:2",
                "role": "AXButton",
                "label": "Apply",
                "parent_index": 1,
                "depth": 2,
            },
            {
                "element_index": 3,
                "element_token": "t:3",
                "role": "AXStaticText",
                "label": "Apply",
                "parent_index": 2,
                "depth": 3,
                "frame": f,
            },
            {
                "element_index": 4,
                "element_token": "t:4",
                "role": "AXStaticText",
                "label": "Referral",
                "parent_index": 1,
                "depth": 2,
                "frame": f,
            },
            {
                "element_index": 5,
                "element_token": "t:5",
                "role": "AXStaticText",
                "label": "Open",
                "parent_index": 1,
                "depth": 2,
                "frame": f,
            },
            {
                "element_index": 6,
                "element_token": "t:6",
                "role": "AXStaticText",
                "label": "Open",
                "parent_index": 1,
                "depth": 2,
                "frame": f,
            },
        ]
        md = [
            '- [0] AXWindow "Tags"',
            '  - [1] AXWebArea "Tags"',
            '    - [2] AXButton "Apply" [actions=[press]]',
            '      - [3] AXStaticText = "Apply"',
            '    - [4] AXStaticText = "Referral"',
            '    - [5] AXStaticText = "Open"',
            '    - [6] AXStaticText = "Open"',
        ]
        return build({"elements": list(elements), "tree_markdown": "\n".join(md), "window_title": "Tags"})

    def test_a_pages_lone_short_texts_are_clickable_after_every_control(self) -> None:
        cands = [c for c in build_candidates(self._page()) if c.kind == "click"]
        assert summaries(cands) == ['click Button "Apply"', 'click the text "Referral"']

    def test_a_text_that_reads_as_destructive_asks_first(self) -> None:
        snap = self._page()
        referral = next(n for n in snap.nodes if n.label == "Referral")
        referral.help = "Permanently delete the account"
        (text,) = [c for c in build_candidates(snap) if "the text" in c.summary]
        assert text.destructive

    def test_texts_outside_a_web_page_are_not_offered(self) -> None:
        snap = self._page()
        snap.nodes = [n for n in snap.nodes if n.role != "AXWebArea"]
        for n in snap.nodes:
            if n.parent == 1:
                n.parent = 0
        assert not any("the text" in c.summary for c in build_candidates(snap))
