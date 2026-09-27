"""Observing a window: cua-driver's element list joined with its markdown rendering."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from beans_picker._aio import gather_settled
from beans_picker._numbers import round_half_up, scalar_text
from beans_picker._text import DIGIT, utf16_len
from beans_picker.driver.markdown import MdNode, parse_tree_markdown
from beans_picker.driver.mcp import Driver
from beans_picker.driver.sentinel import FrontSampler, front_pid
from beans_picker.driver.types import (
    Element,
    Frame,
    GetWindowStateArgs,
    ListWindowsArgs,
    Window,
    window_state_of,
    windows_of,
)
from beans_picker.observe.exacttext import EDITABLE_ROLES, TOGGLE_ROLES, apply_exact_text
from beans_picker.observe.identity import ambiguous_key, key_of, menu_key
from beans_picker.observe.normalize import humanize_identifier, normalize_text, truncate
from beans_picker.observe.signature import state_signature
from beans_picker.observe.types import MenuItem, Modal, Snapshot, TextNode, UINode

_log = logging.getLogger(__name__)

type ReadExact = Callable[[int], Awaitable[list[object] | None]]

_MODAL_ROLES: Final = frozenset({"AXSheet", "AXDialog", "AXPopover"})
_CONTAINER_ROLES: Final = frozenset(
    {
        "AXGroup", "AXToolbar", "AXSheet", "AXDialog", "AXPopover", "AXScrollArea", "AXSplitGroup",
        "AXTabGroup", "AXList", "AXOutline", "AXTable", "AXRow", "AXWindow",
    }
)  # fmt: skip
# The driver's own overlay (agent cursor) sits above everything and is never "the front app".
_OVERLAY_APP: Final = re.compile("cua driver", re.IGNORECASE | re.ASCII)
# Directional marks split digit runs: a pending "2^10" must not read as "210".
_DIGIT_RUN_BREAK: Final = re.compile(f"({DIGIT})[\u200b-\u200f\u2066-\u2069]+(?={DIGIT})")
_UNTITLED_WINDOW: Final = "(untitled window)"
_OPEN_MENU: Final = "open menu"


async def observe(
    driver: Driver,
    pid: int,
    window_id: int,
    *,
    front_pid: FrontSampler = front_pid,
    read_exact: ReadExact,
) -> Snapshot:
    """A snapshot of the window, with desktop facts and exact field text when the native reader has it."""
    t0 = time.perf_counter()
    state_args: GetWindowStateArgs = {
        "pid": pid,
        "window_id": window_id,
        "include_screenshot": False,
        "max_elements": 1500,
        "max_depth": 25,
    }
    all_windows: ListWindowsArgs = {}
    # list_windows, not get_accessibility_tree: the latter walks every app and hangs on an unresponsive one.
    raw_task = asyncio.ensure_future(driver.must("get_window_state", state_args))
    desk_task = asyncio.ensure_future(driver.call("list_windows", all_windows))
    front_task = asyncio.ensure_future(front_pid())
    await gather_settled(raw_task, desk_task, front_task)
    raw, desk, front = raw_task.result(), desk_task.result(), front_task.result()

    snap = build_snapshot(raw, pid, window_id)
    windows = windows_of(desk.data) if desk.ok else None
    if windows is not None:
        facts = desktop_facts(windows, pid, window_id)
        snap.frontmost = facts.frontmost
        snap.app_windows = facts.app_windows
        # get_window_state can leave the app and window names out; the window list has them.
        own = facts.own
        if not snap.app_name:
            snap.app_name = normalize_text(own.app_name if own is not None else None)
        if not snap.window_title:
            snap.window_title = normalize_text(own.title if own is not None else None)
        # A background launch orders a window front without activating its app, and menus follow the active app.
        if front is not None and front != pid:
            snap.frontmost = False
    if any(n.role in EDITABLE_ROLES or n.role in TOGGLE_ROLES for n in snap.nodes):
        try:
            fields = await read_exact(pid)
        except Exception as err:  # exact text is optional: a failure to read it means "not read"
            _log.debug("exact text for pid %d: %s", pid, err)
            fields = None
        if fields is not None:
            apply_exact_text(snap.nodes, fields, snap.window_title)
            assign_keys(snap.nodes)
    snap.signature = state_signature(snap)
    snap.ms = round_half_up((time.perf_counter() - t0) * 1000)
    return snap


@dataclass(frozen=True, slots=True, kw_only=True)
class DesktopFacts:
    frontmost: bool
    """Whether the window is the frontmost ordinary window."""
    app_windows: list[str]
    """Titles of the app's other ordinary windows, front first."""
    own: Window | None
    """The window's own entry in the list."""


def desktop_facts(windows: Sequence[Window], pid: int, window_id: int) -> DesktopFacts:
    """Whether the window is the frontmost ordinary one, and the app's other visible windows."""
    ordinary = sorted(
        (
            w
            for w in windows
            if w.is_on_screen is not False
            and (w.layer if w.layer is not None else 0) == 0
            and not _OVERLAY_APP.fullmatch(w.app_name if w.app_name is not None else "")
        ),
        key=lambda w: -(w.z_index if w.z_index is not None else -1),
    )
    return DesktopFacts(
        frontmost=bool(ordinary) and ordinary[0].window_id == window_id,
        app_windows=[
            normalize_text(w.title) or _UNTITLED_WINDOW for w in ordinary if w.pid == pid and w.window_id != window_id
        ],
        own=next((w for w in windows if w.window_id == window_id), None),
    )


def build_snapshot(raw: Mapping[str, object], pid: int, window_id: int) -> Snapshot:
    """Joins a `get_window_state` result's elements with its markdown rendering."""
    state = window_state_of(raw)
    every = state.elements if state.elements is not None else []
    md_all = parse_tree_markdown(state.tree_markdown or "")
    secure = {e.element_index for e in every if e.role == "AXSecureTextField" or e.subrole == "AXSecureTextField"}
    roots = _repeated_windows(every) | _column_copies(every) | secure
    copies, dropped_lines = _excluded_tree(every, md_all, roots)
    elements = [e for e in every if e.element_index not in copies]
    md = [n for n in md_all if n.line not in dropped_lines]
    md_by_index = {n.index: n for n in md if n.index is not None}
    by_index = {e.element_index: e for e in elements}
    menu_bar = next((e.element_index for e in elements if e.role == "AXMenuBar"), None)

    def in_menu_bar(e: Element) -> bool:
        return menu_bar is not None and any(cur.element_index == menu_bar for cur in _element_chain(e, by_index))

    base = [_to_node(e, md_by_index.get(e.element_index), in_menu_bar(e)) for e in elements]
    base_by_index = {n.index: n for n in base}
    _name_rows_by_first_text(base, md, base_by_index)
    for n in base:
        n.within = _ancestors_of(n, base_by_index)
    assign_keys(base)

    nodes = [n for n in base if not n.in_menu_bar]
    snap = Snapshot(
        id=state.snapshot_id if state.snapshot_id is not None else "",
        pid=pid,
        window_id=window_id,
        app_name=normalize_text(state.app_name),
        window_title=normalize_text(state.window_title),
        nodes=nodes,
        texts=_collect_texts(md),
        menu=_collect_menu(md, base_by_index),
        modal=_find_modal(nodes, md),
        signature="",
        taken_at=time.time_ns() // 1_000_000,
        ms=0,
    )
    snap.signature = state_signature(snap)
    return snap


def _excluded_tree(
    elements: Sequence[Element], markdown: Sequence[MdNode], roots: set[int]
) -> tuple[set[int], set[int]]:
    # Keep element IDs and markdown line numbers in separate namespaces. The two representations
    # of an indexed node are linked both ways; parent edges remain directed toward children.
    children: dict[tuple[str, int], list[tuple[str, int]]] = {}
    excluded = {("element", index) for index in roots}
    for e in elements:
        if e.parent_index is not None:
            children.setdefault(("element", e.parent_index), []).append(("element", e.element_index))
    for row in markdown:
        line = ("line", row.line)
        if row.parent_line is not None:
            children.setdefault(("line", row.parent_line), []).append(line)
        if row.index is not None:
            element = ("element", row.index)
            children.setdefault(element, []).append(line)
            children.setdefault(line, []).append(element)
        if row.role == "AXSecureTextField":
            excluded.add(line)
    # Each vertex and edge is visited at most once, including arbitrarily alternating paths and
    # cycles. Unindexed markdown descendants travel through their line edges as well.
    pending = list(excluded)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in excluded:
                excluded.add(child)
                pending.append(child)
    return (
        {index for kind, index in excluded if kind == "element"},
        {index for kind, index in excluded if kind == "line"},
    )


def _owner(n: MdNode) -> int:
    if n.index is not None:
        return n.index
    return n.parent_index if n.parent_index is not None else -1


def _element_chain(e: Element, by_index: Mapping[int, Element]) -> Iterator[Element]:
    cur: Element | None = e
    seen: set[int] = set()
    while cur is not None and cur.element_index not in seen:
        seen.add(cur.element_index)
        yield cur
        cur = by_index.get(cur.parent_index if cur.parent_index is not None else -1)


def _node_chain(start: UINode | None, by_index: Mapping[int, UINode]) -> Iterator[UINode]:
    cur = start
    seen: set[int] = set()
    while cur is not None and cur.index not in seen:
        seen.add(cur.index)
        yield cur
        cur = by_index.get(cur.parent if cur.parent is not None else -1)


def _repeated_windows(elements: Sequence[Element]) -> set[int]:
    """cua-driver can render the target window a second time after the menu bar."""
    seen: set[tuple[str, Frame | None]] = set()
    roots: set[int] = set()
    for e in elements:
        if e.role != "AXWindow" or e.parent_index is not None:
            continue
        name = e.label if e.label is not None else e.title if e.title is not None else ""
        sig = (name, e.frame)
        if sig in seen:
            roots.add(e.element_index)
        seen.add(sig)
    return _subtrees(elements, roots)


def _column_copies(elements: Sequence[Element]) -> set[int]:
    """Web tables list every cell under both its row and its column; only rows are kept."""
    tables_with_rows = {e.parent_index for e in elements if e.role == "AXRow" and e.parent_index is not None}
    roots = {
        e.element_index
        for e in elements
        if e.role == "AXColumn" and (e.parent_index if e.parent_index is not None else -1) in tables_with_rows
    }
    return _subtrees(elements, roots)


def _subtrees(elements: Sequence[Element], roots: set[int]) -> set[int]:
    if not roots:
        return set()
    children: dict[int, list[int]] = {}
    for e in elements:
        if e.parent_index is not None:
            children.setdefault(e.parent_index, []).append(e.element_index)
    # A secure root may exist only in markdown; its index still excludes structured descendants.
    excluded = set(roots)
    pending = list(roots)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in excluded:
                excluded.add(child)
                pending.append(child)
    return excluded


def assign_keys(nodes: Sequence[UINode]) -> None:
    """Gives every node its stable key (in place)."""
    for n in nodes:
        # An unlabeled field's identity must not follow its content, or typing would make it a new control.
        named = (
            not (n.role in EDITABLE_ROLES or n.role == "AXPopUpButton")
            or n.value is None
            or (n.label != truncate(n.value, 40) and n.raw_label != n.value and n.raw_label != n.placeholder)
        )
        n.key = key_of(n.role, n.identifier, n.label if named else "", n.within)
    # No duplicate gets the unique key: losing or reordering a twin must not transfer an action.
    counts = Counter(n.key for n in nodes)
    for n in nodes:
        if counts[n.key] > 1:
            n.key = ambiguous_key(n.key, n.token, n.index)


def _to_node(e: Element, m: MdNode | None, in_menu: bool) -> UINode:
    raw_label = normalize_text(_first(e.label, m.label if m else None, m.title if m else None))
    identifier = m.identifier if m else None
    help_text = normalize_text(m.help) if m is not None and m.help else ""
    if e.value is not None:
        value: str | None = normalize_text(scalar_text(e.value))
    elif m is not None and m.value is not None:
        value = normalize_text(m.value)
    else:
        value = None
    label = (
        raw_label
        or humanize_identifier(identifier)
        or (help_text if help_text and utf16_len(help_text) <= 80 else "")
        or (truncate(value, 40) if value else "")
    )
    node = UINode(
        index=e.element_index,
        token=e.element_token if e.element_token is not None else "",
        role=e.role,
        label=label,
        enabled=e.enabled is not False,
        actions=[a[2:].lower() for a in e.actions or [] if a.startswith("AX")],
        depth=_depth(e.depth if e.depth is not None else m.depth if m is not None else 0),
        in_menu_bar=in_menu,
        within=[],
        key="",
    )
    if raw_label:
        node.raw_label = raw_label
    if m is not None and m.title:
        node.title = normalize_text(m.title)
    node.value = value
    node.raw_value = scalar_text(e.value) if e.value is not None else m.value if m is not None else None
    if identifier:
        node.identifier = identifier
    if help_text:
        node.help = help_text
    if e.subrole:
        node.subrole = e.subrole
    node.selected = e.selected
    node.frame = e.frame
    node.parent = e.parent_index
    return node


def _depth(d: float) -> float:
    return int(d) if float(d).is_integer() else d


def _first(*values: str | None) -> str | None:
    return next((v for v in values if v is not None), None)


def _name_rows_by_first_text(base: Sequence[UINode], md: Sequence[MdNode], by_index: Mapping[int, UINode]) -> None:
    """Unnamed table rows take their first cell's text, so their controls can say which row they are on."""
    unnamed = {n.index for n in base if n.role == "AXRow" and not n.label}
    if not unnamed:
        return
    for t in md:
        if t.role != "AXStaticText":
            continue
        text = normalize_text(_first(t.value, t.title, t.label, ""))
        if not text:
            continue
        row = next((cur for cur in _node_chain(by_index.get(_owner(t)), by_index) if cur.role == "AXRow"), None)
        if row is not None and row.index in unnamed:
            unnamed.discard(row.index)
            row.label = truncate(text, 40)


def _ancestors_of(n: UINode, by_index: Mapping[int, UINode]) -> list[str]:
    out: list[str] = []
    parent = by_index.get(n.parent if n.parent is not None else -1)
    for p in _node_chain(parent, by_index):
        if len(out) >= 3:
            break
        if p.role in _CONTAINER_ROLES and p.label:
            out.append(f"{p.role}: {truncate(p.label, 40)}")
    return out


def _collect_texts(md: Sequence[MdNode]) -> list[TextNode]:
    out: list[TextNode] = []
    for n in md:
        if n.role != "AXStaticText":
            continue
        raw = _first(n.value, n.title, n.label, "") or ""
        value = normalize_text(_DIGIT_RUN_BREAK.sub(r"\1 ", raw))
        if not value:
            continue
        out.append(TextNode(role=n.role, value=value, raw=raw, depth=_depth(n.depth), parent_index=n.parent_index))
    return out


def _collect_menu(md: Sequence[MdNode], by_index: Mapping[int, UINode]) -> list[MenuItem]:
    by_line = {n.line: n for n in md}
    out: list[MenuItem] = []
    for n in md:
        if n.role != "AXMenuItem":
            continue
        path: list[str] = []
        under_bar = False
        cur: MdNode | None = n
        while cur is not None:
            if cur.role in ("AXMenuItem", "AXMenuBarItem"):
                path.insert(0, normalize_text(_first(cur.title, cur.label)))
            if cur.role == "AXMenuBar":
                under_bar = True
            cur = by_line.get(cur.parent_line if cur.parent_line is not None else -1)
        if not under_bar or not all(path):
            continue
        node = by_index.get(n.index) if n.index is not None else None
        item = MenuItem(path=path, enabled=node.enabled if node is not None else False, key=menu_key(path))
        if node is not None:
            item.index = node.index
            item.token = node.token
        if n.identifier:
            item.identifier = n.identifier
        out.append(item)
    return out


def _find_modal(nodes: Sequence[UINode], md: Sequence[MdNode]) -> Modal | None:
    m = next((n for n in nodes if n.role in _MODAL_ROLES), None)
    if m is not None:
        return Modal(role=m.role, label=m.label, index=m.index)
    menu = next((n for n in nodes if n.role == "AXMenu"), None)
    if menu is not None and any(r.parent_index == menu.index and r.role == "AXMenuItem" for r in md):
        return Modal(role="AXMenu", label=menu.label or _OPEN_MENU, index=menu.index)
    return None


def is_descendant(snap: Snapshot, node: UINode, ancestor_index: int) -> bool:
    """Whether `node` is the node at `ancestor_index` or lies under it."""
    by_index = {n.index: n for n in snap.nodes}
    return any(cur.index == ancestor_index for cur in _node_chain(node, by_index))


def in_web_area(snap: Snapshot, node: UINode) -> bool:
    """Whether the node is part of a web page (under an AXWebArea)."""
    by_index = {n.index: n for n in snap.nodes}
    parent = by_index.get(node.parent if node.parent is not None else -1)
    return any(cur.role == "AXWebArea" for cur in _node_chain(parent, by_index))
