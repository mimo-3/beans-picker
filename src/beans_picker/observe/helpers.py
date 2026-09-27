"""Builds of the two native helpers, compiled with clang into the cache on first use."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final, Protocol

from beans_picker._proc import Runner, run
from beans_picker.errors import ProcessError
from beans_picker.paths import Paths

_log = logging.getLogger(__name__)

AXTEXT_SOURCE: Final = "axtext.m"
MENUKEYS_SOURCE: Final = "menukeys.m"

_AXTEXT_FLAGS: Final = ("-fobjc-arc", "-O2", "-framework", "ApplicationServices", "-framework", "Foundation")
_MENUKEYS_FLAGS: Final = ("-fobjc-arc", "-O2", "-framework", "AppKit")

AXTEXT_PLIST: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>dev.beans-picker.axtext</string>'
    "<key>CFBundleName</key><string>beans-picker axtext</string><key>CFBundleExecutable</key><string>axtext</string>"
    "<key>CFBundlePackageType</key><string>APPL</string><key>LSBackgroundOnly</key><true/></dict></plist>\n"
)
# Without an Info.plist allowing mixed localizations, AppKit localizes the nib to English.
MENUKEYS_PLIST: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>dev.beans-picker.menukeys</string>'
    "<key>CFBundleAllowMixedLocalizations</key><true/></dict></plist>\n"
)


class AxtextBuild(Protocol):
    """Where the exact-text helper app comes from."""

    async def axtext_app(self) -> Path | None: ...


def native_sources() -> Traversable:
    return resources.files("beans_picker.native")


def axtext_binary(app: Path) -> Path:
    return app / "Contents" / "MacOS" / "axtext"


class Helpers:
    """The helper builds of one session, each compiled at most once and shared by concurrent callers."""

    def __init__(self, paths: Paths, *, runner: Runner = run, sources: Traversable | None = None) -> None:
        self._paths = paths
        self._runner = runner
        self._sources = sources if sources is not None else native_sources()
        self._axtext: asyncio.Task[Path | None] | None = None
        self._menukeys: asyncio.Task[Path | None] | None = None

    async def axtext_app(self) -> Path | None:
        """The exact-text helper's app bundle, built on first use; `None` when it cannot be built."""
        if self._axtext is None:
            self._axtext = asyncio.ensure_future(_or_none("axtext", self._build_axtext))
        return await _shared(self._axtext)

    async def menukeys_bin(self) -> Path | None:
        """The menu key helper's executable, built on first use; `None` when it cannot be built."""
        if self._menukeys is None:
            self._menukeys = asyncio.ensure_future(_or_none("menukeys", self._build_menukeys))
        return await _shared(self._menukeys)

    async def close(self) -> None:
        """Cancels builds still running and waits for them to stop."""
        running = [t for t in (self._axtext, self._menukeys) if t is not None and not t.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running)

    async def _build_axtext(self) -> Path | None:
        source = self._sources.joinpath(AXTEXT_SOURCE)
        if not source.is_file():
            return None
        app = self._paths.axtext / f"BeansPickerAXText-{_source_hash(source)}.app"
        if axtext_binary(app).exists():
            return app
        stage = _staging_dir(self._paths.axtext, app.name)
        try:
            staged = stage / app.name
            _prepare_bundle(staged)
            _log.warning("building the axtext helper; the first call may take a few seconds")
            with resources.as_file(source) as path:
                if not await self._compile(("clang", *_AXTEXT_FLAGS, "-o", str(axtext_binary(staged)), str(path))):
                    return None
            try:
                staged.rename(app)
            except OSError:
                if not axtext_binary(app).exists():
                    raise
            return app
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    async def _build_menukeys(self) -> Path | None:
        source = self._sources.joinpath(MENUKEYS_SOURCE)
        if not source.is_file():
            return None
        binary = self._paths.menukeys / f"menukeys-{_source_hash(source)}"
        if binary.exists():
            return binary
        plist = _write_menukeys_plist(self._paths.menukeys)
        stage = _staging_dir(self._paths.menukeys, binary.name)
        try:
            staged = stage / binary.name
            _log.warning("building the menukeys helper; the first call may take a few seconds")
            with resources.as_file(source) as path:
                embed_plist = f"-Wl,-sectcreate,__TEXT,__info_plist,{plist}"
                if not await self._compile(("clang", *_MENUKEYS_FLAGS, embed_plist, "-o", str(staged), str(path))):
                    return None
            staged.replace(binary)
            return binary
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    async def _compile(self, argv: tuple[str, ...]) -> bool:
        try:
            await self._runner(argv)
        except ProcessError:
            return False
        return True


async def _or_none(name: str, build: Callable[[], Awaitable[Path | None]]) -> Path | None:
    try:
        return await build()
    except Exception as err:
        _log.warning("building the %s helper failed: %s", name, err)
        return None


async def _shared(task: asyncio.Task[Path | None]) -> Path | None:
    return await asyncio.shield(task)


def _staging_dir(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{name}.", dir=directory))


def _source_hash(source: Traversable) -> str:
    return hashlib.sha256(source.read_bytes()).hexdigest()[:12]


def _prepare_bundle(app: Path) -> None:
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(AXTEXT_PLIST, encoding="utf-8")


def _write_menukeys_plist(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    plist = directory / "Info.plist"
    fd, tmp = tempfile.mkstemp(prefix=".Info.plist.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(MENUKEYS_PLIST)
        Path(tmp).replace(plist)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return plist
