"""Which menu-bar items are offered as candidate actions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from cua_jev._text import NOT_LINE_END
from cua_jev.candidates.safety import is_destructive_label
from cua_jev.observe.types import MenuItem

_SEP: Final = "\x01"
_SKIP_TOP: Final = frozenset({"Apple"})
_SKIP_SUBMENU: Final = re.compile(
    "(?:services|サービス|open recent|最近使った項目を開く|最近使った項目|移動とサイズ変更|move & resize"
    f"|<<{NOT_LINE_END}*>>|自動入力|autofill|作文ツール|writing tools|スピーチ|speech)",
    re.IGNORECASE | re.ASCII,
)
_APP_MENU_ACTION: Final = re.compile(
    "(?:hide|hideOtherApplications|unhideAllApplications|terminate|NSAlternateQuitMenuItem):?"
)
_PLACEHOLDER: Final = re.compile(f"<<{NOT_LINE_END}*>>")
_HELP_MENU: Final = re.compile("(?:help|ヘルプ)", re.IGNORECASE | re.ASCII)


def offerable_menu_items(
    menu: Sequence[MenuItem], *, allow_destructive: bool = False, include_disabled: bool = False
) -> list[MenuItem]:
    """Leaf menu items that may be offered, in menu order."""
    parents = {_SEP.join(m.path[:-1]) for m in menu}
    app_menu = next(
        (m.path[0] if m.path else None for m in menu if _APP_MENU_ACTION.fullmatch(m.identifier or "")), None
    )
    return [
        m
        for m in menu
        if _offerable(m, parents, app_menu, include_disabled=include_disabled)
        and (allow_destructive or not is_destructive_label(" ".join(m.path)))
    ]


def _offerable(m: MenuItem, parents: set[str], app_menu: str | None, *, include_disabled: bool) -> bool:
    path = m.path
    top = path[0] if path else None
    if (not m.token) if m.enabled else (not include_disabled):
        return False
    if (top if top is not None else "") in _SKIP_TOP:
        return False
    # Stock app-menu items carry identifiers; a disabled one shows none, so unidentified items go too.
    if app_menu is not None and top == app_menu and (not m.identifier or _APP_MENU_ACTION.fullmatch(m.identifier)):
        return False
    if any(_SKIP_SUBMENU.fullmatch(p) for p in path[1:-1]):
        return False
    if _SEP.join(path) in parents:
        return False
    if any(_PLACEHOLDER.fullmatch(p) for p in path):
        return False
    if len(path) == 2 and path[0] == path[1]:
        return False
    return not (len(path) == 2 and _HELP_MENU.fullmatch(path[0]))
