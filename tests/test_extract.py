from __future__ import annotations

from pathlib import Path

from cua_jev._json import JsonValue, dumps
from cua_jev.observe.snapshot import build_snapshot
from cua_jev.observe.types import Snapshot, UINode
from cua_jev.tools.args import ExtractArgs
from cua_jev.tools.extract import content_of, describe_element, elements_of, extract_tool, extracted
from tests.test_state import el, node, snap
from tests.tool_fakes import FakeSession, jev_picking


def page() -> Snapshot:
    elements: list[JsonValue] = [
        el(0, "AXWindow", title="Ledger"),
        el(1, "AXTable", 0, 1, label="Transactions"),
        el(2, "AXRow", 1, 2),
        el(3, "AXCell", 2, 3),
        el(4, "AXCell", 2, 3),
        el(5, "AXRow", 1, 2),
        el(6, "AXCell", 5, 3),
        el(7, "AXList", 0, 1, label="Suggested"),
        el(8, "AXStaticText", 7, 2, value="Jordan Lee \u00b7 Mobile"),
        el(9, "AXStaticText", 8, 3, value="Jordan Lee"),
        el(10, "AXStaticText", 7, 2, value="Jordan Leeds \u00b7 Security"),
    ]
    md = "\n".join(
        [
            '- [0] AXWindow "Ledger"',
            '  - [1] AXTable "Transactions"',
            "    - [2] AXRow",
            "      - [3] AXCell",
            '        - AXStaticText = "TX-1"',
            "      - [4] AXCell",
            '        - AXStaticText = "$1,410.00"',
            "    - [5] AXRow",
            "      - [6] AXCell",
            '        - AXStaticText = "TX-2"',
            '  - [7] AXList "Suggested"',
            '    - [8] AXStaticText = "Jordan Lee \u00b7 Mobile"',
            '      - [9] AXStaticText = "Jordan Lee"',
            '    - [10] AXStaticText = "Jordan Leeds \u00b7 Security"',
        ]
    )
    return build_snapshot({"elements": elements, "tree_markdown": md, "window_title": "Ledger"}, 1, 1)


def find(snap: Snapshot, role: str) -> UINode:
    return next(n for n in snap.nodes if n.role == role)


def test_returns_a_table_row_by_row() -> None:
    snap = page()
    assert content_of(snap, find(snap, "AXTable"), 300) == {"rows": [["TX-1", "$1,410.00"], ["TX-2"]]}


def test_returns_a_lists_options_without_the_text_nested_in_them() -> None:
    snap = page()
    assert content_of(snap, find(snap, "AXList"), 300) == {
        "rows": [["Jordan Lee \u00b7 Mobile"], ["Jordan Leeds \u00b7 Security"]]
    }


def test_returns_a_row_as_one_run_of_text_and_says_when_rows_were_cut() -> None:
    snap = page()
    assert content_of(snap, find(snap, "AXRow"), 300) == {"text": ["TX-1", "$1,410.00"]}
    assert content_of(snap, find(snap, "AXTable"), 1) == {"rows": [["TX-1", "$1,410.00"]], "truncated": True}
    assert content_of(snap, find(snap, "AXRow"), 1) == {"text": ["TX-1"], "truncated": True}
    assert content_of(snap, find(snap, "AXWindow"), 300)["text"][0] == "TX-1"
    assert content_of(snap, find(snap, "AXCell"), 300) == {"text": ["TX-1"]}


def test_a_container_with_no_text_under_it_gives_nothing() -> None:
    snap = page()
    assert content_of(snap, find(snap, "AXStaticText"), 300) == {}


def test_elements_are_nodes_with_text_then_the_unindexed_texts() -> None:
    snap = page()
    shown = [describe_element(snap, e) for e in elements_of(snap)]
    assert [d["role"] for d in shown] == ["AXTable", "AXList"] + ["AXStaticText"] * 9
    assert shown[0] == {
        "role": "AXTable",
        "name": "Transactions",
        "within": ["AXWindow: Ledger"],
        "first_rows": ["TX-1 | $1,410.00", "TX-2"],
    }
    assert shown[2] == {
        "role": "AXStaticText",
        "value": "Jordan Lee \u00b7 Mobile",
        "within": ["AXList: Suggested", "AXWindow: Ledger"],
    }
    assert shown[5] == {
        "role": "AXStaticText",
        "value": "TX-1",
        "within": ["AXCell", "AXRow: TX-1", "AXTable: Transactions"],
    }


async def test_an_empty_window_is_not_found_without_asking_jev(tmp_path: Path) -> None:
    raw: dict[str, JsonValue] = {
        "elements": [el(0, "AXWindow", title="Blank")],
        "tree_markdown": '- [0] AXWindow "Blank"',
        "window_title": "Blank",
    }
    empty = build_snapshot(raw, 1, 1)
    session = FakeSession([empty], cache=tmp_path)
    args: ExtractArgs = {"pid": 1, "instruction": "the total"}
    out = await extract_tool(session, args)
    assert dumps(out) == (
        '{"status":"not_found","window":{"app":"","pid":1,"windowId":1,"title":"Blank"},'
        '"message":"the window shows no text"}'
    )
    assert session.jev_calls == 0


async def test_a_sure_pick_returns_the_element_exactly_as_read(tmp_path: Path) -> None:
    session = FakeSession([page()], jev_picking('"Transactions"'), cache=tmp_path)
    out = await extract_tool(session, {"pid": 1, "instruction": "the transactions"})
    assert dumps(out) == (
        '{"status":"done","window":{"app":"","pid":1,"windowId":1,"title":"Ledger"},'
        '"element":{"role":"AXTable","p":0.95,"label":"Transactions","rows":[["TX-1","$1,410.00"],["TX-2"]]},'
        '"jev":{"calls":1,"inputTokens":10,"ms":1}}'
    )


async def test_an_unclear_leader_lists_previews(tmp_path: Path) -> None:
    session = FakeSession([page()], jev_picking('"Transactions"', '"Suggested"'), cache=tmp_path)
    out = await extract_tool(session, {"pid": 1, "instruction": "the transactions"})
    assert out["status"] == "ambiguous"
    assert out["message"] == "no clear leader; the likeliest elements are listed with their values"
    assert out.get("elements") == [
        {"role": "AXTable", "p": 0.45, "label": "Transactions", "rows": [["TX-1", "$1,410.00"], ["TX-2"]]},
        {
            "role": "AXList",
            "p": 0.45,
            "label": "Suggested",
            "rows": [["Jordan Lee \u00b7 Mobile"], ["Jordan Leeds \u00b7 Security"]],
        },
    ]
    assert list(out) == ["status", "window", "message", "elements", "jev"]


async def test_no_element_is_not_found_with_p_none(tmp_path: Path) -> None:
    session = FakeSession([page()], jev_picking("nothing like this"), cache=tmp_path)
    out = await extract_tool(session, {"pid": 1, "instruction": "the weather"})
    assert out["status"] == "not_found"
    assert out["message"] == "Jev found no element for this instruction (p(none)=0.95)"
    assert list(out) == ["status", "window", "message", "jev"]


def test_a_node_is_described_by_name_identifier_value_and_help() -> None:
    field = node(
        1,
        "AXTextField",
        "Seats",
        raw_label="Seats",
        identifier="seats-field",
        value="12",
        raw_value=" 12 ",
        help="How many seats",
        within=["AXGroup: Booking"],
        exact=True,
    )
    hidden = node(2, "AXButton", "Go", raw_label="Go", identifier="_NS:12")
    s = snap([field, hidden])
    elements = elements_of(s)
    assert describe_element(s, elements[0]) == {
        "role": "AXTextField",
        "name": "Seats",
        "identifier": "seats-field",
        "value": "12",
        "help": "How many seats",
        "within": ["AXGroup: Booking"],
    }
    assert describe_element(s, elements[1]) == {"role": "AXButton", "name": "Go"}
    assert extracted(s, elements[0], 0.87654, 300) == {
        "role": "AXTextField",
        "p": 0.877,
        "label": "Seats",
        "identifier": "seats-field",
        "value": " 12 ",
        "exact": True,
    }
    assert extracted(s, elements[1], 0.5, 300) == {"role": "AXButton", "p": 0.5, "label": "Go", "identifier": "_NS:12"}
