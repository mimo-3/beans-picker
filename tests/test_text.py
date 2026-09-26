from __future__ import annotations

import hashlib
import re

import pytest

from cua_jev._text import (
    DIGIT,
    LETTER_OR_NUMBER,
    NOT_LETTER_OR_NUMBER,
    NOT_LINE_END,
    NOT_WS,
    WORD,
    WS,
    WS_CHARS,
    WS_CODE_POINTS,
    hash_bytes,
    is_ws,
    lstrip_ws,
    rstrip_ws,
    trim,
    utf16_len,
    utf16_slice,
    utf16_units,
)

GRIN = "\U0001f600"  # outside the BMP: two UTF-16 units


def test_whitespace_set_is_exactly_the_listed_code_points() -> None:
    expected = [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B)]
    expected += [0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF]
    assert list(WS_CODE_POINTS) == expected
    assert frozenset(chr(c) for c in expected) == WS_CHARS


def test_whitespace_class_matches_the_set_and_nothing_else() -> None:
    ws = re.compile(WS)
    not_ws = re.compile(NOT_WS)
    for c in range(0x10000):
        ch = chr(c)
        if 0xD800 <= c <= 0xDFFF:
            continue
        assert (ws.fullmatch(ch) is not None) == (ch in WS_CHARS), hex(c)
        assert (not_ws.fullmatch(ch) is not None) == (ch not in WS_CHARS), hex(c)


@pytest.mark.parametrize("ch", ["\x85", "\u200e", "\u200b", "\u180e", "x"])
def test_characters_outside_the_set_are_not_whitespace(ch: str) -> None:
    assert not is_ws(ch)
    assert trim(f"{ch}a{ch}") == f"{ch}a{ch}"


def test_trim_strips_only_the_set() -> None:
    assert trim("\ufeff\u3000 a b\u2029\t") == "a b"
    assert lstrip_ws("\u00a0 a ") == "a "
    assert rstrip_ws(" a \u202f") == " a"
    assert trim("") == ""


def test_ascii_classes() -> None:
    assert re.fullmatch(WORD, "_")
    assert not re.fullmatch(WORD, "\u00e9")
    assert not re.fullmatch(DIGIT, "\u0663")  # ARABIC-INDIC DIGIT THREE
    assert re.fullmatch(LETTER_OR_NUMBER, "\u00e9")
    assert re.fullmatch(LETTER_OR_NUMBER, "\u0663")
    assert not re.fullmatch(LETTER_OR_NUMBER, "_")


@pytest.mark.parametrize("ch", ["a", "\u00e9", "7", "\u0663", "\u00b2", "_", " ", "-", "\u3002"])
def test_not_letter_or_number_is_the_complement(ch: str) -> None:
    assert bool(re.fullmatch(NOT_LETTER_OR_NUMBER, ch)) is not bool(re.fullmatch(LETTER_OR_NUMBER, ch))


@pytest.mark.parametrize("ch", ["\n", "\r", "\u2028", "\u2029"])
def test_not_line_end_excludes_line_terminators(ch: str) -> None:
    assert not re.fullmatch(NOT_LINE_END, ch)


@pytest.mark.parametrize("ch", ["a", "\x85", "\x0b", "\x0c"])
def test_not_line_end_matches_other_characters(ch: str) -> None:
    assert re.fullmatch(NOT_LINE_END, ch)


def test_utf16_len_counts_astral_characters_twice() -> None:
    assert utf16_len("") == 0
    assert utf16_len("abc") == 3
    assert utf16_len(f"A{GRIN}B") == 4
    assert utf16_len("\ud800") == 1
    assert utf16_units(f"A{GRIN}") == "A".encode("utf-16-le") + GRIN.encode("utf-16-le")


def test_utf16_slice_cuts_inside_a_pair_to_a_lone_high_surrogate() -> None:
    s = f"A{GRIN}B"
    assert utf16_slice(s, 1) == "A"
    assert utf16_slice(s, 2) == "A\ud83d"
    assert utf16_slice(s, 3) == f"A{GRIN}"
    assert utf16_slice(s, 99) == s
    assert utf16_slice(s, 0) == ""


def test_utf16_slice_start_and_negative_indices() -> None:
    s = f"A{GRIN}B"
    assert utf16_slice(s, 4, start=1) == f"{GRIN}B"
    assert utf16_slice(s, 4, start=2) == "\ude00B"
    assert utf16_slice(s, -1) == f"A{GRIN}"
    assert utf16_slice(s, 1, start=3) == ""
    assert utf16_slice(s, 4, start=-99) == s


def test_hash_bytes_replaces_lone_surrogates() -> None:
    assert hash_bytes("\ud800") == "\ufffd".encode()
    assert hashlib.sha1(hash_bytes("\ud800")).hexdigest().startswith("9bdb7727")  # noqa: S324
    assert hash_bytes(f"a{GRIN}") == f"a{GRIN}".encode()
    assert hash_bytes("\ud83d\ude00") == GRIN.encode()
