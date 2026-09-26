from __future__ import annotations

import os
from pathlib import Path

import pytest

from cua_jev import config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_real_env_beats_env_local_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / ".env.local", "CUA_A=local\nCUA_B=local\n")
    _write(tmp_path / ".env", "CUA_A=env\nCUA_B=env\nCUA_C=env\n")
    monkeypatch.setenv("CUA_B", "")
    config.load_env(tmp_path)
    assert os.environ["CUA_A"] == "local"
    assert os.environ["CUA_B"] == ""
    assert os.environ["CUA_C"] == "env"


@pytest.mark.parametrize(
    ("line", "key", "value"),
    [
        ("CUA_X=plain", "CUA_X", "plain"),
        ("  export   CUA_X =  spaced  ", "CUA_X", "spaced"),
        ("CUA_X='single'", "CUA_X", "single"),
        ('CUA_X="double"', "CUA_X", "double"),
        ("CUA_X='mismatched\"", "CUA_X", "'mismatched\""),
        ('CUA_X="a"b"', "CUA_X", 'a"b'),
        ("CUA_X=b # c", "CUA_X", "b # c"),
        ("CUA_X=a\\nb", "CUA_X", "a\\nb"),
        ("CUA_X=", "CUA_X", ""),
        ('CUA_X=""', "CUA_X", ""),
        ("\ufeffCUA_X=bom", "CUA_X", "bom"),
        ("CUA_X=x\u3000", "CUA_X", "x"),
    ],
)
def test_line_forms(tmp_path: Path, line: str, key: str, value: str) -> None:
    _write(tmp_path / ".env", line + "\n")
    config.load_env(tmp_path)
    assert os.environ[key] == value


def test_export_as_a_name_and_skipped_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("export", raising=False)
    _write(
        tmp_path / ".env",
        "# comment\n\nexport=1\nexport CUA_NOEQ\nCUA_Y=cr\r\nCUA_BAD KEY=1\n1CUA=2\nCUA_Z=a\rb\n",
    )
    config.load_env(tmp_path)
    assert os.environ["export"] == "1"  # noqa: SIM112
    assert "CUA_NOEQ" not in os.environ
    assert os.environ["CUA_Y"] == "cr"
    assert "CUA_BAD" not in os.environ
    assert "CUA_Z" not in os.environ


def test_missing_files_are_skipped(tmp_path: Path) -> None:
    config.load_env(tmp_path / "nowhere")
    (tmp_path / ".env").mkdir()
    config.load_env(tmp_path)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, 5),
        ("", 5),
        ("  ", 5),
        (" 7 ", 7),
        ("7.0", 7),
        ("1e1", 10),
        ("0x10", 16),
        ("0b11", 3),
        ("7.5", 5),
        ("-3", 5),
        ("0", 5),
        ("abc", 5),
        ("Infinity", 5),
    ],
)
def test_effect_retakes(monkeypatch: pytest.MonkeyPatch, raw: str | None, want: int) -> None:
    if raw is not None:
        monkeypatch.setenv("CUA_JEV_EFFECT_RETAKES", raw)
    assert config.effect_retakes() == want


def test_jev_api_key_empty_string_is_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.jev_api_key() is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    assert config.jev_api_key() == "ts"
    monkeypatch.setenv("JEV_API_KEY", "")
    assert config.jev_api_key() == ""
    monkeypatch.setenv("JEV_API_KEY", "jev")
    assert config.jev_api_key() == "jev"


def test_model_and_driver_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.model() == "jev-latest"
    assert config.driver_bin() == str(tmp_path / ".local" / "bin" / "cua-driver")
    monkeypatch.setenv("CUA_JEV_MODEL", "")
    monkeypatch.setenv("CUA_DRIVER_BIN", "/opt/cua-driver")
    assert config.model() == ""
    assert config.driver_bin() == "/opt/cua-driver"


def test_values_loaded_later_apply(tmp_path: Path) -> None:
    assert config.model() == "jev-latest"
    _write(tmp_path / ".env.local", "CUA_JEV_MODEL=jev-1.13.0\n")
    config.load_env(tmp_path)
    assert config.model() == "jev-1.13.0"


def test_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.log_level() == "WARNING"
    monkeypatch.setenv("CUA_JEV_LOG_LEVEL", "")
    assert config.log_level() == "WARNING"
    monkeypatch.setenv("CUA_JEV_LOG_LEVEL", "debug")
    assert config.log_level() == "debug"


def _checkout(root: Path, name: str) -> Path:
    pkg = root / "src" / "cua_jev"
    pkg.mkdir(parents=True)
    _write(root / "pyproject.toml", f'[project]\nname = "{name}"\n')
    return pkg


def test_env_dirs_include_a_cua_jev_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    pkg = _checkout(tmp_path / "repo", "cua-jev")
    assert config.env_dirs(pkg) == [tmp_path / "repo", tmp_path / "xdg" / "cua-jev"]


def test_env_dirs_skip_another_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pkg = _checkout(tmp_path / "other", "someone-else")
    _write(tmp_path / "other" / ".env.local", "JEV_API_KEY=leak\n")
    assert config.env_dirs(pkg) == [tmp_path / "home" / ".config" / "cua-jev"]


def test_env_dirs_skip_an_installed_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    site = tmp_path / "proj" / ".venv" / "lib" / "python3.12" / "site-packages" / "cua_jev"
    site.mkdir(parents=True)
    _write(tmp_path / "proj" / "pyproject.toml", '[project]\nname = "cua-jev"\n')
    assert config.env_dirs(site) == [tmp_path / "home" / ".config" / "cua-jev"]


def test_checkout_root_rejects_bad_pyproject(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "cua_jev"
    pkg.mkdir(parents=True)
    assert config.checkout_root(pkg) is None
    _write(tmp_path / "pyproject.toml", "not toml [")
    assert config.checkout_root(pkg) is None
    _write(tmp_path / "pyproject.toml", 'project = "x"\n')
    assert config.checkout_root(pkg) is None


def test_this_checkout_qualifies() -> None:
    root = config.checkout_root()
    assert root is not None
    assert (root / "src" / "cua_jev" / "config.py").is_file()


def test_constants() -> None:
    assert config.JEV_RETRIES == 3
    t = config.THRESHOLDS
    assert (t.strict, t.agree, t.lead_ratio, t.max_none, t.not_found, t.shortlist_min) == (
        0.8,
        0.5,
        2.0,
        0.3,
        0.5,
        0.03,
    )
    assert dict(config.APP_BUNDLES) == {
        "calculator": "com.apple.calculator",
        "textedit": "com.apple.TextEdit",
        "preview": "com.apple.Preview",
        "stickies": "com.apple.Stickies",
        "notes": "com.apple.Notes",
    }
