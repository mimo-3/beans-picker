"""Shared test helpers: captured `get_window_state` results and their snapshots, and small images
with a PNG encoder for the decoder and the pixel judges."""

from __future__ import annotations

import json
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from cua_jev._json import JsonObject
from cua_jev.observe.png import Rgba
from cua_jev.observe.snapshot import build_snapshot
from cua_jev.observe.types import Snapshot

FIXTURES = Path(__file__).parent / "fixtures"

type FixtureName = Literal["calculator", "textedit"]


def raw_fixture(name: FixtureName) -> JsonObject:
    """A cleaned-up `get_window_state` capture, plus its `pid` and `window_id`."""
    data: JsonObject = json.loads((FIXTURES / f"{name}.window-state.json").read_text(encoding="utf-8"))
    return data


def fixture_ids(raw: JsonObject) -> tuple[int, int]:
    """The capture's `pid` and `window_id`."""
    pid, window_id = raw["pid"], raw["window_id"]
    assert isinstance(pid, int)
    assert isinstance(window_id, int)
    return pid, window_id


def snap_fixture(name: FixtureName) -> Snapshot:
    """The snapshot built from a capture."""
    raw = raw_fixture(name)
    return build_snapshot(raw, *fixture_ids(raw))


def blank(w: int, h: int, v: int = 255) -> Rgba:
    """A `w` x `h` image of gray level `v`, opaque."""
    return Rgba(w, h, bytes([v]) * (w * h * 4))


def paint(img: Rgba, x0: int, y0: int, x1: int, y1: int, v: int) -> Rgba:
    """`img` with the box `[x0, x1) x [y0, y1)` painted gray level `v` (alpha kept)."""
    data = bytearray(img.data)
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * img.width + x) * 4
            data[i : i + 3] = bytes([v, v, v])
    return Rgba(img.width, img.height, bytes(data))


_PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))
_CHANNELS_OF = {0: 1, 2: 3, 4: 2, 6: 4}


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4) + kind + body + zlib.crc32(kind + body).to_bytes(4)


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def encode_png(img: Rgba, *, color_type: int = 2, filters: Sequence[int] = (0, 2, 1)) -> bytes:
    """A PNG of `img` (8-bit, one IDAT). Row `y` uses filter `filters[y % len(filters)]`.

    Gray types take the red channel as the gray level.
    """
    channels = _CHANNELS_OF[color_type]
    stride = img.width * channels
    raw = bytearray()
    prev = bytes(stride)
    for y in range(img.height):
        row = bytearray()
        for x in range(img.width):
            r, g, b, a = img.data[(y * img.width + x) * 4 : (y * img.width + x) * 4 + 4]
            row += {0: bytes([r]), 2: bytes([r, g, b]), 4: bytes([r, a]), 6: bytes([r, g, b, a])}[color_type]
        kind = filters[y % len(filters)]
        raw.append(kind)
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            predicted = (0, left, up, (left + up) >> 1, _paeth_predictor(left, up, up_left))[kind]
            raw.append((row[i] - predicted) & 0xFF)
        prev = bytes(row)
    ihdr = img.width.to_bytes(4) + img.height.to_bytes(4) + bytes([8, color_type, 0, 0, 0])
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )
