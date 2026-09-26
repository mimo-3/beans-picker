"""A minimal PNG decoder for the window screenshots cua-driver writes, and cropping."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from itertools import accumulate
from typing import Final

from cua_jev._numbers import round_half_up

_SIGNATURE: Final = bytes((137, 80, 78, 71, 13, 10, 26, 10))
_CHANNELS: Final = {6: 4, 2: 3, 0: 1, 4: 2}

type _Row = bytes | bytearray


@dataclass(frozen=True, slots=True)
class Rgba:
    """`width * height * 4` bytes, row-major RGBA."""

    width: int
    height: int
    data: bytes


def decode_png(buf: bytes) -> Rgba | None:
    """The image as RGBA pixels, or `None` when it is not a PNG this decoder reads."""
    if buf[:8] != _SIGNATURE:
        return None
    width = height = depth = interlace = 0
    color_type = -1
    idat: list[bytes] = []
    pos = 8
    while pos + 8 <= len(buf):
        length = int.from_bytes(buf[pos : pos + 4])
        kind = buf[pos + 4 : pos + 8]
        if kind == b"IHDR":
            if pos + 21 > len(buf):
                return None
            width = int.from_bytes(buf[pos + 8 : pos + 12])
            height = int.from_bytes(buf[pos + 12 : pos + 16])
            depth, color_type = buf[pos + 16], buf[pos + 17]
            interlace = buf[pos + 20]
        elif kind == b"IDAT":
            idat.append(buf[pos + 8 : pos + 8 + length])
        elif kind == b"IEND":
            break
        pos += 12 + length
    channels = _CHANNELS.get(color_type, 0)
    if not width or not height or depth != 8 or not channels or interlace != 0:
        return None
    raw = _inflate(b"".join(idat))
    if raw is None:
        return None
    stride = width * channels
    if len(raw) < height * (stride + 1):
        return None
    px = _unfilter(raw, height, stride, channels)
    if px is None:
        return None
    return Rgba(width, height, _to_rgba(px, width * height, channels))


def _inflate(data: bytes) -> bytes | None:
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data)
    except zlib.error:
        return None
    return out if inflater.eof else None


def _unfilter(raw: bytes, height: int, stride: int, channels: int) -> bytearray | None:
    px = bytearray(height * stride)
    prev: _Row = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = raw[start + 1 : start + 1 + stride]
        row: _Row
        if kind == 0:
            row = line
        elif kind == 1:
            row = _sub(line, channels)
        elif kind == 2:
            row = _add(line, prev)
        elif kind == 3:
            row = _average(line, prev, channels)
        elif kind == 4:
            row = _paeth(line, prev, channels)
        else:
            return None
        px[y * stride : (y + 1) * stride] = row
        prev = row
    return px


def _add(line: bytes, prev: _Row) -> bytes:
    n = len(line)
    low = int.from_bytes(b"\x7f" * n)
    high = int.from_bytes(b"\x80" * n)
    a = int.from_bytes(line)
    b = int.from_bytes(prev)
    return (((a & low) + (b & low)) ^ ((a ^ b) & high)).to_bytes(n)


def _sub(line: bytes, channels: int) -> bytearray:
    out = bytearray(len(line))
    for c in range(channels):
        out[c::channels] = bytes(v & 0xFF for v in accumulate(line[c::channels]))
    return out


def _average(line: bytes, prev: _Row, channels: int) -> bytearray:
    out = bytearray(line)
    for i in range(len(out)):
        left = out[i - channels] if i >= channels else 0
        out[i] = (out[i] + ((left + prev[i]) >> 1)) & 0xFF
    return out


def _paeth(line: bytes, prev: _Row, channels: int) -> bytearray:
    out = bytearray(line)
    for i in range(len(out)):
        up = prev[i]
        if i >= channels:
            left = out[i - channels]
            up_left = prev[i - channels]
        else:
            left = up_left = 0
        p = left + up - up_left
        pa = abs(p - left)
        pb = abs(p - up)
        pc = abs(p - up_left)
        pred = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
        out[i] = (out[i] + pred) & 0xFF
    return out


def _to_rgba(px: bytearray, count: int, channels: int) -> bytes:
    if channels == 4:
        return bytes(px)
    data = bytearray(b"\xff" * (count * 4))
    if channels == 3:
        for c in range(3):
            data[c::4] = px[c::3]
    else:
        gray = px[::channels]
        for c in range(3):
            data[c::4] = gray
        if channels == 2:
            data[3::4] = px[1::2]
    return bytes(data)


def crop(img: Rgba, x: float, y: float, w: float, h: float) -> Rgba:
    """The pixels of `[x, x + w) x [y, y + h)`, edges rounded half up and clamped to the image."""
    x0 = max(0, min(img.width, round_half_up(x)))
    y0 = max(0, min(img.height, round_half_up(y)))
    x1 = max(x0, min(img.width, round_half_up(x + w)))
    y1 = max(y0, min(img.height, round_half_up(y + h)))
    rows = [img.data[((y0 + r) * img.width + x0) * 4 : ((y0 + r) * img.width + x1) * 4] for r in range(y1 - y0)]
    return Rgba(x1 - x0, y1 - y0, b"".join(rows))
