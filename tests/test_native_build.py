from __future__ import annotations

import shutil

import pytest

from cua_jev.observe.helpers import Helpers, axtext_binary
from cua_jev.paths import Paths

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
