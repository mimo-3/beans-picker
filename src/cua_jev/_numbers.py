"""Number rounding, formatting and parsing with the exact rules the tool outputs use.

Probabilities and pixel coordinates round half up (x.5 -> x+1, -x.5 -> -x). Numbers are written the
way they are written in JSON output (`1`, `0.5`, `1e-7`): shortest round-trip digits, no `.0` on
integral values, and exponent form only for very large or very small magnitudes.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from cua_jev._text import trim


def round_half_up(x: float) -> int:
    """The integer nearest to `x`; a tie goes up (2.5 -> 3, -2.5 -> -2).

    `x - floor(x)` is computed exactly, unlike `floor(x + 0.5)`, which turns
    0.49999999999999994 into 1.
    """
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def round3(p: float) -> float:
    """`p` rounded half up to three decimals."""
    return round_half_up(p * 1000) / 1000


def fixed(p: float, digits: int) -> str:
    """`p` with exactly `digits` decimals, rounded half away from zero on its exact binary value.

    `fixed(0.125, 2) == "0.13"` (0.125 is an exact tie) but `fixed(0.615, 2) == "0.61"`
    (0.615 is stored slightly below the tie). Magnitudes of 1e21 or more are written as by
    `number_text`, and so are NaN and the infinities. Negative zero is written without a sign.
    """
    if not math.isfinite(p) or abs(p) >= 1e21:
        return number_text(p)
    if p == 0:
        p = 0.0
    q = Decimal(p).quantize(Decimal(10) ** -digits, rounding=ROUND_HALF_UP)
    return f"{q:f}"


_EXP_LIMIT_HIGH: Final = 21
_EXP_LIMIT_LOW: Final = -6


def number_text(x: float) -> str:
    """`x` written as in JSON output.

    Uses the shortest digits that read back as the same float. With the decimal exponent `n` (the
    position of the decimal point relative to the first digit): `n` up to 21 is written in plain
    form (`1e20 -> "100000000000000000000"`), `n` down to -5 too (`1e-6 -> "0.000001"`); beyond
    that the exponent form is used (`"1e+21"`, `"1e-7"`, `"1.5e-7"`). Negative zero is `"0"`,
    NaN is `"NaN"`, the infinities are `"Infinity"` and `"-Infinity"`.
    """
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, int):
        try:
            x = float(x)
        except OverflowError:
            x = math.inf if x > 0 else -math.inf
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    digits, n = _shortest_digits(abs(x))
    k = len(digits)
    if k <= n <= _EXP_LIMIT_HIGH:
        body = digits + "0" * (n - k)
    elif 0 < n <= _EXP_LIMIT_HIGH:
        body = digits[:n] + "." + digits[n:]
    elif _EXP_LIMIT_LOW < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = f"e+{e}" if e >= 0 else f"e-{-e}"
        body = (digits[0] + "." + digits[1:] if k > 1 else digits) + exp
    return sign + body


def _shortest_digits(x: float) -> tuple[str, int]:
    """For a positive finite `x`: its shortest significant digits and the exponent `n` such that
    `x == 0.<digits> * 10**n`."""
    text = repr(x)
    mantissa, _, exp_text = text.partition("e")
    exp = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    point = len(int_part) + exp
    stripped = all_digits.lstrip("0")
    point -= len(all_digits) - len(stripped)
    stripped = stripped.rstrip("0")
    return stripped, point


_DECIMAL_RE: Final = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_INFINITY_RE: Final = re.compile(r"[+-]?Infinity")
_PREFIXED_RE: Final = re.compile(r"0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")


def parse_number(text: str) -> float:
    """The number a text denotes, or NaN.

    Surrounding whitespace is ignored and an empty text is 0. Accepted: decimal numbers with an
    optional sign, fraction and exponent (`" 7 "`, `"7.0"`, `".5"`, `"1e1"`), `Infinity` with an
    optional sign, and unsigned `0x`/`0o`/`0b` integers. Anything else is NaN.
    """
    s = trim(text)
    if s == "":
        return 0.0
    if _DECIMAL_RE.fullmatch(s):
        return float(s)
    if _INFINITY_RE.fullmatch(s):
        return -math.inf if s[0] == "-" else math.inf
    if _PREFIXED_RE.fullmatch(s):
        base = {"x": 16, "o": 8, "b": 2}[s[1].lower()]
        try:
            return float(int(s[2:], base))
        except OverflowError:
            return math.inf
    return math.nan


def scalar_text(v: object) -> str:
    """A received value written as text: numbers via `number_text`, `true`/`false`, `null`,
    strings as they are, lists as their items joined by `,` (absent items empty), and any other
    object as `[object Object]`."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return number_text(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list | tuple):
        return ",".join("" if item is None else scalar_text(item) for item in v)
    return "[object Object]"
