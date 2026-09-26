"""Parser for cua-driver's `tree_markdown`.

It carries fields the structured `elements[]` lacks: `id=` (AXIdentifier), `help=`, and unindexed
rows (StaticText values, disabled menu items). Rows look like::

    - [13] AXButton (1) [id=One actions=[press]]
    - [0] AXWindow "Calculator" [id=main actions=[raise]]
    - [1] AXTextArea = "alpha" [id=First Text View actions=[showmenu]]
    - AXStaticText = "84"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from cua_jev._text import DIGIT, NOT_LINE_END, WORD, WS, lstrip_ws, trim

# `(?s:.)` is any character, line breaks included: the rest of a row may span continuation lines.
_ROW: Final = re.compile(f"({WS}*)- (?:\\[({DIGIT}+)\\] )?(AX{WORD}+)((?s:.)*)\\Z")
# A double-quoted string with backslash escapes; an escaped character is never a line terminator.
_QUOTED: Final = f'"((?:[^"\\\\]|\\\\{NOT_LINE_END})*)"'
_TITLE: Final = re.compile(f"{_QUOTED}{WS}*")
_LABEL: Final = re.compile(f"\\(([^)]*)\\){WS}*")
_VALUE: Final = re.compile(f"= {_QUOTED}{WS}*")
_IDENTIFIER: Final = re.compile(f'(?:^\\[|{WS})id=({NOT_LINE_END}*?)(?= help="| actions=\\[|\\]\\Z|\\Z)')
_HELP: Final = re.compile(f"help={_QUOTED}")
_ESCAPED_QUOTE_OR_BACKSLASH: Final = re.compile(r'\\(["\\])')


@dataclass(slots=True, kw_only=True)
class MdNode:
    """One row of the tree.

    `depth` is half the row's indentation (fractional for odd widths). `parent_index` is the index
    of the nearest indexed ancestor; `parent_line` is the line of the immediate parent row, indexed
    or not. `line` is the row's 0-based line in the source.
    """

    depth: float
    role: str
    line: int
    index: int | None = None
    title: str | None = None
    label: str | None = None
    value: str | None = None
    identifier: str | None = None
    help: str | None = None
    parent_index: int | None = None
    parent_line: int | None = None


@dataclass(frozen=True, slots=True)
class _Row:
    text: str
    line: int


@dataclass(frozen=True, slots=True)
class _Open:
    depth: float
    line: int
    index: int | None


def parse_tree_markdown(md: str) -> list[MdNode]:
    """Every row of `md`, in order, with its parent links."""
    nodes: list[MdNode] = []
    stack: list[_Open] = []
    for row in _rows(md):
        m = _ROW.match(row.text)
        if m is None:  # pragma: no cover - a row always starts with a row line
            continue
        indent, idx, role, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        depth = len(indent) / 2
        while stack and stack[-1].depth >= depth:
            stack.pop()
        parent = stack[-1] if stack else None
        indexed_ancestor = next((s for s in reversed(stack) if s.index is not None), None)
        node = MdNode(depth=depth, role=role, line=row.line)
        _parse_rest(rest, node)
        if idx is not None:
            node.index = int(idx)
        if indexed_ancestor is not None:
            node.parent_index = indexed_ancestor.index
        if parent is not None:
            node.parent_line = parent.line
        nodes.append(node)
        stack.append(_Open(depth=depth, line=row.line, index=node.index))
    return nodes


def _rows(md: str) -> list[_Row]:
    """Rows joined with the lines that continue them.

    A quoted value or help text with a line break in it runs onto the next lines, and so can an
    attribute list (a custom action name may contain line breaks): those lines belong to the row,
    or its help would be lost and its label judged alone.
    """
    out: list[_Row] = []
    for line, text in enumerate(md.split("\n")):
        if _ROW.match(text):
            out.append(_Row(text, line))
        elif out and trim(text) != "":
            out[-1] = _Row(f"{out[-1].text}\n{lstrip_ws(text)}", out[-1].line)
    return out


def _parse_rest(rest: str, node: MdNode) -> None:
    """Read title, label, value (in that order), then `id=` and `help=` from the attribute list
    when it follows them directly."""
    s = trim(rest)
    if m := _TITLE.match(s):
        node.title = unescape(m.group(1))
        s = s[m.end() :]
    if m := _LABEL.match(s):
        node.label = m.group(1)
        s = s[m.end() :]
    if m := _VALUE.match(s):
        node.value = unescape(m.group(1))
        s = s[m.end() :]
    if s.startswith("["):
        if (m := _IDENTIFIER.search(s)) and m.group(1):
            node.identifier = trim(m.group(1))
        if m := _HELP.search(s):
            node.help = unescape(m.group(1))


def unescape(s: str) -> str:
    """Undo the quoting: `\\"` and `\\\\` lose their backslash, then `\\n` becomes a line break.

    The two steps run one after the other, so an escaped backslash followed by `n` also ends up
    as a line break.
    """
    return _ESCAPED_QUOTE_OR_BACKSLASH.sub(r"\1", s).replace("\\n", "\n")
