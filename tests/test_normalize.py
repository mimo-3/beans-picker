from __future__ import annotations

import pytest

from cua_jev.observe.normalize import humanize_identifier, normalize_text, tokenize, truncate


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, ""),
        (" a \u200bb  \n c ", "a b c"),
        ("x\ufeffy", "xy"),
        ("a\u00a0\u00a0b", "a b"),
        ("\u200e84", "84"),
        ("\u202aab\u202e", "ab"),
        ("a\u3000\u2028b\t", "a b"),
        ("a\x85b", "a\x85b"),
        ("\x1c", "\x1c"),
        (1, "1"),
        (1.0, "1"),
        (0.5, "0.5"),
        (True, "true"),
        ("", ""),
    ],
)
def test_normalize_text(given: str | float | None, expected: str) -> None:
    assert normalize_text(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("AllClear", "All Clear"),
        ("_NS:34", ""),
        ("NSWindow", ""),
        ("NSFoo", ""),
        ("menuActionFoo", ""),
        ("menuActionX", ""),
        ("foo(x)", ""),
        ("closeAll:", ""),
        ("save-button", "save button"),
        ("a_b-c", "a b c"),
        ("helloWorldAgain", "hello World Again"),
        ("aBC", "a BC"),
        ("First Text View", "First Text View"),
        ("__", ""),
        ("-x-", "x"),
        ("", ""),
        (None, ""),
    ],
)
def test_humanize_identifier(given: str | None, expected: str) -> None:
    assert humanize_identifier(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Open the Files", ["open", "file"]),
        ("is", []),
        ("bus", ["bus"]),
        ("thiss", []),
        ("日本語 テスト", ["日本語", "テスト"]),
        ("save_as-copy2", ["save", "copy2"]),
        ("Buttons, buttons!", ["button", "button"]),
        ("  ", []),
        ("\u00dcn\u00efc\u00f6des", ["\u00fcn\u00efc\u00f6de"]),
        ("x\u00b2", ["x\u00b2"]),
    ],
)
def test_tokenize(given: str, expected: list[str]) -> None:
    assert tokenize(given) == expected


def test_tokenize_counts_length_in_utf16_units() -> None:
    # One astral character counts twice: "\U0001d49c" + "s" is three units, so the "s" stays.
    assert tokenize("\U0001d49cs") == ["\U0001d49cs"]
    assert tokenize("\U0001d49cxs") == ["\U0001d49cx"]


@pytest.mark.parametrize(
    ("given", "limit", "expected"),
    [
        ("abcdef", 4, "abc\u2026"),
        ("abc", 3, "abc"),
        ("", 0, ""),
        ("A\U0001f600B", 3, "A\ud83d\u2026"),
        ("A\U0001f600B", 4, "A\U0001f600B"),
        ("A\U0001f600BC", 4, "A\U0001f600\u2026"),
    ],
)
def test_truncate(given: str, limit: int, expected: str) -> None:
    assert truncate(given, limit) == expected
