from __future__ import annotations

import math

import pytest

from beans_picker._numbers import fixed, number_text, parse_number, round3, round_half_up, scalar_text


@pytest.mark.parametrize(
    ("x", "want"),
    [
        (2.5, 3),
        (-2.5, -2),
        (0.49999999999999994, 0),
        (-0.5, 0),
        (0.5, 1),
        (1.4, 1),
        (-1.6, -2),
        (7.0, 7),
        (2.0**53, 2**53),
    ],
)
def test_round_half_up(x: float, want: int) -> None:
    assert round_half_up(x) == want


def test_round3() -> None:
    assert round3(0.1234) == 0.123
    assert round3(0.5) == 0.5
    assert round3(0.9995) == 1.0
    assert round3(0.0) == 0.0


@pytest.mark.parametrize(
    ("p", "digits", "want"),
    [
        (0.125, 2, "0.13"),
        (0.625, 2, "0.63"),
        (0.615, 2, "0.61"),
        (1.0, 2, "1.00"),
        (0.0, 2, "0.00"),
        (-0.0, 2, "0.00"),
        (-0.125, 2, "-0.13"),
        (-0.001, 2, "-0.00"),
        (2.5, 0, "3"),
        (123.456, 1, "123.5"),
        (1e21, 2, "1e+21"),
        (math.nan, 2, "NaN"),
    ],
)
def test_fixed(p: float, digits: int, want: str) -> None:
    assert fixed(p, digits) == want


@pytest.mark.parametrize(
    ("x", "want"),
    [
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1.5e-7, "1.5e-7"),
        (1e16, "10000000000000000"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.2345e25, "1.2345e+25"),
        (-0.0, "0"),
        (0.0, "0"),
        (1.0, "1"),
        (-1.0, "-1"),
        (0.5, "0.5"),
        (0.1 + 0.2, "0.30000000000000004"),
        (123.456, "123.456"),
        (-0.000123, "-0.000123"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (math.nan, "NaN"),
        (math.inf, "Infinity"),
        (-math.inf, "-Infinity"),
        (5, "5"),
        (-12, "-12"),
        (10**21, "1e+21"),
        (2**53 + 1, "9007199254740992"),
        (10**400, "Infinity"),
        (True, "1"),
    ],
)
def test_number_text(x: float, want: str) -> None:
    assert number_text(x) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("", 0.0),
        ("   ", 0.0),
        (" 7 ", 7.0),
        ("7.0", 7.0),
        ("7.", 7.0),
        (".5", 0.5),
        ("1e1", 10.0),
        ("-3", -3.0),
        ("+4", 4.0),
        ("0x10", 16.0),
        ("0X1f", 31.0),
        ("0o17", 15.0),
        ("0b11", 3.0),
        ("Infinity", math.inf),
        ("-Infinity", -math.inf),
        ("\u00a012\ufeff", 12.0),
        ("1e400", math.inf),
    ],
)
def test_parse_number(text: str, want: float) -> None:
    assert parse_number(text) == want


@pytest.mark.parametrize(
    "text", ["abc", "1_000", "inf", "nan", "infinity", "-0x10", "0x", "1e", ".", "1 2", "0b2", "\u0663"]
)
def test_parse_number_rejects(text: str) -> None:
    assert math.isnan(parse_number(text))


def test_parse_number_huge_prefixed_integer_is_infinite() -> None:
    assert parse_number("0x" + "f" * 300) == math.inf


@pytest.mark.parametrize(
    ("v", "want"),
    [
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (2.0, "2"),
        (0.25, "0.25"),
        ("x", "x"),
        ("", ""),
        ([1, None, "a", [2, 3]], "1,,a,2,3"),
        ({"a": 1}, "[object Object]"),
    ],
)
def test_scalar_text(v: object, want: str) -> None:
    assert scalar_text(v) == want
