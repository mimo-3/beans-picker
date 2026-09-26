from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from cua_jev.paths import Paths

_ISOLATED_PREFIXES = ("TYPESAFE_", "CUA_")
_ISOLATED_NAMES = ("JEV_API_KEY", "XDG_CONFIG_HOME")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tests marked `macos` need macOS; elsewhere they are skipped."""
    if platform.system() == "Darwin":
        return
    skip = pytest.mark.skip(reason="needs macOS")
    for item in items:
        if "macos" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without the variables this package reads (restored afterwards)."""
    for name in list(os.environ):
        if name in _ISOLATED_NAMES or name.startswith(_ISOLATED_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """Cache directories under the test's temp dir, never the real cache."""
    return Paths(cache=tmp_path / "cache")
