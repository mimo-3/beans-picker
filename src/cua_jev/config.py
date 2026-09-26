"""Settings read from the environment and from `.env.local` / `.env` files."""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from cua_jev._numbers import parse_number
from cua_jev._text import NOT_LINE_END, WS

ENV_FILES: Final[tuple[str, ...]] = (".env.local", ".env")

DEFAULT_MODEL: Final = "jev-latest"
DEFAULT_EFFECT_RETAKES: Final = 5
DEFAULT_LOG_LEVEL: Final = "WARNING"
DEFAULT_JEV_CONNECT_TIMEOUT: Final = 10.0
DEFAULT_JEV_READ_TIMEOUT: Final = 120.0
DEFAULT_DRIVER_TIMEOUT: Final = 120.0

JEV_RETRIES: Final = 3

_PACKAGE_DIR: Final = Path(__file__).resolve().parent
_DIST_NAME: Final = "cua-jev"

_LINE_RE: Final = re.compile(f"{WS}*(?:export{WS}+)?([A-Za-z_][A-Za-z0-9_]*){WS}*={WS}*({NOT_LINE_END}*?){WS}*")
_QUOTED_RE: Final = re.compile(f"(['\"])({NOT_LINE_END}*)\\1")
_LINE_SPLIT_RE: Final = re.compile(r"\r?\n")


def load_env(directory: Path) -> None:
    """Set variables from `directory/.env.local`, then `directory/.env`."""
    for name in ENV_FILES:
        path = directory / name
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for line in _LINE_SPLIT_RE.split(text):
            m = _LINE_RE.fullmatch(line)
            if m is None:
                continue
            key, raw = m.group(1), m.group(2)
            if key in os.environ:
                continue
            q = _QUOTED_RE.fullmatch(raw)
            os.environ[key] = q.group(2) if q is not None else raw


def checkout_root(package_dir: Path = _PACKAGE_DIR) -> Path | None:
    """The source checkout this package runs from, if it runs from one."""
    if package_dir.name != "cua_jev" or package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    try:
        with (root / "pyproject.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if isinstance(project, dict) and project.get("name") == _DIST_NAME:
        return root
    return None


def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/cua-jev`, or `~/.config/cua-jev` when that variable is unset or empty."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "cua-jev"


def env_dirs(package_dir: Path = _PACKAGE_DIR) -> list[Path]:
    """Directories whose env files are loaded, in order: the source checkout, then the config directory."""
    dirs: list[Path] = []
    root = checkout_root(package_dir)
    if root is not None:
        dirs.append(root)
    dirs.append(config_dir())
    return dirs


def jev_api_key() -> str | None:
    """`JEV_API_KEY` if set and not empty, else `TYPESAFE_API_KEY` if set, else None."""
    return os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")


def model() -> str:
    """The Jev model: `CUA_JEV_MODEL` if set (even empty), else `jev-latest`."""
    value = os.environ.get("CUA_JEV_MODEL")
    return value if value is not None else DEFAULT_MODEL


def driver_bin() -> str:
    """The cua-driver binary: `CUA_DRIVER_BIN` (with `~` expanded) if set and not empty, else
    `~/.local/bin/cua-driver`."""
    value = os.environ.get("CUA_DRIVER_BIN")
    # The binary is spawned without a shell, so nothing else would expand `~`.
    return str(Path(value).expanduser()) if value else str(Path.home() / ".local" / "bin" / "cua-driver")


def effect_retakes() -> int:
    """How many fresh snapshots act takes before judging an action's effect."""
    raw = os.environ.get("CUA_JEV_EFFECT_RETAKES")
    n = parse_number(raw) if raw is not None else math.nan
    if math.isfinite(n) and n == math.floor(n) and n > 0:
        return int(n)
    return DEFAULT_EFFECT_RETAKES


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    n = parse_number(raw) if raw is not None else math.nan
    return n if math.isfinite(n) and n > 0 else default


def jev_connect_timeout() -> float:
    """Seconds to wait for a connection to Jev: `JEV_CONNECT_TIMEOUT`, default 10."""
    return _seconds("JEV_CONNECT_TIMEOUT", DEFAULT_JEV_CONNECT_TIMEOUT)


def jev_read_timeout() -> float:
    """Seconds to wait for Jev's answer (and to send, and for a pooled connection): `JEV_READ_TIMEOUT`, default 120."""
    return _seconds("JEV_READ_TIMEOUT", DEFAULT_JEV_READ_TIMEOUT)


def driver_timeout() -> float:
    """Seconds to wait for cua-driver to start or answer one call: `CUA_DRIVER_TIMEOUT`, default 120."""
    return _seconds("CUA_DRIVER_TIMEOUT", DEFAULT_DRIVER_TIMEOUT)


def log_level() -> str:
    """`CUA_JEV_LOG_LEVEL` if set and not empty, else `WARNING`."""
    value = os.environ.get("CUA_JEV_LOG_LEVEL")
    return value if value else DEFAULT_LOG_LEVEL


@dataclass(frozen=True, slots=True, kw_only=True)
class Thresholds:
    """How sure Jev's answer must be before a candidate is picked."""

    # A pick this sure is taken even when the forced question disagrees.
    strict: float = 0.8
    # Both questions agree and the strict one is at least this sure.
    agree: float = 0.5
    # The leader must hold this multiple of the runner-up's probability.
    lead_ratio: float = 2.0
    # `none` above this blocks a pick.
    max_none: float = 0.3
    # `none` at least this likely with no candidate above it means not_found.
    not_found: float = 0.5
    # Candidates at least this likely are listed on an ambiguous answer.
    shortlist_min: float = 0.03


THRESHOLDS: Final = Thresholds()

APP_BUNDLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "calculator": "com.apple.calculator",
        "textedit": "com.apple.TextEdit",
        "preview": "com.apple.Preview",
        "stickies": "com.apple.Stickies",
        "notes": "com.apple.Notes",
    }
)
