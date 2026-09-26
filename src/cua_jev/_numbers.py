"""Number rounding, formatting and parsing with the exact rules the tool outputs use."""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from cua_jev._text import trim


def round_half_up(x: float) -> int:
    """The integer nearest to `x`; a tie goes up (2.5 -> 3, -2.5 -> -2)."""
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def round3(p: float) -> float:
    return round_half_up(p * 1000) / 1000


def fixed(p: float, digits: int) -> str:
    """`p` with exactly `digits` decimals, rounded half away from zero on its exact binary value."""
    if not math.isfinite(p) or abs(p) >= 1e21:
        return number_text(p)
    if p == 0:
        p = 0.0
    q = Decimal(p).quantize(Decimal(10) ** -digits, rounding=ROUND_HALF_UP)
    return f"{q:f}"


_EXP_LIMIT_HIGH: Final = 21
_EXP_LIMIT_LOW: Final = -6


def number_text(x: float) -> str:
    """`x` written as in JSON output."""
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
    """The number a text denotes, or NaN."""
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
    """A received value as text: `null`, `true`/`false`, a number in its shortest form, a list joined by commas."""
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
