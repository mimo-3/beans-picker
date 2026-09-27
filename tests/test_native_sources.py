from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from beans_picker.observe.exacttext import EDITABLE_ROLES, TOGGLE_ROLES

ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = ROOT / "bench/fixture-app/build.sh"


@pytest.mark.parametrize(
    ("name", "usage"),
    [("axtext.m", "Usage: axtext <pid> [<output file>]"), ("menukeys.m", "menukeys <App.app path> [--nib <name>]")],
)
def test_helper_sources_are_package_data(name: str, usage: str) -> None:
    source = resources.files("beans_picker.native").joinpath(name)
    text = source.read_bytes().decode("utf-8")
    assert usage in text
    assert "int main(int argc, const char *argv[])" in text
    with resources.as_file(source) as path:
        assert path.is_file()


def _roles_in(text: str, function: str) -> set[str]:
    body = text[text.index(f"static BOOL {function}(") :]
    body = body[: body.index("\n}")]
    return set(re.findall(r'@"(AX\w+)"', body)) | {f"AX{m}" for m in re.findall(r"kAX(\w+)Role", body)}


def test_the_helper_reports_the_roles_exact_text_expects() -> None:
    text = resources.files("beans_picker.native").joinpath("axtext.m").read_text(encoding="utf-8")
    assert _roles_in(text, "toggle") == TOGGLE_ROLES
    assert _roles_in(text, "editable") == EDITABLE_ROLES


def test_fixture_build_script_is_executable() -> None:
    assert os.access(BUILD_SH, os.X_OK)


@pytest.mark.macos
@pytest.mark.skipif(shutil.which("clang") is None, reason="needs clang")
def test_fixture_app_builds_into_the_cache(tmp_path: Path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    proc = subprocess.run(["/bin/sh", str(BUILD_SH)], env=env, capture_output=True, text=True, check=True)
    app = tmp_path / "Library/Caches/beans-picker/fixture/BeansPickerFixture.app"
    assert proc.stdout == f"{app}\n"
    assert os.access(app / "Contents/MacOS/BeansPickerFixture", os.X_OK)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "dev.beans-picker.fixture"
    assert info["CFBundleExecutable"] == "BeansPickerFixture"
    assert info["NSPrincipalClass"] == "NSApplication"
