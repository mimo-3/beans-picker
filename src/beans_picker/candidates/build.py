"""Deterministic candidate actions from one snapshot."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from beans_picker._json import quote
from beans_picker._text import hash_bytes
from beans_picker.candidates.keypad import compile_keypad, keypad_keys, keypad_parents
from beans_picker.candidates.menu import offerable_menu_items
from beans_picker.candidates.prune import lexical_score
from beans_picker.candidates.safety import (
    clipboard_withheld,
    control_context,
    is_destructive_control,
    is_destructive_label,
    is_destructive_menu,
)
from beans_picker.candidates.types import ActionCandidate, ActionKind, ScrollDirection
from beans_picker.menus.keyequiv import english_title, key_equivalent
from beans_picker.menus.menukeys import MenuKeyTable
from beans_picker.observe.exacttext import EDITABLE_ROLES
from beans_picker.observe.normalize import truncate
from beans_picker.observe.snapshot import is_descendant
from beans_picker.observe.types import Snapshot, UINode

# Two-state controls; a radio button is chosen with a click instead.
_TOGGLE_ROLES: Final = frozenset({"AXCheckBox", "AXSwitch"})
_SCROLL_ROLES: Final = frozenset({"AXScrollArea", "AXTable", "AXOutline", "AXList", "AXWebArea"})
_CLICK_ROLES: Final = frozenset(
    {
        "AXButton",
        "AXMenuButton",
        "AXLink",
        "AXRadioButton",
        "AXTab",
        "AXDisclosureTriangle",
        "AXCell",
        "AXRow",
        "AXImage",
        "AXPopUpButton",
        "AXMenuItem",
    }
)
# Items whose own context menu (a right-click) often holds commands found nowhere else: rename, star, move to trash.
_CONTEXT_ROLES: Final = frozenset({"AXRow", "AXLink", "AXImage"})
_CHROME_ROLES: Final = frozenset({"AXWindow", "AXToolbar", "AXGroup", "AXScrollArea"})
_KEYS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("return",), "Return"),
    (("escape",), "Escape"),
    (("tab",), "Tab"),
    (("space",), "Space"),
    (("up",), "Up arrow"),
    (("down",), "Down arrow"),
    (("left",), "Left arrow"),
    (("right",), "Right arrow"),
    (("shift", "f10"), "Shift+F10 (opens the focused item's context menu)"),
)
_SCROLL_DIRECTIONS: Final[tuple[ScrollDirection, ...]] = ("down", "up")


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildOptions:
    instruction: str | None = None
    text: str | None = None
    list_text_kinds: bool = False
    learned: MenuKeyTable | None = None


def build_candidates(snap: Snapshot, opts: BuildOptions | None = None) -> list[ActionCandidate]:
    """Every candidate action on the snapshot, in a fixed order (controls, menu, keypad, keys)."""
    opts = opts if opts is not None else BuildOptions()
    drafts = _Drafts(snap, opts)
    for n in snap.nodes:
        drafts.add_node(n)
    if snap.modal is None:
        drafts.add_menu()
        drafts.add_keypad()
    for keys, label in _KEYS:
        drafts.add(
            kind="key",
            key=f"key|{'+'.join(keys)}",
            summary=f"press {label}",
            keys=list(keys),
            destructive=keys in (("return",), ("space",)),
        )
    out = _place_twins(_dedupe(drafts.out))
    for c in out:
        c.id = candidate_id(c.key)
        c.lexical = lexical_score(c, opts.instruction if opts.instruction is not None else "", opts.learned)
    return out


def candidate_id(key: str) -> str:
    """The id act and observe hand out for a candidate key."""
    return "c" + hashlib.sha256(hash_bytes(key)).hexdigest()[:32]


def describe_short(n: UINode) -> str:
    """A control in a few words: `Button "Save" (saveBtn)`, `TextField`."""
    out = n.role.removeprefix("AX")
    if n.label:
        out += f' "{truncate(n.label, 40)}"'
    if n.identifier and n.identifier != n.label:
        out += f" ({truncate(n.identifier, 30)})"
    return out


class _Drafts:
    def __init__(self, snap: Snapshot, opts: BuildOptions) -> None:
        self.snap = snap
        self.opts = opts
        self.out: list[ActionCandidate] = []
        self.ctx = control_context(snap, opts.instruction)
        self.with_text = opts.text is not None or opts.list_text_kinds
        self.by_index = {n.index: n for n in snap.nodes}

    def add(
        self,
        *,
        kind: ActionKind,
        key: str,
        summary: str,
        target: UINode | None = None,
        with_text: bool = False,
        destructive: bool = False,
        keys: list[str] | None = None,
        direction: ScrollDirection | None = None,
    ) -> ActionCandidate:
        c = ActionCandidate(
            id="",
            kind=kind,
            key=key,
            summary=summary,
            target=target,
            text=self.opts.text if with_text else None,
            destructive=destructive,
            keys=keys,
            direction=direction,
        )
        self.out.append(c)
        return c

    def shown(self) -> str:
        t = self.opts.text
        return "the given text" if t is None else quote(truncate(t, 60))

    def add_node(self, n: UINode) -> None:
        snap = self.snap
        if not n.enabled or not n.token:
            return
        # A sheet, dialog or open menu takes the input: only its own controls (and fields) are offered.
        if snap.modal is not None and not is_descendant(snap, n, snap.modal.index) and n.role not in EDITABLE_ROLES:
            return
        if n.role in _SCROLL_ROLES and n.frame is not None:
            for direction in _SCROLL_DIRECTIONS:
                self.add(
                    kind="scroll",
                    key=f"scroll|{direction}|{n.key}",
                    summary=f"scroll {describe_short(n)} {direction} one page",
                    target=n,
                    direction=direction,
                )
        if _is_chrome(n):
            return
        d = describe_short(n)
        if n.role == "AXIncrementor":
            if self.with_text:
                self.add(
                    kind="set_value",
                    key=f"set_value|{n.key}",
                    summary=f"set {d} to {self.shown()}",
                    target=n,
                    with_text=True,
                )
            self.add_steps(n, ("up", "Up arrow"), ("down", "Down arrow"))
        elif n.role in EDITABLE_ROLES:
            if self.with_text:
                s = self.shown()
                self.add(
                    kind="set_value",
                    key=f"set_value|{n.key}",
                    summary=f"replace the whole text of {d} with {s}",
                    target=n,
                    with_text=True,
                )
                self.add(
                    kind="type_into",
                    key=f"type_into|{n.key}",
                    summary=f"type {s} into {d} at its caret",
                    target=n,
                    with_text=True,
                )
                self.add(
                    kind="append",
                    key=f"append|{n.key}",
                    summary=f"add {s} at the end of {d}",
                    target=n,
                    with_text=True,
                )
            if n.role != "AXTextArea":
                self.add_click(n)
        elif n.role in _TOGGLE_ROLES:
            # A toggle can always be switched back, whatever its label says ("Send newsletter").
            on = n.value in ("1", "true")
            state = "switch" if n.value is None else "turn off" if on else "turn on"
            self.add(kind="toggle", key=f"toggle|{n.key}", summary=f"{state} {d}", target=n)
        elif n.role == "AXSlider":
            self.add_steps(n, ("right", "Right arrow"), ("left", "Left arrow"))
        elif n.role == "AXStaticText":
            parent = self.by_index.get(n.parent) if n.parent is not None else None
            if parent is not None and parent.role == "AXList" and n.label:
                self.add(
                    kind="click",
                    key=f"click|{n.key}",
                    summary=f'choose option "{truncate(n.label, 60)}" in {describe_short(parent)}',
                    target=n,
                )
        else:
            self.add_control(n, d)

    def add_control(self, n: UINode, d: str) -> None:
        destructive = is_destructive_control(n, self.ctx)
        if n.role == "AXPopUpButton" and self.with_text:
            self.add(
                kind="choose_option",
                key=f"choose|{n.key}",
                summary=f"choose {self.shown()} in {d}",
                target=n,
                with_text=True,
                destructive=destructive or is_destructive_label(self.opts.text or ""),
            )
        if n.role in _CLICK_ROLES and (n.role != "AXImage" or "press" in n.actions):
            self.add_click(n, destructive=destructive)
        if n.role in _CONTEXT_ROLES and n.label:
            self.add(
                kind="context_menu",
                key=f"context_menu|{n.key}",
                summary=f"open the context menu of {d} (right-click)",
                target=n,
            )

    def add_click(self, n: UINode, *, destructive: bool = False) -> None:
        self.add(
            kind="click", key=f"click|{n.key}", summary=f"click {describe_short(n)}", target=n, destructive=destructive
        )

    def add_steps(self, n: UINode, up: tuple[str, str], down: tuple[str, str]) -> None:
        d = describe_short(n)
        for verb, (key, name) in (("increase", up), ("decrease", down)):
            self.add(
                kind="key",
                key=f"key|{key}|{n.key}",
                summary=f"{verb} {d} by one step ({name} on it)",
                keys=[key],
                target=n,
            )

    def add_menu(self) -> None:
        snap = self.snap
        instruction = self.opts.instruction
        learned = self.opts.learned
        # The application menu here is the first one after Apple's.
        app_menu = next((top for top in (_top(m.path) for m in snap.menu) if top != "Apple"), None)
        # Destructive commands are offered too, flagged, so act can ask before running one.
        for m in offerable_menu_items(snap.menu, allow_destructive=True):
            if instruction is not None and clipboard_withheld(m.path, instruction, learned):
                continue
            c = self.add(
                kind="menu",
                key=m.key,
                summary="menu " + " > ".join(m.path),
                destructive=is_destructive_menu(m.path, app_menu)
                or is_destructive_menu([*m.path[:-1], english_title(m.path[-1], learned) or ""], app_menu),
            )
            c.menu = m
            eq = key_equivalent(m.path, snap.menu, learned)
            if eq is not None:
                c.shortcut = eq
            else:
                c.needs_foreground = True

    def add_keypad(self) -> None:
        text = self.opts.text
        if text is not None:
            keys = keypad_keys(text)
            presses = compile_keypad(keys, self.snap) if keys else None
            if keys and presses is not None:
                c = self.add(
                    kind="keypad",
                    key="keypad",
                    summary=f"press the on-screen keys {self.shown()} in order",
                    keys=keys,
                    with_text=True,
                )
                c.presses = presses
        elif self.opts.list_text_kinds and keypad_parents(self.snap):
            self.add(
                kind="keypad",
                key="keypad",
                summary="press the on-screen keys for the given text (digits and operators) in order",
            )


def _top(path: Sequence[str]) -> str | None:
    return path[0] if path else None


def _is_chrome(n: UINode) -> bool:
    if n.role in _CHROME_ROLES:
        return True
    return n.role == "AXButton" and not n.raw_label and not n.identifier and n.depth <= 1


def _dedupe(cands: Sequence[ActionCandidate]) -> list[ActionCandidate]:
    seen: Counter[str] = Counter()
    out: list[ActionCandidate] = []
    for c in cands:
        seen[c.key] += 1
        n = seen[c.key]
        out.append(replace(c, key=f"{c.key}#{n}") if n > 1 else c)
    return out


def _place_twins(cands: Sequence[ActionCandidate]) -> list[ActionCandidate]:
    """Look-alike candidates are told apart by their named container, then by order."""
    before = Counter(c.summary for c in cands)
    placed = [
        replace(c, summary=f"{c.summary} in {c.target.within[0]}")
        if before[c.summary] > 1 and c.target is not None and c.target.within and c.target.within[0]
        else c
        for c in cands
    ]
    total = Counter(c.summary for c in placed)
    seen: Counter[str] = Counter()
    out: list[ActionCandidate] = []
    for c in placed:
        n = total[c.summary]
        if n < 2:
            out.append(c)
            continue
        seen[c.summary] += 1
        out.append(replace(c, summary=f"{c.summary} (#{seen[c.summary]} of {n})"))
    return out
