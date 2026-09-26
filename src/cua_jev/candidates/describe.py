"""The Jev criterion object for a candidate action."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from cua_jev._json import JsonObject, JsonValue
from cua_jev.candidates.keypad import GLYPH
from cua_jev.candidates.types import ActionCandidate, ActionKind
from cua_jev.menus.keyequiv import english_title, view_modes
from cua_jev.menus.menukeys import MenuKeyTable
from cua_jev.observe.normalize import truncate
from cua_jev.observe.types import Snapshot

_EFFECT_HINT: Final[Mapping[ActionKind, str]] = {
    "click": "presses this control",
    "toggle": "switches this control to the other state",
    "choose_option": "selects the option named `text` in this pop-up",
    "set_value": "replaces the whole content of the field with `text`",
    "type_into": "inserts `text` at the field's current caret position (which may be at the start of existing text)",
    "append": "adds `text` at the very end of the field, after everything it already contains",
    "menu": "chooses this menu command",
    "keypad": "presses the on-screen keys for `text`, in order",
    "key": "presses this key",
    "scroll": (
        "scrolls this area by one page; what is scrolled out of view is not in the window's tree, "
        "so rows past the visible ones appear only after scrolling"
    ),
    "context_menu": (
        "opens this item's context menu, as a right-click does; its commands "
        "(rename, star, move to trash \N{HORIZONTAL ELLIPSIS}) then appear as menu items to press"
    ),
}


def describe(c: ActionCandidate, snap: Snapshot, learned: MenuKeyTable | None = None) -> JsonObject:
    """What Jev is told about one candidate, keys in a fixed order."""
    d: JsonObject = {"action": c.kind}
    t = c.target
    if t is not None:
        d["role"] = t.role
        if t.label:
            d["name"] = truncate(t.label, 80)
        # AppKit's own private identifiers ("_NS:123") say nothing about the control.
        if t.identifier and not t.identifier.startswith("_"):
            d["identifier"] = truncate(t.identifier, 60)
        if t.help and t.help != t.label:
            d["help"] = truncate(t.help, 80)
        if t.value is not None:
            d["current_value"] = truncate(t.value, 80)
        if t.within:
            within: list[JsonValue] = [truncate(w, 40) for w in t.within[:3]]
            d["within"] = within
        twins = [n.index for n in snap.nodes if n.role == t.role and n.label == t.label]
        if len(twins) > 1:
            pos = twins.index(t.index) + 1 if t.index in twins else 0
            d["occurrence"] = f"{pos} of {len(twins)}"
        # A showmenu+cancel control may open a pop-up menu that is not in the window's tree.
        if may_only_open_popup(c):
            d["pop_up"] = "pressing it may only open a pop-up menu whose choices are not in this list"
    if c.menu is not None:
        path = c.menu.path
        d["path"] = " > ".join(path)
        english = english_title(path[-1] if path else "", learned)
        if english:
            d["standard_command"] = f"{english} (the stock macOS command this localized item is)"
        views = view_modes(path, snap.menu)
        if views is not None:
            d["view"] = truncate(
                f"one of the window's views ({', '.join(views)}); switching views changes which controls are shown",
                200,
            )
        if c.needs_foreground:
            d["availability"] = "cannot run while the app stays in the background (no keyboard shortcut)"
    if c.kind == "keypad" and c.keys is not None and c.presses is not None:
        d["sequence"] = "".join(GLYPH.get(k, k) for k in c.keys)
        via: list[JsonValue] = [p.identifier if p.identifier is not None else p.label for p in c.presses]
        d["via"] = via
    if c.kind == "key":
        d["keys"] = "+".join(c.keys or [])
    if c.kind == "scroll":
        d["direction"] = c.direction or "down"
    d["effect_hint"] = _EFFECT_HINT[c.kind]
    return d


def may_only_open_popup(c: ActionCandidate) -> bool:
    """A click on a control exposing showmenu+cancel, which may only open an invisible pop-up menu."""
    t = c.target
    return c.kind == "click" and t is not None and "showmenu" in t.actions and "cancel" in t.actions
