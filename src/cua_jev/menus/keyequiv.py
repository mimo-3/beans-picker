"""Keyboard equivalents for menu commands."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

from cua_jev._text import WS, trim

if TYPE_CHECKING:
    from cua_jev.menus.menukeys import MenuKeyTable

type ShortcutSource = Literal["learned", "standard", "convention"]


@dataclass(slots=True, kw_only=True)
class KeyEquivalent:
    """A shortcut for a menu command: modifiers first, one key last."""

    keys: list[str]
    source: ShortcutSource


class HasPath(Protocol):
    """A menu item as far as shortcut lookup needs it: its title path, top-level menu first."""

    @property
    def path(self) -> Sequence[str]: ...


_STANDARD: Final[tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = (
    (("Undo", "取り消す"), ("cmd", "z")),
    (("Redo", "やり直す"), ("cmd", "shift", "z")),
    (("Cut", "カット"), ("cmd", "x")),
    (("Copy", "コピー"), ("cmd", "c")),
    (("Paste", "ペースト"), ("cmd", "v")),
    (("Paste and Match Style", "ペーストしてスタイルを合わせる"), ("cmd", "option", "shift", "v")),
    (("Select All", "すべてを選択"), ("cmd", "a")),
    (("Find", "検索"), ("cmd", "f")),
    (("Find and Replace", "検索と置換"), ("cmd", "option", "f")),
    (("Find Next", "次を検索"), ("cmd", "g")),
    (("Find Previous", "前を検索"), ("cmd", "shift", "g")),
    (("Use Selection for Find", "選択部分を検索に使用"), ("cmd", "e")),
    (("Jump to Selection", "選択部分へジャンプ"), ("cmd", "j")),
    (("Show Fonts", "Hide Fonts", "フォントパネルを表示", "フォントパネルを非表示"), ("cmd", "t")),
    (("Bold", "ボールド"), ("cmd", "b")),
    (("Italic", "イタリック"), ("cmd", "i")),
    (("Underline", "アンダーライン"), ("cmd", "u")),
    (("Show Colors", "Hide Colors", "カラーパネルを表示", "カラーパネルを非表示"), ("cmd", "shift", "c")),
    (("Copy Style", "スタイルをコピー"), ("cmd", "option", "c")),
    (("Paste Style", "スタイルをペースト"), ("cmd", "option", "v")),
    (("Show Ruler", "Hide Ruler", "ルーラを表示", "ルーラを非表示"), ("cmd", "r")),
    (("Copy Ruler", "ルーラをコピー"), ("cmd", "ctrl", "c")),
    (("Paste Ruler", "ルーラをペースト"), ("cmd", "ctrl", "v")),
    (("Make Rich Text", "Make Plain Text", "リッチテキストにする", "標準テキストにする"), ("cmd", "shift", "t")),
    (("Wrap to Page", "Wrap to Window", "ページサイズで表示", "ウインドウサイズで表示"), ("cmd", "shift", "w")),
    (("Show Properties", "プロパティを表示"), ("cmd", "option", "p")),
    (("Show Toolbar", "Hide Toolbar", "ツールバーを表示", "ツールバーを非表示"), ("cmd", "option", "t")),
    (("Show Sidebar", "Hide Sidebar", "サイドバーを表示", "サイドバーを非表示"), ("cmd", "ctrl", "s")),
    (("New", "新規", "New Document", "新規書類"), ("cmd", "n")),
)

# Items AppKit adds at run time: an autosaving app's Duplicate takes Save As's Shift-Cmd-S.
_RUNTIME: Final = ((("Duplicate", "複製"), ("cmd", "shift", "s")),)
_DISPLACED: Final = ((("Save As", "別名で保存"), ("cmd", "option", "shift", "s")),)

_TRAILING_ELLIPSIS: Final = re.compile(f"(?:\N{HORIZONTAL ELLIPSIS}|\\.\\.\\.){WS}*\\Z")
_WS_RUN: Final = re.compile(f"{WS}+")
_PRINTABLE_ASCII: Final = re.compile("[\\x20-\\x7e]+")
_VIEW_MENU: Final = re.compile("(?:view|表示)", re.IGNORECASE | re.ASCII)
_MAX_CONVENTION: Final = 9
_SEP: Final = "\x01"


def normalize_title(title: str) -> str:
    """A menu title as matched: no trailing ellipsis, whitespace runs collapsed, trimmed, lowercase."""
    return trim(_WS_RUN.sub(" ", _TRAILING_ELLIPSIS.sub("", title, count=1))).lower()


def _by_title(rows: Sequence[tuple[tuple[str, ...], tuple[str, ...]]]) -> Mapping[str, tuple[str, ...]]:
    return {normalize_title(t): keys for titles, keys in rows for t in titles}


def _english_names() -> Mapping[str, str]:
    out: dict[str, str] = {}
    for titles, _keys in _STANDARD:
        english = [t for t in titles if _PRINTABLE_ASCII.fullmatch(t)]
        localized = [t for t in titles if not _PRINTABLE_ASCII.fullmatch(t)]
        for i, t in enumerate(localized):
            out[normalize_title(t)] = english[i % len(english)]
    return out


_BY_TITLE: Final = _by_title(_STANDARD)
_RUNTIME_BY_TITLE: Final = _by_title(_RUNTIME)
_DISPLACED_BY_TITLE: Final = _by_title(_DISPLACED)
_ENGLISH: Final = _english_names()


def english_title(leaf: str, learned: MenuKeyTable | None = None) -> str | None:
    """The English name of a localized menu command."""
    found = _ENGLISH.get(normalize_title(leaf))
    if found is not None:
        return found
    return learned.english_title(leaf) if learned is not None else None


def _joined(path: Sequence[str]) -> str:
    return _SEP.join(path)


def _last(path: Sequence[str]) -> str:
    return path[-1] if path else ""


def _top(path: Sequence[str]) -> str | None:
    return path[0] if path else None


def _same_keys(a: Sequence[str] | None, b: Sequence[str]) -> bool:
    return a is not None and list(a) == list(b)


def key_equivalent(
    path: Sequence[str], menu: Sequence[HasPath], learned: MenuKeyTable | None = None
) -> KeyEquivalent | None:
    """The shortcut for the menu item at `path`, or None."""
    leaf = _last(path)
    if not leaf:
        return None
    top = path[0]
    if learned is not None:
        own = learned.lookup(path)
        if own is not None:
            return _learned_key(path, own.keys, menu, learned)
    # Only next to the item it displaced; elsewhere Duplicate is the app's own command.
    runtime = _RUNTIME_BY_TITLE.get(normalize_title(leaf))
    if runtime is not None and any(
        _top(m.path) == top and normalize_title(_last(m.path)) in _DISPLACED_BY_TITLE for m in menu
    ):
        return KeyEquivalent(keys=list(runtime), source="standard")
    std = _BY_TITLE.get(normalize_title(leaf))
    if std is not None:
        return KeyEquivalent(keys=list(std), source="standard")
    if len(path) != 2 or not _VIEW_MENU.fullmatch(top):
        return None
    run = _view_mode_run(top, menu)
    if leaf in run:
        i = run.index(leaf)
        if i < _MAX_CONVENTION:
            return KeyEquivalent(keys=["cmd", str(i + 1)], source="convention")
    return None


def _learned_key(
    path: Sequence[str], own: Sequence[str], menu: Sequence[HasPath], learned: MenuKeyTable
) -> KeyEquivalent | None:
    """AppKit may hand a shortcut to an item added at run time (Save As's Shift-Cmd-S goes to Duplicate)."""
    taken = any(
        _top(m.path) == path[0]
        and _joined(m.path) != _joined(path)
        and learned.lookup(m.path) is None
        and _same_keys(_RUNTIME_BY_TITLE.get(normalize_title(_last(m.path))), own)
        for m in menu
    )
    if not taken:
        return KeyEquivalent(keys=list(own), source="learned")
    moved = _DISPLACED_BY_TITLE.get(normalize_title(_last(path)))
    if moved is not None and not _same_keys(moved, own):
        return KeyEquivalent(keys=list(moved), source="standard")
    return None


def view_modes(path: Sequence[str], menu: Sequence[HasPath]) -> list[str] | None:
    """The sibling views when `path` is one of the View menu's leading modes (two or more), else None."""
    if len(path) != 2 or not _VIEW_MENU.fullmatch(path[0]):
        return None
    run = _view_mode_run(path[0], menu)
    return run if len(run) >= 2 and path[1] in run else None


def _view_mode_run(top: str, menu: Sequence[HasPath]) -> list[str]:
    parents = {_joined(m.path[:-1]) for m in menu}
    out: list[str] = []
    for m in menu:
        if len(m.path) != 2 or m.path[0] != top:
            continue
        leaf = m.path[1]
        if _TRAILING_ELLIPSIS.search(leaf) or _joined(m.path) in parents or normalize_title(leaf) in _BY_TITLE:
            break
        out.append(leaf)
    return out
