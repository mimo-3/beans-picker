"""Smoke test of an installed beans-picker, run from outside the checkout."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Final

from mcp import Client

import beans_picker
from beans_picker.observe.helpers import Helpers
from beans_picker.paths import Paths
from beans_picker.server import create_server

CHECKOUT: Final = Path(__file__).resolve().parent.parent
TOOLS: Final = ["act", "extract", "observe"]


def imported_from_checkout() -> Path | None:
    """Where beans_picker was imported from, when that is this checkout rather than the install."""
    package = Path(beans_picker.__file__).resolve()
    return package if package.is_relative_to(CHECKOUT) else None


def fail(message: str) -> int:
    sys.stderr.write(f"smoke test failed: {message}\n")
    return 1


async def list_tools() -> list[str]:
    async with Client(create_server()) as client:
        return sorted(tool.name for tool in (await client.list_tools()).tools)


async def build_helpers() -> list[str]:
    """The helpers that did not build."""
    with tempfile.TemporaryDirectory() as cache:
        helpers = Helpers(Paths(cache=Path(cache)))
        broken = []
        if await helpers.axtext_app() is None:
            broken.append("axtext")
        if await helpers.menukeys_bin() is None:
            broken.append("menukeys")
        return broken


async def main() -> int:
    if package := imported_from_checkout():
        return fail(f"beans_picker was imported from the checkout ({package}), not from the installed wheel")
    if (names := await list_tools()) != TOOLS:
        return fail(f"tools are {names}, expected {TOOLS}")
    for name in ("axtext.m", "menukeys.m"):
        if not resources.files("beans_picker.native").joinpath(name).read_bytes():
            return fail(f"native/{name} is empty")
    if sys.platform == "darwin" and (broken := await build_helpers()):
        return fail(f"could not build {', '.join(broken)}")
    sys.stdout.write(f"beans-picker {beans_picker.__version__}: ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
