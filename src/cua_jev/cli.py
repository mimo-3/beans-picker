"""Entry point: `cua-jev` serves MCP over stdio.

stdout carries the protocol, so nothing else is written there while serving. Settings are read
from `.env.local` / `.env` in the cua-jev checkout and in `~/.config/cua-jev/`, whatever the
caller's working directory; the real environment wins. `cua-jev grant-ax` asks macOS to list the
exact-text helper under Accessibility; `cua-jev --version` prints the version and `cua-jev --help`
the usage.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Sequence
from typing import Final

from mcp.server.stdio import stdio_server

from cua_jev import __version__, config, log
from cua_jev._stdin import StdinLines
from cua_jev.observe.exacttext import prompt_for_access
from cua_jev.observe.helpers import Helpers
from cua_jev.paths import Paths
from cua_jev.server import create_server
from cua_jev.tools.session import Session

USAGE: Final = (
    "usage: cua-jev             serve MCP over stdio (started by an MCP client)\n"
    "       cua-jev grant-ax    list the exact-text helper under Privacy & Security > Accessibility\n"
    "       cua-jev --version   print the version"
)


def main(argv: Sequence[str] | None = None, *, platform: str | None = None) -> int:
    """Runs the command line; returns the exit code. Arguments other than the ones above are ignored."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(__version__)  # noqa: T201 - the one line this command prints
        return 0
    if args in (["--help"], ["-h"]):
        print(USAGE)  # noqa: T201 - this command's output
        return 0
    if (platform if platform is not None else sys.platform) != "darwin":
        print("cua-jev runs on macOS only", file=sys.stderr)  # noqa: T201 - reported before anything starts
        return 1
    # The Jev SDK reads TYPESAFE_* itself, so the env files are loaded before anything else runs.
    for directory in config.env_dirs():
        config.load_env(directory)
    log.configure()
    if args[:1] == ["grant-ax"]:
        print(asyncio.run(_grant_ax()))  # noqa: T201 - the helper's answer is this command's output
        return 0
    asyncio.run(_serve())
    return 0


async def _grant_ax() -> str:
    return await prompt_for_access(Helpers(Paths.default()))


async def _serve() -> None:
    """Serves until stdin closes or SIGINT/SIGTERM arrives, then closes the session. Signals that
    arrive while the session closes are ignored, so they cannot cut the cleanup short."""
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
        task.uncancel()  # a signal: shut down like on end of input
    finally:
        stopping = True
        await session.close()
