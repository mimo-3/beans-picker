from __future__ import annotations

import asyncio
import os

from cua_jev._stdin import StdinLines


async def test_reads_lines_then_the_end_of_input() -> None:
    read_end, write_end = os.pipe()
    try:
        lines = StdinLines(read_end)
        os.write(write_end, b'{"a": 1}\n{"b": "\xc3\xa9"}\npartial')
        os.close(write_end)
        assert [line async for line in lines] == ['{"a": 1}\n', '{"b": "é"}\n', "partial"]
        assert await lines.readline() == ""
    finally:
        os.close(read_end)


async def test_a_blocked_read_is_cancelled_at_once() -> None:
    read_end, write_end = os.pipe()
    try:
        lines = StdinLines(read_end)
        reading = asyncio.ensure_future(lines.readline())
        await asyncio.sleep(0.05)
        reading.cancel()
        await asyncio.wait_for(asyncio.wait({reading}), 1)
        assert reading.cancelled()
    finally:
        os.close(write_end)
        os.close(read_end)
