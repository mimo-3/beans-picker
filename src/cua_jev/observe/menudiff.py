"""Menu items retitled in place and menu items whose enabled state flipped."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, TypedDict

from cua_jev._json import JsonObject
from cua_jev.observe.types import MenuItem

_SEP: Final = "\x01"

# AppKit's standard Window menu, recognized by its stock actions whatever the UI language.
_WINDOW_MENU_ACTION: Final = re.compile(
    r"(?:arrangeInFront|miniaturizeAll|performMiniaturize|performZoom|zoomAll|alternateArrangeInFront):?"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class MenuRelabel:
    """The item in slot `from_` of the submenu at `parent` now reads `to`."""

    parent: list[str]
    from_: str
    to: str

    def to_json(self) -> JsonObject:
        """`{"parent": [...], "from": ..., "to": ...}`."""
        return {"parent": list(self.parent), "from": self.from_, "to": self.to}


class EnabledChanges(TypedDict):
    enabled: list[str]
    disabled: list[str]


def _last(path: Sequence[str]) -> str:
    return path[-1] if path else ""


def _groups(menu: Sequence[MenuItem]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in menu:
        out.setdefault(_SEP.join(m.path[:-1]), []).append(_last(m.path))
    return out


def relabeled_menu_items(before: Sequence[MenuItem], after: Sequence[MenuItem], limit: int = 6) -> list[MenuRelabel]:
    """Retitled slots of every submenu whose item count did not change (at most `limit`)."""
    now_by_parent = _groups(after)
    out: list[MenuRelabel] = []
    for parent, was in _groups(before).items():
        now = now_by_parent.get(parent)
        if now is None or len(now) != len(was):
            continue
        for w, n in zip(was, now, strict=True):
            if len(out) >= limit:
                break
            if w != n:
                out.append(MenuRelabel(parent=parent.split(_SEP), from_=w, to=n))
    return out


def relabel_of(path: Sequence[str], before: Sequence[MenuItem], after: Sequence[MenuItem]) -> str | None:
    """The new title of the slot `path` occupied, when that exact item was retitled."""
    parent = _SEP.join(path[:-1])
    was = _groups(before).get(parent)
    now = _groups(after).get(parent)
    if was is None or now is None or len(was) != len(now):
        return None
    title = _last(path)
    if was.count(title) != 1:
        return None
    i = was.index(title)
    return now[i] if now[i] != was[i] else None


def enabled_changes(before: Sequence[MenuItem], after: Sequence[MenuItem], limit: int = 6) -> EnabledChanges:
    """Items whose enabled state flipped, named by their path below the menu title."""
    was = {_SEP.join(m.path): m.enabled for m in before}
    window_menus = set(_window_menu_titles(before)) | set(_window_menu_titles(after))
    enabled: list[str] = []
    disabled: list[str] = []
    for m in after:
        prev = was.get(_SEP.join(m.path))
        if prev is None or prev == m.enabled:
            continue
        if (m.path[0] if m.path else "") in window_menus:
            continue
        name = " > ".join(m.path[1:])
        if m.enabled and len(enabled) < limit:
            enabled.append(name)
        if not m.enabled and len(disabled) < limit:
            disabled.append(name)
    return {"enabled": enabled, "disabled": disabled}


def _window_menu_titles(menu: Sequence[MenuItem]) -> list[str]:
    titles: dict[str, None] = {}
    for m in menu:
        if _WINDOW_MENU_ACTION.fullmatch(m.identifier or ""):
            titles[m.path[0] if m.path else ""] = None
    return list(titles)
