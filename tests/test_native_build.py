from __future__ import annotations

import shutil

import pytest

from beans_picker._proc import run
from beans_picker.observe.helpers import Helpers, axtext_binary
from beans_picker.paths import Paths

pytestmark = [pytest.mark.macos, pytest.mark.skipif(shutil.which("clang") is None, reason="needs clang")]


async def test_builds_both_helpers(paths: Paths) -> None:
    helpers = Helpers(paths)
    app = await helpers.axtext_app()
    assert app is not None
    assert axtext_binary(app).is_file()
    assert (app / "Contents" / "Info.plist").is_file()
    binary = await helpers.menukeys_bin()
    assert binary is not None
    assert binary.is_file()
    again = Helpers(paths)
    assert await again.axtext_app() == app
    assert await again.menukeys_bin() == binary


async def test_builds_window_recorder_without_running_it(paths: Paths) -> None:
    sdk = await run(["xcrun", "--sdk", "macosx", "--show-sdk-version"], check=False)
    if sdk.returncode != 0:
        pytest.skip("macOS SDK is unavailable")
    major = int(sdk.stdout.strip().split(".", maxsplit=1)[0])
    if major < 15:
        pytest.skip("SCRecordingOutput requires the macOS 15 SDK or newer")
    helpers = Helpers(paths)
    binary = await helpers.winrec_bin()
    assert binary is not None
    assert binary.is_file()
    assert await Helpers(paths).winrec_bin() == binary
