from __future__ import annotations

import pytest

from cua_jev.driver.markdown import MdNode, parse_tree_markdown, unescape

TREE = "\n".join(
    [
        '- [0] AXWindow "計算機" [id=main actions=[raise]]',
        "  - [13] AXButton (1) [id=One actions=[press]]",
        '  - [1] AXTextArea = "alpha" [id=First Text View actions=[showmenu]]',
        '    - AXStaticText = "\u200e84"',
    ]
)


def test_parses_the_four_example_rows_with_parents() -> None:
    nodes = parse_tree_markdown(TREE)
    assert nodes == [
        MdNode(depth=0, role="AXWindow", line=0, index=0, title="計算機", identifier="main"),
        MdNode(depth=1, role="AXButton", line=1, index=13, label="1", identifier="One", parent_index=0, parent_line=0),
        MdNode(
            depth=1,
            role="AXTextArea",
            line=2,
            index=1,
            value="alpha",
            identifier="First Text View",
            parent_index=0,
            parent_line=0,
        ),
        MdNode(depth=2, role="AXStaticText", line=3, value="\u200e84", parent_index=1, parent_line=2),
    ]


def test_parent_index_skips_unindexed_rows_and_parent_line_does_not() -> None:
    md = '- [0] AXMenuBar\n  - AXMenuItem "Copy"\n    - AXStaticText = "x"\n  - [4] AXMenuItem "Paste"'
    nodes = parse_tree_markdown(md)
    assert [(n.index, n.parent_index, n.parent_line) for n in nodes] == [
        (0, None, None),
        (None, 0, 0),
        (None, 0, 1),
        (4, 0, 0),
    ]


def test_depth_follows_the_indentation_width() -> None:
    md = "- [0] AXWindow\n    - [1] AXButton\n   - [2] AXButton\n\t- [3] AXButton"
    nodes = parse_tree_markdown(md)
    assert [n.depth for n in nodes] == [0, 2, 1.5, 0.5]
    # A shallower row closes the deeper ones; a row at the same depth closes its sibling.
    assert [n.parent_line for n in nodes] == [None, 0, 0, 0]


def test_a_help_text_over_two_lines_stays_one_row() -> None:
    md = '- [2] AXButton "すべて" [help="line1\n   line2" actions=[press]]\n- [3] AXButton "next"'
    nodes = parse_tree_markdown(md)
    assert len(nodes) == 2
    assert nodes[0].help == "line1\nline2"
    assert nodes[0].identifier is None
    assert nodes[0].title == "すべて"
    assert (nodes[1].index, nodes[1].line) == (3, 2)


def test_custom_action_names_with_line_breaks_are_not_rows() -> None:
    md = "\n".join(
        [
            "- [21] AXToolbar",
            "    - [23] AXButton (モード) [id=Mode: basic; unitConversion: false actions=[increment,press,"
            "name:前に移動",
            "target:0x0",
            "selector:(null),name:ツールバーから削除",
            "target:0x0]]",
            "    - [24] AXButton",
        ]
    )
    nodes = parse_tree_markdown(md)
    assert [(n.index, n.line) for n in nodes] == [(21, 0), (23, 1), (24, 5)]
    assert nodes[1].label == "モード"
    assert nodes[1].identifier == "Mode: basic; unitConversion: false"


def test_a_label_may_run_over_lines() -> None:
    nodes = parse_tree_markdown("- [1] AXButton (first\n  second) [id=x]")
    assert nodes[0].label == "first\nsecond"
    assert nodes[0].identifier == "x"


def test_lines_before_the_first_row_and_blank_lines_are_dropped() -> None:
    md = "header\n- not a row\n\n- [1] AXButton\n\n   \n- AXStaticText"
    nodes = parse_tree_markdown(md)
    assert [(n.role, n.line) for n in nodes] == [("AXButton", 3), ("AXStaticText", 6)]


def test_a_row_needs_a_numeric_index_or_none() -> None:
    nodes = parse_tree_markdown('- [1] AXGroup\n  - [x] AXButton "a"')
    assert len(nodes) == 1
    assert nodes[0].role == "AXGroup"


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        ('"t" (l) = "v" [id=a]', {"title": "t", "label": "l", "value": "v", "identifier": "a"}),
        ('(l) "t" [id=a]', {"label": "l"}),  # a title after the label is not read, nor what follows
        ('= "v" (l) [id=a]', {"value": "v"}),
        ('[id=Delete help="x" actions=[press]]', {"identifier": "Delete", "help": "x"}),
        ("[id=closeAll: actions=[press]]", {"identifier": "closeAll:"}),
        ("[id=_NS:34]", {"identifier": "_NS:34"}),
        ("[id= ]", {"identifier": ""}),
        ("[id=]", {}),
        ("[id= actions=[press]]", {}),
        ("[actions=[press] id=late]", {"identifier": "late"}),
        ("[actions=[press]]", {}),
        ('[help="a \\"b\\" c\\\\d"]', {"help": 'a "b" c\\d'}),
        ('"a\\"b"', {"title": 'a"b'}),
        ("(no close", {}),
    ],
)
def test_title_label_value_identifier_and_help(rest: str, expected: dict[str, str]) -> None:
    (node,) = parse_tree_markdown(f"- [1] AXButton {rest}")
    fields = {k: getattr(node, k) for k in ("title", "label", "value", "identifier", "help")}
    assert {k: v for k, v in fields.items() if v is not None} == expected


def test_the_rest_may_follow_the_role_without_a_space() -> None:
    (node,) = parse_tree_markdown('- AXButtonX"t"')
    assert node.role == "AXButtonX"
    assert node.title == "t"


@pytest.mark.parametrize("terminator", ["\r", chr(0x2028), chr(0x2029)])
def test_a_backslash_before_a_line_terminator_ends_the_quoted_text(terminator: str) -> None:
    (title,) = parse_tree_markdown(f'- [1] AXButton "a\\{terminator}b"')
    assert title.title is None
    (value,) = parse_tree_markdown(f'- [1] AXButton = "a\\{terminator}b"')
    assert value.value is None
    (helped,) = parse_tree_markdown(f'- [1] AXButton [help="a\\{terminator}b"]')
    assert helped.help is None
    (plain,) = parse_tree_markdown(f'- [1] AXButton "a{terminator}b"')
    assert plain.title == f"a{terminator}b"


def test_a_backslash_before_a_line_break_ends_the_quoted_text() -> None:
    (node,) = parse_tree_markdown('- [1] AXButton "a\\\nb"')
    assert node.title is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('\\"', '"'),
        ("\\\\", "\\"),
        ("\\n", "\n"),
        ("\\\\n", "\n"),
        ("a\\tb", "a\\tb"),
        ("plain", "plain"),
    ],
)
def test_unescape(raw: str, expected: str) -> None:
    assert unescape(raw) == expected


def test_an_empty_tree_has_no_rows() -> None:
    assert parse_tree_markdown("") == []
