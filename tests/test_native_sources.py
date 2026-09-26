"""The Objective-C sources: shipped as package data, and the bench fixture app builds."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = ROOT / "bench/fixture-app/build.sh"


@pytest.mark.parametrize(
    ("name", "usage"),
    [("axtext.m", "Usage: axtext <pid> [<output file>]"), ("menukeys.m", "menukeys <App.app path> [--nib <name>]")],
)
def test_helper_sources_are_package_data(name: str, usage: str) -> None:
    source = resources.files("cua_jev.native").joinpath(name)
    text = source.read_bytes().decode("utf-8")
    assert usage in text
    assert "int main(int argc, const char *argv[])" in text
    with resources.as_file(source) as path:
        assert path.is_file()


def test_fixture_build_script_is_executable() -> None:
    assert os.access(BUILD_SH, os.X_OK)


@pytest.mark.macos
@pytest.mark.skipif(shutil.which("clang") is None, reason="needs clang")
def test_fixture_app_builds_into_the_cache(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    proc = subprocess.run(["/bin/sh", str(BUILD_SH)], env=env, capture_output=True, text=True, check=True)
    app = tmp_path / "Library/Caches/cua-jev/fixture/CuaJevFixture.app"
    assert proc.stdout == f"{app}\n"
    assert os.access(app / "Contents/MacOS/CuaJevFixture", os.X_OK)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "dev.cua-jev.fixture"
    assert info["CFBundleExecutable"] == "CuaJevFixture"
    assert info["NSPrincipalClass"] == "NSApplication"
