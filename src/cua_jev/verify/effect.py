"""Per-action effect verification."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from cua_jev._json import quote
from cua_jev._text import utf16_len, utf16_slice, utf16_units
from cua_jev.jev.state import change_of, changed
from cua_jev.observe.menudiff import relabel_of
from cua_jev.observe.normalize import normalize_text

if TYPE_CHECKING:
    from cua_jev.candidates.types import ActionCandidate, ActionKind
    from cua_jev.jev.state import Change
    from cua_jev.observe.types import Snapshot, UINode

type VerifyEffect = Literal["ok", "wrong", "none", "unverified"]
"""The verifier's judgment (a different set from cua-driver's own `effect` word):
ok, the effect asked for is observed; wrong, something changed but not that; none, nothing changed;
unverified, the field changed but its exact text cannot be read to confirm it."""

TEXT_ENTRY: Final[frozenset[ActionKind]] = frozenset({"set_value", "type_into", "append", "choose_option"})

_CLIP: Final = 120
_SUMMARY_LIMIT: Final = 400
_ELLIPSIS: Final = "\u2026"
_ARROW: Final = "\u2192"


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectVerdict:
    """The judgment on one action."""

    effect: VerifyEffect
    detail: str
    exact: bool | None = None


def verify_effect(c: ActionCandidate, before: Snapshot, after: Snapshot) -> EffectVerdict:
    """Judges what the action on `c` did, from the snapshots before and after it."""
    diff = change_of(before, after)
    moved = before.signature != after.signature or changed(diff)
    if c.kind in TEXT_ENTRY:
        return _verify_text(c, before, after, moved)
    if c.kind == "toggle":
        target = c.target
        node = _by_key(after, target)
        if target is not None and node is not None and node.value != target.value:
            return EffectVerdict(effect="ok", detail=f"value {_shown(target.value)} {_ARROW} {_shown(node.value)}")
        if moved:
            return EffectVerdict(effect="wrong", detail="the screen changed but the control's state did not flip")
        return EffectVerdict(effect="none", detail="unchanged")
    if c.kind == "menu" and c.menu is not None:
        # A panel outside our window (Fonts, Colors) shows only as the item's new title.
        to = relabel_of(c.menu.path, before.menu, after.menu)
        if to:
            return EffectVerdict(effect="ok", detail=f'menu item now reads "{to}"')
    if moved:
        return EffectVerdict(effect="ok", detail=summarize(diff))
    return EffectVerdict(effect="none", detail="unchanged")


def expected_text(kind: ActionKind, was: str, text: str) -> Callable[[str], bool] | None:
    """The test the field's exact text must pass after the action; None for kinds that enter no text."""
    match kind:
        case "set_value" | "choose_option":
            return lambda now: now == text
        case "append":
            return lambda now: now == was + text
        case "type_into":
            return lambda now: _inserted_once(utf16_units(was), utf16_units(text), utf16_units(now))
        case _:
            return None


def _inserted_once(was: bytes, text: bytes, now: bytes) -> bool:
    if len(now) != len(was) + len(text):
        return False
    for i in range(0, len(was) + 1, 2):
        if now.startswith(was[:i]) and now.startswith(text, i) and now[i + len(text) :] == was[i:]:
            return True
    return False


def _verify_text(c: ActionCandidate, before: Snapshot, after: Snapshot, moved: bool) -> EffectVerdict:
    text = c.text if c.text is not None else ""
    # Only the stable key: after the UI changed, the old index can name a different element.
    was = _by_key(before, c.target)
    now = _by_key(after, c.target)
    if c.target is None or now is None:
        return EffectVerdict(effect="wrong" if moved else "none", detail="the target field is gone")
    exact = _exactness(c.kind, was, now, text)
    old, new = _value_of(was), _value_of(now)
    if not exact:
        # cua-driver's reading is trimmed, so it can show a change but never the exact text.
        if new != old or (c.kind == "choose_option" and new == normalize_text(text)):
            return EffectVerdict(
                effect="unverified",
                detail=f"the field changed; cua-driver reads {quote(clip(new))} (whitespace at the ends trimmed), "
                "and the exact text could not be read",
                exact=exact,
            )
        return _unchanged_field(moved, exact)
    expect = expected_text(c.kind, old, text)
    if expect is not None and expect(new):
        role = c.target.role.removeprefix("AX")
        return EffectVerdict(effect="ok", detail=f"{role} reads exactly {quote(clip(new))}", exact=exact)
    if new != old:
        return EffectVerdict(
            effect="wrong", detail=f"the field reads {quote(clip(new))}, not what was asked", exact=exact
        )
    return _unchanged_field(moved, exact)


def _unchanged_field(moved: bool, exact: bool) -> EffectVerdict:
    if moved:
        return EffectVerdict(effect="wrong", detail="the screen changed but the field did not", exact=exact)
    return EffectVerdict(effect="none", detail="unchanged", exact=exact)


def _exactness(kind: ActionKind, was: UINode | None, now: UINode, text: str) -> bool:
    """Field text is exact only when read natively, since cua-driver trims whitespace."""
    if kind == "choose_option":
        return text == normalize_text(text)
    return now.exact is True and (kind == "set_value" or (was is not None and was.exact is True))


def _by_key(snap: Snapshot, target: UINode | None) -> UINode | None:
    if target is None:
        return None
    return next((n for n in snap.nodes if n.key == target.key), None)


def _value_of(n: UINode | None) -> str:
    if n is None:
        return ""
    if n.raw_value is not None:
        return n.raw_value
    return n.value if n.value is not None else ""


def _shown(v: str | None) -> str:
    return v if v is not None else "(none)"


def clip(s: str) -> str:
    """At most 120 UTF-16 units, the last one an ellipsis when cut."""
    return s if utf16_len(s) <= _CLIP else utf16_slice(s, _CLIP - 1) + _ELLIPSIS


def summarize(d: Change) -> str:
    """One line naming what changed on the window, at most 400 UTF-16 units."""
    parts: list[str] = []
    if "window_title" in d:
        parts.append(f'window title now "{d["window_title"]}"')
    parts.extend(d.get("fields_changed", []))
    texts = d.get("texts_changed", [])
    if texts:
        parts.append("new text: " + " | ".join(texts))
    parts.extend(d.get("menu_items_retitled", []))
    if d["appeared"]:
        parts.append("appeared: " + ", ".join(d["appeared"][:5]))
    if d["disappeared"]:
        parts.append("disappeared: " + ", ".join(d["disappeared"][:5]))
    return utf16_slice("; ".join(parts) or "the window's state changed", _SUMMARY_LIMIT)
