"""Text normalization shared by every comparison, and the word tokens used for lexical scoring."""

from __future__ import annotations

import re
from typing import Final

from beans_picker._numbers import scalar_text
from beans_picker._text import NOT_LETTER_OR_NUMBER, WS, trim, utf16_len, utf16_slice

# Invisible formatting characters never take part in a comparison.
_INVISIBLE: Final = re.compile("[\\u200b-\\u200f\\u2066-\\u2069\\u202a-\\u202e\\ufeff]")
_WS_RUN: Final = re.compile(f"{WS}+")
_CAMEL_HUMP: Final = re.compile("([a-z])([A-Z])")
_SEPARATORS: Final = re.compile("[_-]+")
_NOT_WORD_RUN: Final = re.compile(f"{NOT_LETTER_OR_NUMBER}+")
_STOP_WORDS: Final = frozenset(
    {
        "the", "a", "an", "to", "of", "in", "on", "and", "then", "into",
        "it", "is", "for", "with", "at", "by", "from", "as", "this", "that",
    }
)  # fmt: skip
_ELLIPSIS: Final = "\u2026"


def normalize_text(s: str | float | None) -> str:
    """Text as compared: invisible characters removed, whitespace collapsed and trimmed."""
    if s is None:
        return ""
    text = s if isinstance(s, str) else scalar_text(s)
    text = _INVISIBLE.sub("", text).replace("\u00a0", " ")
    return trim(_WS_RUN.sub(" ", text))


def humanize_identifier(ident: str | None) -> str:
    """A readable name from an accessibility identifier: `AllClear` -> `All Clear`, `save-button` -> `save button`."""
    if not ident:
        return ""
    if ident.startswith(("_", "NS", "menuAction")) or ":" in ident or "(" in ident:
        return ""
    return trim(_SEPARATORS.sub(" ", _CAMEL_HUMP.sub(r"\1 \2", ident)))


def tokenize(s: str) -> list[str]:
    """Lowercased word tokens without stop words, in order and with repeats."""
    out: list[str] = []
    for word in _NOT_WORD_RUN.split(normalize_text(s).lower()):
        w = word[:-1] if utf16_len(word) > 3 and word.endswith("s") else word
        if w and w not in _STOP_WORDS:
            out.append(w)
    return out


def truncate(s: str, limit: int) -> str:
    """`s` cut to `limit` UTF-16 units, the last one replaced by an ellipsis when it is cut."""
    if utf16_len(s) <= limit:
        return s
    return utf16_slice(s, limit - 1) + _ELLIPSIS
