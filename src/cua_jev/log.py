"""Logging setup. Library modules only call `logging.getLogger(__name__)`; the command line entry
point calls `configure()` once, and all output goes to stderr (stdout carries the MCP protocol)."""

from __future__ import annotations

import logging
import sys
from typing import Final, TextIO

from cua_jev import config

LOGGER_NAME: Final = "cua_jev"
_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _StderrHandler(logging.StreamHandler[TextIO]):
    """The handler `configure()` installs, recognizable so a second call replaces it."""


def parse_level(name: str | int | None) -> int:
    """A logging level from a name (`"debug"`, `"INFO"`, ...) or number; WARNING when unknown."""
    if isinstance(name, int):
        return name
    if name is None:
        return logging.WARNING
    text = name.strip()
    if text.isdigit():
        return int(text)
    level = logging.getLevelNamesMapping().get(text.upper())
    return level if level is not None else logging.WARNING


def configure(level: str | int | None = None, *, stream: TextIO | None = None) -> logging.Logger:
    """Send the `cua_jev` logger's records to `stream` (default stderr) at `level` (default: the
    `CUA_JEV_LOG_LEVEL` setting). Calling it again replaces the handler it installed before."""
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, _StderrHandler):
            logger.removeHandler(h)
    handler = _StderrHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(parse_level(level if level is not None else config.log_level()))
    return logger


def silence(name: str) -> None:
    """Turn one logger off completely: no records, and nothing propagated to the root handlers.
    Only that logger (and its children) is affected."""
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.disabled = True
