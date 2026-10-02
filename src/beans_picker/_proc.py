"""The one place subprocesses are started."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
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
            env=_child_env(),
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


STOP_GRACE_S: Final = 35.0
"""How long a child asked to stop may take to finish its work before it is killed."""


async def run_until(argv: Sequence[str], stop: asyncio.Event, /, *, grace: float = STOP_GRACE_S) -> int:
    """Run `argv` with its stderr passed through; once `stop` is set, send SIGINT and wait for it to finish.

    A child still running `grace` seconds after the SIGINT is killed. Returns the exit code (negative
    when a signal ended it).
    """
    if not argv:
        raise ProcessError("empty command")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            env=_child_env(),
        )
    except OSError as err:
        raise ProcessError(f"{argv[0]}: could not start: {err}") from err
    exited = asyncio.ensure_future(proc.wait())
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({exited, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if not exited.done():
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
            await asyncio.wait({exited}, timeout=grace)
            if not exited.done():
                await _kill(proc)
        return await exited
    except BaseException:
        exited.cancel()
        await _kill(proc)
        raise
    finally:
        stopped.cancel()


def _child_env() -> dict[str, str]:
    # Native helpers and system utilities do not need the remote Jev credentials.
    return {k: v for k, v in os.environ.items() if k not in {"JEV_API_KEY", "TYPESAFE_API_KEY"}}


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
