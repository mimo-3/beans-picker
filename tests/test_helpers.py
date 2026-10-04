from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest

from beans_picker._proc import DEFAULT_MAX_BYTES, Completed
from beans_picker.errors import ProcessError
from beans_picker.observe.helpers import AXTEXT_PLIST, MENUKEYS_PLIST, Helpers, native_sources
from beans_picker.paths import Paths


class _Clang:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list[tuple[str, ...]] = []
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()

    async def __call__(
        self, argv: Sequence[str], /, *, max_bytes: int = DEFAULT_MAX_BYTES, check: bool = True, cwd: Path | None = None
    ) -> Completed:
        self.calls.append(tuple(argv))
        self.started.set()
        await self.release.wait()
        if self.fails:
            raise ProcessError("clang exited with code 1", returncode=1)
        _write_output(Path(argv[argv.index("-o") + 1]))
        return Completed("", 0)


def _write_output(path: Path) -> None:
    path.write_bytes(b"binary")


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "axtext.m").write_bytes(b"// axtext\n")
    (src / "menukeys.m").write_bytes(b"// menukeys\n")
    (src / "activate.m").write_bytes(b"// activate\n")
    (src / "winrec.m").write_bytes(b"// winrec\n")
    return src


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def test_builds_the_axtext_app_once(paths: Paths, sources: Path, caplog: pytest.LogCaptureFixture) -> None:
    clang = _Clang()
    helpers = Helpers(paths, runner=clang, sources=sources)
    with caplog.at_level(logging.WARNING, logger="beans_picker"):
        app = await helpers.axtext_app()
    expected = paths.axtext / f"BeansPickerAXText-{_hash(sources / 'axtext.m')}.app"
    assert app == expected
    assert clang.calls == [
        (
            "clang",
            "-fobjc-arc",
            "-O2",
            "-framework",
            "ApplicationServices",
            "-framework",
            "Foundation",
            "-o",
            clang.calls[0][8],
            str(sources / "axtext.m"),
        )
    ]
    staged = Path(clang.calls[0][8])
    assert staged.parts[-4:] == (expected.name, "Contents", "MacOS", "axtext")
    assert staged.parents[3].parent == paths.axtext
    assert list(paths.axtext.iterdir()) == [expected]
    assert (expected / "Contents" / "Info.plist").read_text(encoding="utf-8") == AXTEXT_PLIST
    assert [r.getMessage() for r in caplog.records] == [
        "building the axtext helper; the first call may take a few seconds"
    ]
    assert await helpers.axtext_app() == expected
    assert len(clang.calls) == 1


async def test_an_existing_build_is_reused(paths: Paths, sources: Path) -> None:
    clang = _Clang()
    app = paths.axtext / f"BeansPickerAXText-{_hash(sources / 'axtext.m')}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "axtext").write_bytes(b"built earlier")
    binary = paths.menukeys / f"menukeys-{_hash(sources / 'menukeys.m')}"
    paths.menukeys.mkdir(parents=True)
    binary.write_bytes(b"built earlier")
    helpers = Helpers(paths, runner=clang, sources=sources)
    assert await helpers.axtext_app() == app
    assert await helpers.menukeys_bin() == binary
    assert clang.calls == []


async def test_a_failed_axtext_build_is_removed_and_remembered(paths: Paths, sources: Path) -> None:
    clang = _Clang(fails=True)
    helpers = Helpers(paths, runner=clang, sources=sources)
    assert await helpers.axtext_app() is None
    assert await helpers.axtext_app() is None
    assert len(clang.calls) == 1
    assert list(paths.axtext.iterdir()) == []


async def test_builds_the_menukeys_binary_with_an_embedded_plist(paths: Paths, sources: Path) -> None:
    clang = _Clang()
    helpers = Helpers(paths, runner=clang, sources=sources)
    binary = await helpers.menukeys_bin()
    expected = paths.menukeys / f"menukeys-{_hash(sources / 'menukeys.m')}"
    plist = paths.menukeys / "Info.plist"
    assert binary == expected
    assert clang.calls == [
        (
            "clang",
            "-fobjc-arc",
            "-O2",
            "-framework",
            "AppKit",
            f"-Wl,-sectcreate,__TEXT,__info_plist,{plist}",
            "-o",
            clang.calls[0][7],
            str(sources / "menukeys.m"),
        )
    ]
    staged = Path(clang.calls[0][7])
    assert staged.name == expected.name
    assert staged.parent.parent == paths.menukeys
    assert sorted(paths.menukeys.iterdir()) == [plist, expected]
    assert plist.read_text(encoding="utf-8") == MENUKEYS_PLIST
    assert await helpers.menukeys_bin() == expected
    assert len(clang.calls) == 1


async def test_a_failed_menukeys_build_is_remembered(paths: Paths, sources: Path) -> None:
    clang = _Clang(fails=True)
    helpers = Helpers(paths, runner=clang, sources=sources)
    assert await helpers.menukeys_bin() is None
    assert await helpers.menukeys_bin() is None
    assert len(clang.calls) == 1


async def test_missing_sources_build_nothing(paths: Paths, tmp_path: Path) -> None:
    clang = _Clang()
    helpers = Helpers(paths, runner=clang, sources=tmp_path / "nowhere")
    assert await helpers.axtext_app() is None
    assert await helpers.menukeys_bin() is None
    assert await helpers.activate_bin() is None
    assert await helpers.winrec_bin() is None
    assert clang.calls == []


async def test_concurrent_callers_share_one_build(paths: Paths, sources: Path) -> None:
    clang = _Clang()
    clang.release.clear()
    helpers = Helpers(paths, runner=clang, sources=sources)
    first = asyncio.create_task(helpers.axtext_app())
    second = asyncio.create_task(helpers.axtext_app())
    await asyncio.sleep(0)
    first.cancel()
    clang.release.set()
    assert await second is not None
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(clang.calls) == 1


def test_the_sources_ship_with_the_package() -> None:
    sources = native_sources()
    assert sources.joinpath("axtext.m").read_bytes().startswith(b"//")
    assert sources.joinpath("menukeys.m").read_bytes().startswith(b"//")


async def test_an_unwritable_cache_means_no_helper(paths: Paths, sources: Path) -> None:
    paths.axtext.parent.mkdir(parents=True, exist_ok=True)
    paths.axtext.write_bytes(b"a file where the directory should be")
    paths.menukeys.write_bytes(b"a file where the directory should be")
    clang = _Clang()
    helpers = Helpers(paths, runner=clang, sources=sources)
    assert await helpers.axtext_app() is None
    assert await helpers.axtext_app() is None
    assert await helpers.menukeys_bin() is None
    assert clang.calls == []


async def test_a_build_in_progress_elsewhere_is_not_taken_as_done(paths: Paths, sources: Path) -> None:
    first_clang = _Clang()
    first_clang.release.clear()
    first = asyncio.create_task(Helpers(paths, runner=first_clang, sources=sources).axtext_app())
    await first_clang.started.wait()
    _write_output(Path(first_clang.calls[0][8]))
    second_clang = _Clang()
    second = await Helpers(paths, runner=second_clang, sources=sources).axtext_app()
    assert len(second_clang.calls) == 1
    first_clang.release.set()
    assert await first == second
    assert second is not None
    assert list(paths.axtext.iterdir()) == [second]


async def test_close_cancels_a_running_build(paths: Paths, sources: Path) -> None:
    clang = _Clang()
    clang.release.clear()
    helpers = Helpers(paths, runner=clang, sources=sources)
    caller = asyncio.create_task(helpers.menukeys_bin())
    await clang.started.wait()
    await helpers.close()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert [p.name for p in paths.menukeys.iterdir()] == ["Info.plist"]


async def test_builds_the_activate_and_winrec_binaries_once(paths: Paths, sources: Path) -> None:
    clang = _Clang()
    helpers = Helpers(paths, runner=clang, sources=sources)
    activate = await helpers.activate_bin()
    winrec = await helpers.winrec_bin()
    assert activate == paths.activate / f"activate-{_hash(sources / 'activate.m')}"
    assert winrec == paths.winrec / f"winrec-{_hash(sources / 'winrec.m')}"
    assert await helpers.activate_bin() == activate
    assert await Helpers(paths, runner=clang, sources=sources).winrec_bin() == winrec
    assert [call[:3] for call in clang.calls] == [("clang", "-fobjc-arc", "-O2"), ("clang", "-fobjc-arc", "-O2")]
    assert "-mmacosx-version-min=15.0" in clang.calls[1]
    assert "ScreenCaptureKit" in clang.calls[1]
    assert [p.name for p in paths.activate.iterdir()] == [activate.name]


async def test_a_failed_winrec_build_is_remembered_and_leaves_nothing(paths: Paths, sources: Path) -> None:
    clang = _Clang(fails=True)
    helpers = Helpers(paths, runner=clang, sources=sources)
    assert await helpers.winrec_bin() is None
    assert await helpers.winrec_bin() is None
    assert len(clang.calls) == 1
    assert list(paths.winrec.iterdir()) == []
