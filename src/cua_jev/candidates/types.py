"""Candidate actions: what can be done on a window, one entry per control, menu command or key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from cua_jev.menus.keyequiv import KeyEquivalent, ShortcutSource
from cua_jev.observe.types import MenuItem, UINode

type ActionKind = Literal[
    "click",
    "toggle",
    "choose_option",
    "set_value",
    "type_into",
    "append",
    "menu",
    "keypad",
    "key",
    "scroll",
    "context_menu",
]
type ScrollDirection = Literal["up", "down"]

# Kinds that enter the caller's `text` and cannot run without it.
TEXT_KINDS: Final[frozenset[ActionKind]] = frozenset({"choose_option", "set_value", "type_into", "append", "keypad"})

# A menu command's shortcut that runs it in the background.
Shortcut = KeyEquivalent

__all__ = [
    "TEXT_KINDS",
    "ActionCandidate",
    "ActionKind",
    "ScrollDirection",
    "Shortcut",
    "ShortcutSource",
]


@dataclass(slots=True, kw_only=True)
class ActionCandidate:
    """One action on a snapshot.

    Optional fields are None when they do not apply to the kind; `text` is None when the caller gave
    no text and may be "" when the caller's text is empty.
    """

    id: str
    """Short id derived from `key`: the same control gets the same id in every snapshot."""
    kind: ActionKind
    key: str
    """Stable identity (never a snapshot index): role, identifier, label and ancestors of the target."""
    summary: str
    """Short human-readable summary."""
    destructive: bool = False
    """Operating it cannot be undone (deletes, closes, sends, overwrites ...): act asks for allowDestructive."""
    lexical: int = 0
    """Deterministic lexical relevance to the instruction."""
    target: UINode | None = None
    menu: MenuItem | None = None
    text: str | None = None
    """The caller's text, for the kinds in TEXT_KINDS. Entered as given, never trimmed."""
    keys: list[str] | None = None
    """key: key names, modifiers first (["shift", "f10"]); keypad: the key sequence."""
    direction: ScrollDirection | None = None
    """scroll: which way the target moves its content, one page."""
    presses: list[UINode] | None = None
    """keypad: the buttons to press in order."""
    shortcut: Shortcut | None = None
    """menu: the shortcut that runs it in the background; None when it would need the foreground."""
    needs_foreground: bool | None = None
    """menu: True when it cannot run without bringing the app to the front (no known shortcut)."""
