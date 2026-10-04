"""Entry point: `beans-picker` serves MCP over stdio; `record` takes a video of one window."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Sequence
from typing import Final

from mcp.server.stdio import stdio_server

from beans_picker import __version__, config, log, record
from beans_picker._stdin import StdinLines
from beans_picker.observe.exacttext import prompt_for_access
from beans_picker.observe.helpers import Helpers
from beans_picker.paths import Paths
from beans_picker.server import create_server
from beans_picker.tools.session import Session

USAGE: Final = (
    "usage: beans-picker             serve MCP over stdio (started by an MCP client)\n"
    "       beans-picker grant-ax    list the exact-text helper under Privacy & Security > Accessibility\n"
    "       beans-picker record --app <name or bundle id> --out <file.mp4>\n"
    "                                record the app's window until Ctrl-C\n"
    "       beans-picker --version   print the version"
)


def main(argv: Sequence[str] | None = None, *, platform: str | None = None) -> int:
    """Runs the command line; returns the exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(__version__)  # noqa: T201 - the one line this command prints
        return 0
    if args in (["--help"], ["-h"]):
        print(USAGE)  # noqa: T201 - this command's output
        return 0
    recording = record.parse_args(args[1:]) if args[:1] == ["record"] else None
    if args and args != ["grant-ax"] and recording is None:
        print(record.USAGE if args[0] == "record" else USAGE, file=sys.stderr)  # noqa: T201 - reported before anything starts
        return 2
    if (platform if platform is not None else sys.platform) != "darwin":
        print("beans-picker runs on macOS only", file=sys.stderr)  # noqa: T201 - reported before anything starts
        return 1
    # The Jev SDK reads TYPESAFE_* itself, so the env files are loaded before anything else runs.
    for directory in config.env_dirs():
        config.load_env(directory)
    log.configure()
    if args == ["grant-ax"]:
        print(asyncio.run(_grant_ax()))  # noqa: T201 - the helper's answer is this command's output
        return 0
    if recording is not None:
        return asyncio.run(_record(recording))
    asyncio.run(_serve())
    return 0


async def _grant_ax() -> str:
    return await prompt_for_access(Helpers(Paths.default()))


def _say(line: str) -> None:
    print(line, file=sys.stderr)  # noqa: T201 - the record command's progress


async def _record(args: record.RecordArgs) -> int:
    """Records until SIGINT or SIGTERM, which stop the recorder so that it can finish its file."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    helpers = Helpers(Paths.default())
    try:
        return await record.record(args, stop, helpers=helpers, say=_say)
    finally:
        await helpers.close()


async def _serve() -> None:
    """Serves until stdin closes or a signal arrives; signals during cleanup are ignored."""
    session = Session()
    server = create_server(session)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    stopping = False

    def stop() -> None:
        nonlocal stopping
        if not stopping and task is not None:
            stopping = True
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    try:
        async with stdio_server(stdin=StdinLines()) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except asyncio.CancelledError:
        if not stopping or task is None:
            raise
        task.uncancel()
    finally:
        stopping = True
        await session.close()
