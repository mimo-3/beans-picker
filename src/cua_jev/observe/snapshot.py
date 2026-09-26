"""Observing a window: cua-driver's element list joined with its markdown rendering.

The markdown carries what the element list lacks: identifiers (`id=`), help texts and unindexed
rows (static text, disabled menu items).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from cua_jev._aio import gather_settled
from cua_jev._numbers import round_half_up, scalar_text
from cua_jev._text import DIGIT, utf16_len
from cua_jev.driver.markdown import MdNode, parse_tree_markdown
from cua_jev.driver.mcp import Driver
from cua_jev.driver.sentinel import FrontSampler, front_pid
from cua_jev.driver.types import (
    Element,
    Frame,
    GetWindowStateArgs,
    ListWindowsArgs,
    Window,
    window_state_of,
    windows_of,
)
from cua_jev.observe.exacttext import EDITABLE_ROLES, TOGGLE_ROLES, apply_exact_text
from cua_jev.observe.identity import key_of, menu_key
from cua_jev.observe.normalize import humanize_identifier, normalize_text, truncate
from cua_jev.observe.signature import state_signature
from cua_jev.observe.types import MenuItem, Modal, Snapshot, TextNode, UINode

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
# Digits split by directional marks are separate runs (Calculator shows a pending "2^10" as "2",
# marks, then a superscript "10"): keep them apart instead of reading "210".
# The marks are U+200B..U+200F and U+2066..U+2069.
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
    """A snapshot of the window, with desktop facts (front window, the app's other windows) and
    the exact text of fields and toggles when the native reader can supply it."""
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
        # A window on top of the z-order does not make its app active (a background launch orders
        # the window front without activating it), and menus follow the active app.
        if front is not None and front != pid:
            snap.frontmost = False
    # Fields' exact text and toggles' state, which cua-driver trims or leaves out.
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
    """From the window list (on-screen, normal layer, front first by z-index): whether the window is
    the frontmost ordinary window, and the app's other visible windows (panels such as Fonts open as
    separate windows)."""
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
    """Joins a `get_window_state` result's elements with its markdown rendering.

    Pure except for `taken_at`. `frontmost` and `app_windows` are left unknown.
    """
    state = window_state_of(raw)
    every = state.elements if state.elements is not None else []
    copies = _repeated_windows(every) | _column_copies(every)
    elements = [e for e in every if e.element_index not in copies]
    # Unindexed rows belong to their nearest indexed ancestor, so a dropped copy takes them along.
    md = [n for n in parse_tree_markdown(state.tree_markdown or "") if _owner(n) not in copies]
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


def _owner(n: MdNode) -> int:
    """The element a markdown row belongs to: its own index, else its nearest indexed ancestor's."""
    if n.index is not None:
        return n.index
    return n.parent_index if n.parent_index is not None else -1


def _element_chain(e: Element, by_index: Mapping[int, Element]) -> Iterator[Element]:
    """`e` and its ancestors among `by_index`."""
    cur: Element | None = e
    while cur is not None:
        yield cur
        cur = by_index.get(cur.parent_index if cur.parent_index is not None else -1)


def _node_chain(start: UINode | None, by_index: Mapping[int, UINode]) -> Iterator[UINode]:
    cur = start
    while cur is not None:
        yield cur
        cur = by_index.get(cur.parent if cur.parent is not None else -1)


def _repeated_windows(elements: Sequence[Element]) -> set[int]:
    """Elements of a window the tree lists a second time. cua-driver can render the target window,
    then the menu bar, then the same window again (same title and frame); every control would
    otherwise appear twice, under two different ids."""
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
    """A table's columns: they hold the very cells its rows hold (web tables list every cell under
    its row and again under its column), so each control would appear twice. Only the rows are
    kept."""
    tables_with_rows = {e.parent_index for e in elements if e.role == "AXRow" and e.parent_index is not None}
    roots = {
        e.element_index
        for e in elements
        if e.role == "AXColumn" and (e.parent_index if e.parent_index is not None else -1) in tables_with_rows
    }
    return _subtrees(elements, roots)


def _subtrees(elements: Sequence[Element], roots: set[int]) -> set[int]:
    """The roots and everything under them."""
    if not roots:
        return set()
    by_index = {e.element_index: e for e in elements}
    return {e.element_index for e in elements if any(c.element_index in roots for c in _element_chain(e, by_index))}


def assign_keys(nodes: Sequence[UINode]) -> None:
    """Gives every node its stable key (in place).

    Run again once fields have their exact text: cua-driver shows an empty field's placeholder as
    its value, which would make its label look like content until the field is typed in.
    """
    for n in nodes:
        # An unlabeled field is named after its content for display, but its identity must not change
        # when its content does: typing into it would otherwise make it a "new" control. (Some apps
        # report a text view's content as its label too, and a pop-up its chosen title.) A placeholder
        # that cua-driver gives as the label of an empty field is replaced by the content once the
        # field is typed in, so it is no name either.
        named = (
            not (n.role in EDITABLE_ROLES or n.role == "AXPopUpButton")
            or n.value is None
            or (n.label != truncate(n.value, 40) and n.raw_label != n.value and n.raw_label != n.placeholder)
        )
        n.key = key_of(n.role, n.identifier, n.label if named else "", n.within)
    # Controls that share a key (unlabeled fields side by side, a row of identical buttons) are told
    # apart by their order in the tree, so one never stands in for another.
    seen: dict[str, int] = {}
    for n in nodes:
        k = seen.get(n.key, 0)
        seen[n.key] = k + 1
        if k:
            n.key = f"{n.key}#{k + 1}"


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
    """A tree depth; whole depths are kept as `int` (the markdown gives half levels for odd indents)."""
    return int(d) if float(d).is_integer() else d


def _first(*values: str | None) -> str | None:
    """The first value that is not `None` (an empty string counts)."""
    return next((v for v in values if v is not None), None)


def _name_rows_by_first_text(base: Sequence[UINode], md: Sequence[MdNode], by_index: Mapping[int, UINode]) -> None:
    """A table row with no name of its own is named after the first text in it (its first cell, as
    a person reads it: "INV-1043"), so the controls inside it can say which row they are on."""
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
    """The nearest named container ancestors, innermost first, at most three."""
    out: list[str] = []
    parent = by_index.get(n.parent if n.parent is not None else -1)
    for p in _node_chain(parent, by_index):
        if len(out) >= 3:
            break
        if p.role in _CONTAINER_ROLES and p.label:
            out.append(f"{p.role}: {truncate(p.label, 40)}")
    return out


def _collect_texts(md: Sequence[MdNode]) -> list[TextNode]:
    """Static text rows, indexed or not (web pages index theirs); empty ones are left out."""
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
    """Menu-bar rows as path-addressed items. Unindexed menu rows are disabled items; items of an
    open context menu (not under a menu bar) are left out."""
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
    """A sheet, dialog or popover; else an open context or pop-up menu (an AXMenu outside the menu
    bar with items)."""
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
