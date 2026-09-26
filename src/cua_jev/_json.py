"""Compact JSON output and JSON string literals, written by hand so every byte is fixed.

Numbers are written with `_numbers.number_text` (`1`, not `1.0`); NaN and the infinities become
`null`. Keys keep insertion order. Strings escape `"`, `\\`, `\\b`, `\\f`, `\\n`, `\\r`, `\\t`, other
control characters below U+0020 as `\\u00xx`, and lone surrogates as `\\udxxx` (lowercase hex);
everything else is written as it is, so the output always encodes to UTF-8.
"""

from __future__ import annotations

import math
from typing import Final

from cua_jev._numbers import number_text

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

_SHORT_ESCAPES: Final[dict[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _needs_work(s: str) -> bool:
    for ch in s:
        c = ord(ch)
        if c < 0x20 or ch in ('"', "\\") or 0xD800 <= c <= 0xDFFF:
            return True
    return False


def quote(s: str) -> str:
    """`s` as a JSON string literal, quotes included."""
    if not _needs_work(s):
        return f'"{s}"'
    out: list[str] = ['"']
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        c = ord(ch)
        short = _SHORT_ESCAPES.get(ch)
        if short is not None:
            out.append(short)
        elif c < 0x20:
            out.append(f"\\u{c:04x}")
        elif 0xD800 <= c <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
            # A pair held as two code points is one character.
            out.append(chr(0x10000 + ((c - 0xD800) << 10) + (ord(s[i + 1]) - 0xDC00)))
            i += 1
        elif 0xD800 <= c <= 0xDFFF:
            out.append(f"\\u{c:04x}")
        else:
            out.append(ch)
        i += 1
    out.append('"')
    return "".join(out)


def dumps(obj: object) -> str:
    """`obj` as compact JSON (no spaces). Accepts dict (str keys), list, tuple, str, int, float,
    bool and None; anything else raises TypeError."""
    parts: list[str] = []
    _write(obj, parts)
    return "".join(parts)


def _write(obj: object, out: list[str]) -> None:
    if obj is None:
        out.append("null")
    elif isinstance(obj, bool):
        out.append("true" if obj else "false")
    elif isinstance(obj, int):
        text = number_text(obj)
        # An integer too large for a float has no finite JSON number.
        out.append("null" if text.endswith("Infinity") else text)
    elif isinstance(obj, float):
        out.append(number_text(obj) if math.isfinite(obj) else "null")
    elif isinstance(obj, str):
        out.append(quote(obj))
    elif isinstance(obj, dict):
        out.append("{")
        first = True
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be str, not {type(key).__name__}")
            if not first:
                out.append(",")
            first = False
            out.append(quote(key))
            out.append(":")
            _write(value, out)
        out.append("}")
    elif isinstance(obj, list | tuple):
        out.append("[")
        for i, item in enumerate(obj):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
