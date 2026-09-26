from __future__ import annotations

import zlib

import pytest

from cua_jev.observe.png import Rgba, crop, decode_png
from tests.helpers import blank, encode_png, paint


def _gradient(w: int, h: int, *, alpha: bool = False) -> Rgba:
    """Pixels that differ in every channel, so every filter predicts something."""
    data = bytearray()
    for y in range(h):
        for x in range(w):
            data += bytes([(x * 37 + y * 11) & 0xFF, (x * 5 + y * 91) & 0xFF, (x * y * 13 + 7) & 0xFF])
            data.append((x * 29 + y * 3) & 0xFF if alpha else 255)
    return Rgba(w, h, bytes(data))


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_every_filter_round_trips_rgb(kind: int) -> None:
    img = _gradient(7, 5)
    assert decode_png(encode_png(img, filters=[kind])) == img


def test_mixed_filters_round_trip_rgba() -> None:
    img = _gradient(6, 10, alpha=True)
    assert decode_png(encode_png(img, color_type=6, filters=[4, 3, 2, 1, 0])) == img


def test_gray_and_gray_alpha_expand_to_rgba() -> None:
    img = _gradient(5, 4, alpha=True)
    gray = decode_png(encode_png(img, color_type=0, filters=[1, 4]))
    assert gray is not None
    for i in range(20):
        r = img.data[i * 4]
        assert gray.data[i * 4 : i * 4 + 4] == bytes([r, r, r, 255])
    gray_alpha = decode_png(encode_png(img, color_type=4, filters=[3, 2]))
    assert gray_alpha is not None
    for i in range(20):
        r, a = img.data[i * 4], img.data[i * 4 + 3]
        assert gray_alpha.data[i * 4 : i * 4 + 4] == bytes([r, r, r, a])


def _with_ihdr(png: bytes, *, depth: int = 8, color_type: int = 2, interlace: int = 0) -> bytes:
    body = bytearray(png[16:29])
    body[8], body[9], body[12] = depth, color_type, interlace
    chunk = (13).to_bytes(4) + b"IHDR" + bytes(body) + zlib.crc32(b"IHDR" + bytes(body)).to_bytes(4)
    return png[:8] + chunk + png[33:]


def test_unsupported_formats_are_refused() -> None:
    png = encode_png(blank(3, 3))
    assert decode_png(_with_ihdr(png, depth=16)) is None
    assert decode_png(_with_ihdr(png, color_type=3)) is None
    assert decode_png(_with_ihdr(png, interlace=1)) is None
    assert decode_png(png[:8]) is None
    assert decode_png(b"") is None


def test_damaged_data_is_refused() -> None:
    png = encode_png(blank(3, 3))
    assert decode_png(png[:40]) is None  # the image data ends early
    assert decode_png(png[:20]) is None  # the header ends early
    idat_start = 33
    length = int.from_bytes(png[idat_start : idat_start + 4])
    garbage = png[: idat_start + 8] + b"\x00" * length + png[idat_start + 8 + length :]
    assert decode_png(garbage) is None


def test_an_unknown_filter_is_refused() -> None:
    raw = b"\x05" + bytes(3)
    body = (1).to_bytes(4) + (1).to_bytes(4) + bytes([8, 2, 0, 0, 0])
    png = (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + (13).to_bytes(4)
        + b"IHDR"
        + body
        + bytes(4)
        + len(zlib.compress(raw)).to_bytes(4)
        + b"IDAT"
        + zlib.compress(raw)
        + bytes(4)
    )
    assert decode_png(png) is None


def test_short_image_data_is_refused() -> None:
    raw = b"\x00" + bytes(3)  # one row of a two-row image
    body = (1).to_bytes(4) + (2).to_bytes(4) + bytes([8, 2, 0, 0, 0])
    idat = zlib.compress(raw)
    png = bytes((137, 80, 78, 71, 13, 10, 26, 10)) + (13).to_bytes(4) + b"IHDR" + body + bytes(4)
    png += len(idat).to_bytes(4) + b"IDAT" + idat + bytes(4)
    assert decode_png(png) is None


def test_bytes_after_the_zlib_stream_and_split_idat_chunks_are_read() -> None:
    img = paint(blank(4, 3), 1, 1, 3, 2, 9)
    png = encode_png(img)
    idat_start = 33
    length = int.from_bytes(png[idat_start : idat_start + 4])
    stream = png[idat_start + 8 : idat_start + 8 + length]
    head, tail = stream[:5], stream[5:] + b"trailing"
    split = (
        png[:idat_start]
        + len(head).to_bytes(4)
        + b"IDAT"
        + head
        + bytes(4)
        + b"\x00\x00\x00\x00tEXt\x00\x00\x00\x00"
        + len(tail).to_bytes(4)
        + b"IDAT"
        + tail
        + bytes(4)
        + png[idat_start + 12 + length :]
    )
    assert decode_png(split) == img


def test_chunks_after_iend_are_not_read() -> None:
    img = blank(2, 2)
    png = encode_png(img)
    assert decode_png(png + b"\x00\x00\x00\x03IDATxyz\x00\x00\x00\x00") == img


def test_crop_clamps_and_may_be_empty() -> None:
    img = paint(blank(4, 4), 0, 0, 1, 1, 0)
    whole = crop(img, -5, -5, 10, 10)
    assert (whole.width, whole.height) == (4, 4)
    assert whole.data == img.data
    assert crop(img, 10, 10, 5, 5) == Rgba(0, 0, b"")
    # Edges round half up: 0.5 -> 1, 2.5 -> 3.
    part = crop(img, 0.5, 0.5, 2, 2)
    assert (part.width, part.height) == (2, 2)
    assert crop(img, -0.5, -0.5, 1, 1).width == 1
