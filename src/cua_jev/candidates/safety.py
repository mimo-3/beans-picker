"""Code-level marking of destructive actions.

act never runs a destructive action without allowDestructive, whoever picked it. Every pattern with
a word boundary or a case-insensitive English word is ASCII-only: a word boundary sits next to
``[A-Za-z0-9_]`` only, and only ASCII letters fold case.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cua_jev._text import NOT_LINE_END, WS, trim
from cua_jev.candidates.keypad import keypad_parents
from cua_jev.menus.keyequiv import english_title
from cua_jev.menus.menukeys import MenuKeyTable
from cua_jev.observe.types import Snapshot, UINode

_FLAGS: Final = re.IGNORECASE | re.ASCII
_ELLIPSIS: Final = "\N{HORIZONTAL ELLIPSIS}"

_AVOID: Final = re.compile(
    "delete|erase|remove|quit|close|log ?out|sign ?out|empty trash|purchase|buy|send|share|restart"
    "|shut ?down|force quit|lock screen|sleep|hide|clear menu|revert"
    "|don['\N{RIGHT SINGLE QUOTATION MARK}]?t save|discard|trash"
    f"|\\breplace\\b(?!{_ELLIPSIS}|\\.\\.\\.)|overwrite|reset|uninstall"
    "|保存しない|ゴミ箱|置き換え|すべてを置換|破棄|初期化|上書き|削除|終了|閉じる|再起動|ログアウト|ロック|スリープ"
    "|非表示|共有|メニューを消去|バージョンを戻す|しまう",
    _FLAGS,
)

# Roles whose only action here is entering text into the field itself.
_TEXT_ENTRY_ROLES: Final = frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"})

# A closing parenthesis or bracket key types a character; "close" there closes nothing.
_CLOSING_MARK: Final = re.compile(
    f"\\b(?:close|right)(?:{WS}|-)?(?:paren(?:thesis)?|bracket|brace)s?\\b|(?:右|閉じ)(?:丸)?(?:かっこ|括弧|カッコ)",
    _FLAGS,
)
# "Hide" in a window's own control shows or hides part of that window ("Hide Binary",
# "バイナリを非表示"). Hiding the app or other apps happens in the app menu, never from a window
# control, but a control named only "Hide", or "Hide Others", stays withheld.
_VIEW_HIDE: Final = re.compile("\\bhide\\b|非表示", _FLAGS)
_APP_HIDE: Final = re.compile(
    f"\\A{WS}*(?:hide|非表示){WS}*\\Z"
    f"|\\bhide{WS}+(?:others|other applications|all)\\b"
    "|(?:ほか|他|その他|すべて)を非表示",
    _FLAGS,
)
# On an on-screen keypad, delete and clear edit only the entry shown on its display (a backspace
# key, AC). Those words are harmless there; any other destructive word still counts.
_ENTRY_EDIT: Final = re.compile("delete|backspace|erase|clear|削除|消去|クリア", _FLAGS)
# A popover's own close button dismisses a transient overlay, like Escape does: nothing is closed
# but the popover itself. Only a button whose whole label is that word, and whose parent is the
# popover, counts; a sheet's or dialog's Close still counts as destructive, since it can discard
# what the sheet holds.
_POPOVER_CLOSE: Final = re.compile(f"{WS}*(?:close|dismiss|閉じる){WS}*", _FLAGS)
# A find bar's Replace and Replace All edit only the document's text, which Undo restores: when the
# goal itself asks to replace text, they do what was asked. The same words in a sheet or dialog
# still count (a save panel's "Replace" overwrites a file).
_TEXT_REPLACE: Final = re.compile(f"\\breplace(?:{WS}+all)?\\b|すべてを置換|置換|置き換え", _FLAGS)

_GOAL_REPLACES: Final = re.compile("\\breplac(?:e|es|ing)\\b|置き?換え|置換", _FLAGS)
_OPEN_QUOTE: Final = "[\"\N{LEFT DOUBLE QUOTATION MARK}'\N{LEFT CORNER BRACKET}]"
_CLOSE_QUOTE: Final = "[\"\N{RIGHT DOUBLE QUOTATION MARK}'\N{RIGHT CORNER BRACKET}]"
_GOAL_CHANGES: Final = re.compile(
    f"\\bchange{WS}+{_OPEN_QUOTE}{NOT_LINE_END}+?{_CLOSE_QUOTE}{WS}+(?:to|into){WS}+{_OPEN_QUOTE}",
    _FLAGS,
)
_GOAL_CLIPBOARD: Final = re.compile(
    "\\b(?:cut|copy|copies|clipboard|paste|duplicate|move)\\b|カット|コピー|クリップボード|ペースト", _FLAGS
)
_TRAILING_ELLIPSIS: Final = re.compile(f"(?:{_ELLIPSIS}|\\.\\.\\.){WS}*\\Z")
_CLIPBOARD_WRITE: Final = re.compile("(?:cut|copy)", _FLAGS)
_DIALOG_ROLES: Final = frozenset({"AXSheet", "AXDialog"})
_DIALOG_SUBROLES: Final = frozenset({"AXDialog", "AXSystemDialog"})
_MAX_HOPS: Final = 64


def is_destructive_label(text: str) -> bool:
    """Whether `text` names something that cannot be undone (delete, close, send, quit, ...)."""
    return _AVOID.search(text) is not None


def goal_replaces_text(goal: str) -> bool:
    """Whether an instruction asks to replace or change existing text.

    For example 'replace "cat" with "dog"', 'change "a" to "b"' or 「猫を犬に置き換えて」.
    """
    return _GOAL_REPLACES.search(goal) is not None or _GOAL_CHANGES.search(goal) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlContext:
    """What `is_destructive_control` needs to know about one snapshot and the instruction."""

    keypad_parents: frozenset[int] = frozenset()
    """Parents (node indices) holding an on-screen keypad."""
    popovers: frozenset[int] = frozenset()
    """Node indices of open popovers (AXPopover)."""
    replaces_text: bool = False
    """The instruction asks to replace text (`goal_replaces_text`)."""
    in_dialog: frozenset[int] = frozenset()
    """Node indices inside a sheet or dialog (only computed when `replaces_text`)."""


def control_context(snap: Snapshot, goal: str | None = None) -> ControlContext:
    """The context `is_destructive_control` needs for one snapshot (and, when known, the instruction)."""
    keypads = frozenset(keypad_parents(snap))
    popovers = frozenset(n.index for n in snap.nodes if n.role == "AXPopover")
    if goal is None or not goal_replaces_text(goal):
        return ControlContext(keypad_parents=keypads, popovers=popovers)
    by_index = {n.index: n for n in snap.nodes}
    in_dialog = frozenset(n.index for n in snap.nodes if _inside_dialog(n, by_index))
    return ControlContext(keypad_parents=keypads, popovers=popovers, replaces_text=True, in_dialog=in_dialog)


def _inside_dialog(n: UINode, by_index: dict[int, UINode]) -> bool:
    """Whether a strict ancestor of `n` (at most 64 levels up) is a sheet or dialog."""
    p = by_index.get(n.parent) if n.parent is not None else None
    hops = 0
    while p is not None and hops < _MAX_HOPS:
        if p.role in _DIALOG_ROLES or p.subrole in _DIALOG_SUBROLES:
            return True
        p = by_index.get(p.parent) if p.parent is not None else None
        hops += 1
    return False


def is_destructive_control(n: UINode, ctx: ControlContext | None = None) -> bool:
    """Whether operating this control could be destructive.

    A text field never is: typing changes only the field, and its name is often just its placeholder
    or content (a "Replace" field is named after the word it shows), so filtering it by name would
    hide the harmless input and leave the button that acts. Buttons are judged by label, identifier
    and help together, after removing the words that are harmless in context (a closing-parenthesis
    key, a view's own Hide toggle, a keypad's delete key, a find bar's Replace when the goal asks to
    replace text).
    """
    ctx = ctx if ctx is not None else ControlContext()
    if n.role in _TEXT_ENTRY_ROLES:
        return False
    if (
        n.role == "AXButton"
        and n.parent is not None
        and n.parent in ctx.popovers
        and _POPOVER_CLOSE.fullmatch(n.label)
        and not is_destructive_label(n.help or "")
    ):
        return False
    parts = (n.label, n.identifier or "", n.help or "")
    text = _CLOSING_MARK.sub(" ", " ".join(parts))
    if not any(_APP_HIDE.search(p) for p in parts):
        text = _VIEW_HIDE.sub(" ", text)
    if n.parent is not None and n.parent in ctx.keypad_parents:
        text = _ENTRY_EDIT.sub(" ", text)
    if ctx.replaces_text and n.index not in ctx.in_dialog:
        text = _TEXT_REPLACE.sub(" ", text)
    return is_destructive_label(text)


def is_destructive_menu(path: Sequence[str], app_menu: str | None = None) -> bool:
    """Whether a menu command could be destructive.

    A menu's own "Hide ..." shows or hides part of the window (a toolbar, a separator). In the
    application menu (the first menu after Apple's) it hides the app or other apps, and there it
    still counts.
    """
    text = " ".join(path)
    top = path[0] if path else None
    if top != app_menu and not any(_APP_HIDE.search(p) for p in path):
        text = _VIEW_HIDE.sub(" ", text)
    return is_destructive_label(text)


def writes_clipboard(english_leaf: str) -> bool:
    """Whether a menu command, by its English name, overwrites the system clipboard (Cut, Copy)."""
    return _CLIPBOARD_WRITE.fullmatch(trim(_TRAILING_ELLIPSIS.sub("", english_leaf, count=1))) is not None


def clipboard_withheld(path: Sequence[str], goal: str, learned: MenuKeyTable | None = None) -> bool:
    """A Cut or Copy menu item the instruction does not ask for.

    Overwriting the clipboard is a side effect outside the app, so Jev is offered these only when
    the instruction is about the clipboard (cut, copy, paste, move ...).
    """
    leaf = path[-1] if path else ""
    english = english_title(leaf, learned)
    return writes_clipboard(english if english is not None else leaf) and not goal_uses_clipboard(goal)


def goal_uses_clipboard(goal: str) -> bool:
    """Whether an instruction is about the clipboard (cut, copy, paste, duplicate, move)."""
    return _GOAL_CLIPBOARD.search(goal) is not None
