"""`beans-picker record`: a video of one window, taken by the winrec helper (ScreenCaptureKit, macOS 15+).

The window is captured on its own, so an app that comes to the front, or a window covering it, does
not show in the video.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from beans_picker._proc import Runner, run, run_until
from beans_picker.driver.activate import ActivateBuild, Activator
from beans_picker.driver.app import AppTarget, ensure_app, resolve_bundle
from beans_picker.driver.mcp import Connect, CuaDriver, Driver
from beans_picker.driver.sentinel import front_pid
from beans_picker.errors import BeansPickerError

USAGE: Final = "usage: beans-picker record --app <name or bundle id> --out <file.mp4>"

type Until = Callable[[Sequence[str], asyncio.Event], Awaitable[int]]
type Say = Callable[[str], None]


class RecordBuild(ActivateBuild, Protocol):
    """Where the recorder comes from."""

    async def winrec_bin(self) -> Path | None: ...


@dataclass(frozen=True, slots=True)
class RecordArgs:
    app: str
    out: Path


def parse_args(argv: Sequence[str]) -> RecordArgs | None:
    """`--app <app> --out <file>` in either order; None for anything else."""
    if len(argv) != 4 or {argv[0], argv[2]} != {"--app", "--out"}:
        return None
    given = {argv[0]: argv[1], argv[2]: argv[3]}
    if not given["--app"] or not given["--out"]:
        return None
    return RecordArgs(app=given["--app"], out=Path(given["--out"]).expanduser().resolve())


async def _connect() -> Driver:
    return await CuaDriver.connect()


async def record(
    args: RecordArgs,
    stop: asyncio.Event,
    *,
    helpers: RecordBuild,
    say: Say,
    connect: Connect = _connect,
    runner: Runner = run,
    until: Until = run_until,
) -> int:
    """Record the app's window to `args.out` until `stop` is set; returns the exit code."""
    if args.out.exists():
        say(f"{args.out} already exists")
        return 1
    if not args.out.parent.is_dir():
        say(f"{args.out.parent} is not a directory")
        return 1
    # A stop that arrives while the recorder is built or the app is launched ends the command there.
    ready = asyncio.ensure_future(_prepare(args.app, helpers, connect, runner))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({ready, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            say("stopped before the recording started")
            return 1
        binary, window_id = await ready
    except BeansPickerError as err:
        say(f"no window to record: {err}")
        return 1
    finally:
        stopped.cancel()
        if not ready.done():
            ready.cancel()
            await asyncio.wait({ready})
    if binary is None:
        say("the recorder could not be built (it needs clang and the macOS 15 SDK)")
        return 1
    say(f"recording window {window_id} of {args.app} to {args.out}; stop with Ctrl-C")
    code = await until((str(binary), str(window_id), str(args.out)), stop)
    return 0 if code == 0 else 1


async def _prepare(app: str, helpers: RecordBuild, connect: Connect, runner: Runner) -> tuple[Path | None, int]:
    binary = await helpers.winrec_bin()
    if binary is None:
        return None, 0
    return binary, await _window(app, helpers, connect, runner)


async def _window(app: str, helpers: ActivateBuild, connect: Connect, runner: Runner) -> int:
    driver = await connect()
    try:
        bundle_id = resolve_bundle(app)
        target = AppTarget(bundle_id=bundle_id) if bundle_id else AppTarget(name=app)
        ctx = await ensure_app(
            driver,
            target,
            runner=runner,
            front=functools.partial(front_pid, runner=runner),
            restore=Activator(helpers, runner=runner),
        )
        return ctx.window_id
    finally:
        await driver.close()
