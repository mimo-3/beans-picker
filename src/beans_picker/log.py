"""Logging setup."""

from __future__ import annotations

import logging
import sys
from typing import Final, TextIO

from beans_picker import config

LOGGER_NAME: Final = "beans_picker"
_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _StderrHandler(logging.StreamHandler[TextIO]): ...


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
    """Send the `beans_picker` logger's records to `stream`; calling again replaces the handler."""
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
    """Turn one logger off completely: no records, and nothing propagated to the root handlers."""
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.disabled = True
