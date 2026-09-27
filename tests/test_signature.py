from __future__ import annotations

import dataclasses
import hashlib

from beans_picker.observe.signature import state_signature
from beans_picker.observe.types import Modal, Snapshot, TextNode, UINode


def _node(key: str, **fields: object) -> UINode:
    n = UINode(
        index=0,
        token="",
        role="AXTextField",
        label="",
        enabled=True,
        actions=[],
        depth=1,
        in_menu_bar=False,
        within=[],
        key=key,
    )
    for name, value in fields.items():
        setattr(n, name, value)
    return n


def _snap(nodes: list[UINode] | None = None, **fields: object) -> Snapshot:
    s = Snapshot(
        id="",
        pid=1,
        window_id=1,
        app_name="",
        window_title="Doc",
        nodes=nodes or [],
        texts=[],
        menu=[],
        signature="",
        taken_at=0,
        ms=0,
    )
    return dataclasses.replace(s, **fields)  # type: ignore[arg-type]


def _sha(parts: list[str]) -> str:
    return hashlib.sha1("\x01".join(parts).encode()).hexdigest()[:16]  # noqa: S324


def test_signature_parts_in_order() -> None:
    nodes = [
        _node("a", value="1"),
        _node("b", value="", exact=True, raw_value=" x\n"),
        _node("c", selected=True, exact=True),
        _node("d"),
        _node("e", value="2"),
    ]
    snap = _snap(
        nodes,
        modal=Modal(role="AXSheet", label="Save", index=3),
        texts=[TextNode(role="AXStaticText", value="hi", raw="hi", depth=1)],
        app_windows=["Fonts", "(untitled window)"],
    )
    expected = [
        "Doc",
        "n1",
        "AXSheet:Save",
        "a=1",
        "b=",
        "e=2",
        'x:b=" x\\n"',
        "x:c=undefined",
        "sel:c",
        "hi",
        "win:Fonts",
        "win:(untitled window)",
    ]
    assert state_signature(snap) == _sha(expected)


def test_signature_without_modal_or_other_windows() -> None:
    assert state_signature(_snap()) == _sha(["Doc", "n0", "-"])
    assert len(state_signature(_snap())) == 16


def test_exact_text_changes_the_signature_only_when_marked_exact() -> None:
    plain = _snap([_node("a", value="x", raw_value="x")])
    trailing = _snap([_node("a", value="x", raw_value="x\n")])
    assert state_signature(plain) == state_signature(trailing)
    exact = _snap([_node("a", value="x", raw_value="x", exact=True)])
    exact_trailing = _snap([_node("a", value="x", raw_value="x\n", exact=True)])
    assert state_signature(exact) != state_signature(exact_trailing)


def test_a_lone_surrogate_hashes_as_the_replacement_character() -> None:
    lone = _snap(window_title="\ud800")
    replaced = _snap(window_title="\ufffd")
    assert state_signature(lone) == state_signature(replaced)
    exact = _snap([_node("a", raw_value="\ud83d", exact=True)])
    assert state_signature(exact) == _sha(["Doc", "n0", "-", 'x:a="\\ud83d"'])
