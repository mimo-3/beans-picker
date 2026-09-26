"""Text normalization shared by every comparison, and the word tokens used for lexical scoring."""

from __future__ import annotations

import re
from typing import Final

from cua_jev._numbers import scalar_text
from cua_jev._text import NOT_LETTER_OR_NUMBER, WS, trim, utf16_len, utf16_slice

# Zero-width and directional formatting characters: U+200B..U+200F, U+2066..U+2069,
# U+202A..U+202E and U+FEFF. They are invisible on screen, so they never take part in a comparison.
_INVISIBLE: Final = re.compile("[\\u200b-\\u200f\\u2066-\\u2069\\u202a-\\u202e\\ufeff]")
_WS_RUN: Final = re.compile(f"{WS}+")
_CAMEL_HUMP: Final = re.compile("([a-z])([A-Z])")
_SEPARATORS: Final = re.compile("[_-]+")
# A run of anything that is not a Unicode letter or number.
_NOT_WORD_RUN: Final = re.compile(f"{NOT_LETTER_OR_NUMBER}+")
_STOP_WORDS: Final = frozenset(
    {
        "the", "a", "an", "to", "of", "in", "on", "and", "then", "into",
        "it", "is", "for", "with", "at", "by", "from", "as", "this", "that",
    }
)  # fmt: skip
_ELLIPSIS: Final = "\u2026"


def normalize_text(s: str | float | None) -> str:
    """Text as compared: invisible characters removed, whitespace runs (no-break spaces included)
    collapsed to one space, and trimmed. `None` gives `""`; a number or bool is written as in JSON
    output (`1`, `0.5`, `true`)."""
    if s is None:
        return ""
    text = s if isinstance(s, str) else scalar_text(s)
    text = _INVISIBLE.sub("", text).replace("\u00a0", " ")
    return trim(_WS_RUN.sub(" ", text))


def humanize_identifier(ident: str | None) -> str:
    """A readable name from an accessibility identifier: `AllClear` -> `All Clear`,
    `save-button` -> `save button`. Machine-looking identifiers (`_NS:34`, `NSWindow`,
    `menuAction:`, `foo(x)`, anything with a colon) give `""`."""
    if not ident:
        return ""
    if ident.startswith(("_", "NS", "menuAction")) or ":" in ident or "(" in ident:
        return ""
    return trim(_SEPARATORS.sub(" ", _CAMEL_HUMP.sub(r"\1 \2", ident)))


def tokenize(s: str) -> list[str]:
    """Lowercased word tokens without stop words, in order and with repeats.

    Words split on anything that is not a letter or number, so a CJK run stays one token. A word
    longer than three characters loses a final `s` before the stop-word filter.
    """
    out: list[str] = []
    for word in _NOT_WORD_RUN.split(normalize_text(s).lower()):
        w = word[:-1] if utf16_len(word) > 3 and word.endswith("s") else word
        if w and w not in _STOP_WORDS:
            out.append(w)
    return out


def truncate(s: str, limit: int) -> str:
    """`s` cut to `limit` UTF-16 units, the last one replaced by an ellipsis when it is cut.

    A cut inside a surrogate pair keeps the lone high surrogate.
    """
    if utf16_len(s) <= limit:
        return s
    return utf16_slice(s, limit - 1) + _ELLIPSIS
