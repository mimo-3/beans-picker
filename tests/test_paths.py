from __future__ import annotations

from pathlib import Path

import pytest

from cua_jev.paths import Paths


def test_default_is_the_user_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Paths.default().cache == tmp_path / "Library" / "Caches" / "cua-jev"


def test_subdirectories(paths: Paths) -> None:
    assert paths.axtext == paths.cache / "axtext"
    assert paths.shots == paths.cache / "shots"
    assert paths.menukeys == paths.cache / "menukeys"
