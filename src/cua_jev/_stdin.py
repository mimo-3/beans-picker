"""Standard input for the MCP transport, read so that a signal can end serving at once."""

from __future__ import annotations

import asyncio
import fcntl
import os
import threading
from typing import Final

import anyio

_CHUNK: Final = 65536


class StdinLines(anyio.AsyncFile[str]):
    """Lines of a descriptor as UTF-8 text; an empty string at end of input, as from a file."""

    def __init__(self, fd: int = 0) -> None:
        own = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
        # The wrapped file is only there for the `AsyncFile` interface; lines come from the thread.
        super().__init__(os.fdopen(own, "r", encoding="utf-8", errors="replace", closefd=False))
        self._fd = own
        self._lines: asyncio.Queue[str] | None = None

    async def readline(self) -> str:
        if self._lines is None:
            self._lines = asyncio.Queue()
            pump = threading.Thread(
                target=_pump,
                args=(self._fd, asyncio.get_running_loop(), self._lines),
                name="cua-jev stdin",
                daemon=True,
            )
            pump.start()
        line = await self._lines.get()
        if not line:
            self._lines.put_nowait(line)
        return line


def _pump(fd: int, loop: asyncio.AbstractEventLoop, lines: asyncio.Queue[str]) -> None:
    pending = bytearray()
    try:
        while chunk := os.read(fd, _CHUNK):
            pending += chunk
            while (end := pending.find(b"\n")) >= 0:
                line = bytes(pending[: end + 1])
                del pending[: end + 1]
                if not _deliver(loop, lines, line.decode("utf-8", "replace")):
                    return
        if pending:
            _deliver(loop, lines, pending.decode("utf-8", "replace"))
    except OSError:
        pass  # the descriptor failed: treat it as the end of input
    finally:
        _deliver(loop, lines, "")
        os.close(fd)


def _deliver(loop: asyncio.AbstractEventLoop, lines: asyncio.Queue[str], line: str) -> bool:
    try:
        loop.call_soon_threadsafe(lines.put_nowait, line)
    except RuntimeError:
        return False
    return True
