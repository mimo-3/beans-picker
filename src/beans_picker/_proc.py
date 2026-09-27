"""The one place subprocesses are started."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from beans_picker.errors import ProcessError

DEFAULT_MAX_BYTES: Final = 1 << 20
_CHUNK: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class Completed:
    stdout: str
    returncode: int


class Runner(Protocol):
    async def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        check: bool = True,
        cwd: Path | None = None,
    ) -> Completed: ...


async def run(
    argv: Sequence[str],
    /,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    check: bool = True,
    cwd: Path | None = None,
) -> Completed:
    """Run `argv` with stdin and stderr on the null device; return stdout and the exit code."""
    if not argv:
        raise ProcessError("empty command")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            # Native helpers and system utilities do not need the remote Jev credentials.
            env={k: v for k, v in os.environ.items() if k not in {"JEV_API_KEY", "TYPESAFE_API_KEY"}},
        )
    except OSError as err:
        raise ProcessError(f"{argv[0]}: could not start: {err}") from err
    try:
        stdout = await _read_limited(proc, max_bytes)
        if stdout is not None:
            returncode = await proc.wait()
    except BaseException:
        await _kill(proc)
        raise
    if stdout is None:
        await _kill(proc)
        raise ProcessError(f"{argv[0]}: output exceeded {max_bytes} bytes")
    text = stdout.decode("utf-8", errors="replace")
    if check and returncode != 0:
        raise ProcessError(f"{argv[0]} exited with code {returncode}", returncode=returncode, stdout=text)
    return Completed(stdout=text, returncode=returncode)


async def _read_limited(proc: asyncio.subprocess.Process, max_bytes: int) -> bytes | None:
    stream = proc.stdout
    if stream is None:  # pragma: no cover - stdout is always a pipe here
        return b""
    buf = bytearray()
    while True:
        chunk = await stream.read(_CHUNK)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            return None


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    # Reap the child even when the caller is being cancelled.
    await asyncio.shield(proc.wait())
