"""Text primitives shared by every layer: UTF-16 lengths, the whitespace set and regex classes.

NSString and the accessibility API count text in UTF-16 units, so every length that is compared or
sliced here is a number of UTF-16 code units, not of code points.
"""

from __future__ import annotations

from typing import Final

# The whitespace set, by code point:
#   U+0009..U+000D (tab, LF, VT, FF, CR), U+0020 space, U+00A0 no-break space, U+1680 ogham space,
#   U+2000..U+200A (en quad .. hair space), U+2028 line separator, U+2029 paragraph separator,
#   U+202F narrow no-break space, U+205F medium mathematical space, U+3000 ideographic space,
#   U+FEFF zero width no-break space (byte order mark).
# Candidate keys and state signatures depend on this exact set; changing it changes every key.
# U+0085 and U+200E are deliberately not in it.
WS_CODE_POINTS: Final[tuple[int, ...]] = (
    0x0009,
    0x000A,
    0x000B,
    0x000C,
    0x000D,
    0x0020,
    0x00A0,
    0x1680,
    *range(0x2000, 0x200B),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
    0xFEFF,
)

WS_CHARS: Final[frozenset[str]] = frozenset(chr(c) for c in WS_CODE_POINTS)

_WS_STR: Final[str] = "".join(chr(c) for c in WS_CODE_POINTS)


def _escape_class(code_points: tuple[int, ...]) -> str:
    return "".join(f"\\u{c:04x}" for c in code_points)


# The members of the whitespace set, escaped for use inside a regex character class.
WS_CLASS_BODY: Final[str] = _escape_class(WS_CODE_POINTS)
# A regex character class matching one whitespace character of the set above.
WS: Final[str] = f"[{WS_CLASS_BODY}]"
# A regex character class matching one character that is not whitespace.
NOT_WS: Final[str] = f"[^{WS_CLASS_BODY}]"
# ASCII word characters; a word boundary in this package's patterns is between these and anything else.
WORD_CLASS_BODY: Final[str] = "A-Za-z0-9_"
WORD: Final[str] = f"[{WORD_CLASS_BODY}]"
NOT_WORD: Final[str] = f"[^{WORD_CLASS_BODY}]"
DIGIT: Final[str] = "[0-9]"
# Unicode letters and numbers. Never use it inside a pattern compiled with re.ASCII.
LETTER_OR_NUMBER: Final[str] = "[^\\W_]"
# Anything that is not a Unicode letter or number (the complement of LETTER_OR_NUMBER).
NOT_LETTER_OR_NUMBER: Final[str] = "[\\W_]"
# Any character except the line terminators LF, CR, U+2028 and U+2029.
NOT_LINE_END: Final[str] = "[^\\n\\r\\u2028\\u2029]"


def utf16_units(s: str) -> bytes:
    """The UTF-16LE code units of `s`; unit `i` is `bytes[2 * i : 2 * i + 2]`.

    Lone surrogates are kept as they are, so every string has an encoding.
    """
    return s.encode("utf-16-le", "surrogatepass")


def utf16_len(s: str) -> int:
    """The length of `s` in UTF-16 code units (an astral character counts 2)."""
    return len(utf16_units(s)) // 2


def _from_units(units: bytes) -> str:
    return units.decode("utf-16-le", "surrogatepass")


def utf16_slice(s: str, stop: int, *, start: int = 0) -> str:
    """`s` cut to the UTF-16 unit range [start, stop).

    Negative indices count from the end and out-of-range indices are clamped, as with list slicing.
    A cut inside a surrogate pair keeps the half that falls in range as a lone surrogate.
    """
    units = utf16_units(s)
    n = len(units) // 2
    lo = _clamp(start, n)
    hi = _clamp(stop, n)
    if hi <= lo:
        return ""
    return _from_units(units[2 * lo : 2 * hi])


def _clamp(i: int, n: int) -> int:
    if i < 0:
        return max(n + i, 0)
    return min(i, n)


def hash_bytes(s: str) -> bytes:
    """The bytes that are hashed for `s`: UTF-8, with every lone surrogate replaced by U+FFFD.

    Surrogate pairs (also when held as two separate code points) are joined into their character.
    """
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def trim(s: str) -> str:
    """`s` without leading and trailing whitespace of the set above (nothing else is stripped)."""
    return s.strip(_WS_STR)


def lstrip_ws(s: str) -> str:
    """`s` without leading whitespace of the set above."""
    return s.lstrip(_WS_STR)


def rstrip_ws(s: str) -> str:
    """`s` without trailing whitespace of the set above."""
    return s.rstrip(_WS_STR)


def is_ws(ch: str) -> bool:
    """Whether the single character `ch` is whitespace of the set above."""
    return ch in WS_CHARS
