"""extract: the element Jev picks for the instruction, returned with its text exactly as read."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal, NotRequired, TypedDict

from cua_jev._json import JsonObject, JsonValue
from cua_jev._numbers import fixed, round3
from cua_jev.jev.client import JevUsageOut
from cua_jev.jev.questions import element_question
from cua_jev.jev.rank import Ambiguous, NotFound, Pick, RankSpec, gate, rank
from cua_jev.jev.state import build_state, reading_texts
from cua_jev.observe.normalize import truncate
from cua_jev.observe.types import Snapshot, TextNode, UINode
from cua_jev.tools.args import ExtractArgs
from cua_jev.tools.present import ShownWindow, show_window
from cua_jev.tools.session import ToolSession

CONTENT_LIMIT: Final = 300
"""How many rows (or texts) a picked element returns."""
PREVIEW_LIMIT: Final = 3
"""How many rows (or texts) each element of a shortlist previews."""
ROWS_OF: Final = frozenset({"AXTable", "AXOutline", "AXList"})


@dataclass(frozen=True, slots=True)
class NodeElement:
    node: UINode


@dataclass(frozen=True, slots=True)
class TextElement:
    text: TextNode
    within: list[str] = field(default_factory=list)


type Element = NodeElement | TextElement


class Content(TypedDict, total=False):
    """A container's texts: row by row for a table, outline or list, else one run in tree order."""

    rows: list[list[str]]
    text: list[str]
    truncated: bool


class Extracted(TypedDict):
    """An element as returned: the value or text exactly as read (not trimmed or normalized)."""

    role: str
    p: NotRequired[float]
    label: NotRequired[str]
    identifier: NotRequired[str]
    value: NotRequired[str]
    exact: NotRequired[bool]
    rows: NotRequired[list[list[str]]]
    text: NotRequired[list[str]]
    truncated: NotRequired[bool]


class ExtractOutput(TypedDict):
    status: Literal["done", "ambiguous", "not_found"]
    window: ShownWindow
    message: NotRequired[str]
    element: NotRequired[Extracted]
    elements: NotRequired[list[Extracted]]
    jev: NotRequired[JevUsageOut]


async def extract_tool(session: ToolSession, args: ExtractArgs) -> ExtractOutput:
    t = await session.target(args)
    snap = await session.snapshot(t)
    elements = elements_of(snap)
    window = show_window(snap)
    if not elements:
        return {"status": "not_found", "window": window, "message": "the window shows no text"}
    jev = session.jev()
    r = await rank(
        jev,
        build_state(args["instruction"], snap),
        elements,
        RankSpec(describe=lambda e: describe_element(snap, e), question=element_question),
    )
    match gate(r, 5):
        case Pick(item=item, p=p):
            return {
                "status": "done",
                "window": window,
                "element": extracted(snap, item, p, CONTENT_LIMIT),
                "jev": jev.take_usage().as_output(),
            }
        case Ambiguous(shortlist=shortlist):
            return {
                "status": "ambiguous",
                "window": window,
                "message": "no clear leader; the likeliest elements are listed with their values",
                "elements": [extracted(snap, x.item, x.p, PREVIEW_LIMIT) for x in shortlist],
                "jev": jev.take_usage().as_output(),
            }
        case NotFound(p_none=p_none):
            return {
                "status": "not_found",
                "window": window,
                "message": f"Jev found no element for this instruction (p(none)={fixed(p_none, 2)})",
                "jev": jev.take_usage().as_output(),
            }


def elements_of(snap: Snapshot) -> list[Element]:
    """Indexed elements that carry text, and every table, outline and list (a web page's table often
    has no name), then the unindexed static texts. Menu-bar items are not content."""
    by_index = {n.index: n for n in snap.nodes}
    nodes: list[Element] = [
        NodeElement(n)
        for n in snap.nodes
        if n.role != "AXWindow"
        and (n.raw_value is not None or n.raw_label is not None or n.title is not None or n.role in ROWS_OF)
    ]
    texts: list[Element] = []
    for t in snap.texts:
        parent = by_index.get(t.parent_index) if t.parent_index is not None else None
        within: list[str] = []
        if parent is not None:
            name = f"{parent.role}: {truncate(parent.label, 40)}" if parent.label else parent.role
            within = [name, *parent.within][:3]
        texts.append(TextElement(t, within))
    return nodes + texts


def describe_element(snap: Snapshot, e: Element) -> JsonObject:
    """The element as Jev sees it."""
    if isinstance(e, TextElement):
        d: JsonObject = {"role": e.text.role, "value": truncate(e.text.value, 160)}
        if e.within:
            d["within"] = list[JsonValue](e.within)
        return d
    n = e.node
    d = {"role": n.role}
    if n.raw_label:
        d["name"] = truncate(n.raw_label, 80)
    if n.identifier and not n.identifier.startswith("_"):
        d["identifier"] = truncate(n.identifier, 60)
    if n.value is not None:
        d["value"] = truncate(n.value, 160)
    if n.help:
        d["help"] = truncate(n.help, 80)
    if n.within:
        d["within"] = list[JsonValue](n.within)
    # A table or list is known by its first rows (the header), whether it has a name or not.
    if n.role in ROWS_OF:
        c = content_of(snap, n, 2)
        rows = c.get("rows")
        first = [" | ".join(r) for r in rows] if rows is not None else c.get("text")
        if first:
            d["first_rows"] = [truncate(x, 120) for x in first]
    return d


def extracted(snap: Snapshot, e: Element, p: float, limit: int) -> Extracted:
    """The element as returned to the caller, its content cut to `limit` rows or texts."""
    rounded = round3(p)
    if isinstance(e, TextElement):
        return {"role": e.text.role, "value": e.text.raw, "p": rounded}
    n = e.node
    out: Extracted = {"role": n.role, "p": rounded}
    if n.raw_label:
        out["label"] = n.raw_label
    if n.identifier:
        out["identifier"] = n.identifier
    if n.raw_value is not None:
        out["value"] = n.raw_value
    if n.exact:
        out["exact"] = True
    if n.raw_value is None:
        c = content_of(snap, n, limit)
        if "rows" in c:
            out["rows"] = c["rows"]
        if "text" in c:
            out["text"] = c["text"]
        if "truncated" in c:
            out["truncated"] = c["truncated"]
    return out


def content_of(snap: Snapshot, node: UINode, limit: int) -> Content:
    """The texts under a container, which has no value of its own: a table, outline or list gives
    them row by row (its rows, or a list's direct children), anything else as one run in tree order."""
    by_index = {n.index: n for n in snap.nodes}

    def at(index: int | None) -> UINode | None:
        return by_index.get(index if index is not None else -1)

    def path_of(t: TextNode) -> list[int]:
        path: list[int] = []
        cur = at(t.parent_index)
        while cur is not None:
            path.append(cur.index)
            cur = at(cur.parent)
        return path

    # Text nested in text (a bold run) is already in its parent's.
    texts = [t for t in reading_texts(snap) if (parent := at(t.parent_index)) is None or parent.role != "AXStaticText"]
    under = [(t, path) for t in texts if node.index in (path := path_of(t))]
    if not under:
        return {}
    if node.role in ROWS_OF:

        def is_row(n: UINode | None) -> bool:
            if n is None:
                return False
            return n.parent == node.index if node.role == "AXList" else n.role == "AXRow"

        # Rows in first-seen order: a row node's index, or ("text", i) for a text that is a row itself.
        rows: dict[int | tuple[str, int], list[str]] = {}
        for i, (t, path) in enumerate(under):
            row = next((k for k in path if is_row(by_index.get(k))), None)
            if row is not None:
                rows.setdefault(row, []).append(t.raw)
            elif node.role == "AXList" and t.parent_index == node.index:
                # A list's option can be a text itself, directly under the list: it is a row of its own.
                rows[("text", i)] = [t.raw]
        if rows:
            everything = list(rows.values())
            out: Content = {"rows": everything[:limit]}
            if len(everything) > limit:
                out["truncated"] = True
            return out
    run = [t.raw for t, _ in under]
    out = {"text": run[:limit]}
    if len(run) > limit:
        out["truncated"] = True
    return out
