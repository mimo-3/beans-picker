"""Compile a key sequence into on-screen button presses.

Buttons are found by AXIdentifier first (locale-independent), then by label or glyph.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from cua_jev._text import DIGIT, WS
from cua_jev.observe.types import Snapshot, UINode

_BY_ID: Final[Mapping[str, tuple[str, ...]]] = {
    "0": ("Zero",),
    "1": ("One",),
    "2": ("Two",),
    "3": ("Three",),
    "4": ("Four",),
    "5": ("Five",),
    "6": ("Six",),
    "7": ("Seven",),
    "8": ("Eight",),
    "9": ("Nine",),
    "+": ("Add", "Plus"),
    "-": ("Subtract", "Minus"),
    "*": ("Multiply", "Times"),
    "/": ("Divide",),
    "=": ("Equals",),
    ".": ("Decimal", "Point"),
    "^": ("Power",),
    "\N{SQUARE ROOT}": ("SquareRoot",),
    "\N{PLUS-MINUS SIGN}": ("Negate", "ChangeSign", "PlusMinus"),
}

_BY_LABEL: Final[Mapping[str, tuple[str, ...]]] = {
    "+": ("+", "加算", "add", "plus"),
    "-": ("-", "\N{MINUS SIGN}", "減算", "subtract", "minus"),
    "*": ("\N{MULTIPLICATION SIGN}", "*", "乗算", "multiply"),
    "/": ("\N{DIVISION SIGN}", "/", "除算", "divide"),
    "=": ("=", "計算実行", "equals"),
    ".": (".", "小数点", "decimal"),
    "^": ("x\N{MODIFIER LETTER SMALL Y}", "x^y", "xのy乗", "power"),
    "\N{SQUARE ROOT}": (
        "\N{SQUARE ROOT}",
        "\N{SQUARE ROOT}x",
        "\N{SUPERSCRIPT TWO}\N{SQUARE ROOT}x",
        "平方根",
        "square root",
    ),
    "\N{PLUS-MINUS SIGN}": (
        "\N{PLUS-MINUS SIGN}",
        "+/-",
        "+/\N{MINUS SIGN}",
        "記号を変更",
        "change sign",
        "negate",
    ),
}

# How keys are shown to Jev: the calculator glyphs for the ASCII operators.
GLYPH: Final[Mapping[str, str]] = {"*": "\N{MULTIPLICATION SIGN}", "/": "\N{DIVISION SIGN}", "-": "\N{MINUS SIGN}"}

# Typed glyphs to keys; a lowercase ASCII x also means multiply ("3x4").
_FROM_GLYPH: Final[Mapping[str, str]] = {
    "\N{MULTIPLICATION SIGN}": "*",
    "\N{DIVISION SIGN}": "/",
    "\N{MINUS SIGN}": "-",
    "x": "*",
}

_WS_RUN: Final = re.compile(f"{WS}+")
_ONE_DIGIT: Final = re.compile(DIGIT)
_DIGIT_KEYS: Final = tuple((k, ids) for k, ids in _BY_ID.items() if _ONE_DIGIT.fullmatch(k))


def keypad_keys(text: str) -> list[str] | None:
    """The key sequence for text entered on an on-screen keypad ("12*7=" or "12x7=" -> 1 2 * 7 =).

    Whitespace is skipped; any other character without a key makes it None, as does empty text.
    """
    keys = [_FROM_GLYPH.get(ch, ch) for ch in _WS_RUN.sub("", text)]
    if not keys or any(not _ONE_DIGIT.fullmatch(k) and k not in _BY_ID for k in keys):
        return None
    return keys


def compile_keypad(keys: Sequence[str], snap: Snapshot) -> list[UINode] | None:
    """The buttons to press for `keys`, in order; None when any key has no button."""
    buttons = [n for n in snap.nodes if n.role == "AXButton" and n.enabled and not n.in_menu_bar]
    presses: list[UINode] = []
    for k in keys:
        b = _find_key(k, buttons)
        if b is None:
            return None
        presses.append(b)
    return presses


def _find_key(k: str, buttons: Sequence[UINode]) -> UINode | None:
    """The button for key `k`: an identifier match anywhere wins over a label match."""
    ids = _BY_ID.get(k, ())
    for b in buttons:
        if b.identifier and b.identifier in ids:
            return b
    labels = _BY_LABEL.get(k, (k.lower(),))
    for b in buttons:
        if (b.raw_label or "").lower() in labels:
            return b
    return None


def keypad_parents(snap: Snapshot) -> set[int]:
    """Parents (node indices) that hold an on-screen keypad.

    That is at least eight of the digit keys 0-9 as buttons side by side. Keys there only edit the
    entry on the keypad's display.
    """
    digits: dict[int, set[str]] = {}
    for n in snap.nodes:
        if n.role != "AXButton" or n.parent is None:
            continue
        name = n.raw_label if n.raw_label is not None else n.label
        d: str | None = name if _ONE_DIGIT.fullmatch(name) else None
        if d is None and n.identifier:
            d = next((k for k, ids in _DIGIT_KEYS if n.identifier in ids), None)
        if d is not None:
            digits.setdefault(n.parent, set()).add(d)
    return {p for p, ds in digits.items() if len(ds) >= 8}
