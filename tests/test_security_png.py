from __future__ import annotations

import zlib

import pytest

from beans_picker.observe.png import decode_png

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4) + kind + body + zlib.crc32(kind + body).to_bytes(4)


def _png(*, width: int = 1, height: int = 1, raw: bytes = bytes(4)) -> bytes:
    header = width.to_bytes(4) + height.to_bytes(4) + bytes([8, 2, 0, 0, 0])
    return SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


@pytest.mark.parametrize(("width", "height"), [(2**30, 1), (1, 2**30), (8193, 8192)])
def test_oversized_dimensions_are_refused_before_decompression(
    width: int, height: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_inflate() -> None:
        raise AssertionError("oversized image reached decompression")

    monkeypatch.setattr(zlib, "decompressobj", unexpected_inflate)
    assert decode_png(_png(width=width, height=height)) is None


def test_inflated_bytes_beyond_the_declared_rows_are_refused() -> None:
    assert decode_png(_png(raw=bytes(1024 * 1024))) is None


@pytest.mark.parametrize("missing", [1, 4, 12])
def test_truncated_or_missing_iend_is_refused(missing: int) -> None:
    assert decode_png(_png()[:-missing]) is None


def test_an_idat_chunk_claiming_more_bytes_than_available_is_refused() -> None:
    png = _png()
    malformed = png[:33] + (2**30).to_bytes(4) + png[37:]
    assert decode_png(malformed) is None


def test_a_header_with_an_incorrect_declared_length_is_refused() -> None:
    png = _png()
    malformed = png[:8] + (12).to_bytes(4) + png[12:]
    assert decode_png(malformed) is None
