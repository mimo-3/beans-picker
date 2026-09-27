from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from beans_picker import log


@pytest.fixture
def clean_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger(log.LOGGER_NAME)
    handlers, level = list(logger.handlers), logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)


@pytest.mark.parametrize(
    ("name", "want"),
    [
        (None, logging.WARNING),
        ("debug", logging.DEBUG),
        (" INFO ", logging.INFO),
        ("error", logging.ERROR),
        ("15", 15),
        (logging.CRITICAL, logging.CRITICAL),
        ("chatty", logging.WARNING),
    ],
)
def test_parse_level(name: str | int | None, want: int) -> None:
    assert log.parse_level(name) == want


def test_configure_writes_to_the_given_stream(clean_logger: logging.Logger) -> None:
    buf = io.StringIO()
    log.configure("info", stream=buf)
    logging.getLogger("beans_picker.driver.mcp").info("hello %s", "there")
    logging.getLogger("beans_picker.x").debug("hidden")
    assert "INFO beans_picker.driver.mcp: hello there" in buf.getvalue()
    assert "hidden" not in buf.getvalue()


def test_configure_reads_the_env_and_replaces_its_handler(
    clean_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEANS_PICKER_LOG_LEVEL", "debug")
    log.configure(stream=io.StringIO())
    log.configure(stream=io.StringIO())
    assert clean_logger.level == logging.DEBUG
    ours = [h for h in clean_logger.handlers if type(h).__name__ == "_StderrHandler"]
    assert len(ours) == 1


def test_silence_stops_records_and_propagation() -> None:
    root = logging.getLogger()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root.addHandler(handler)
    name = "beans_picker_test_silenced"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    try:
        log.silence(name)
        logger.warning("SECRET-1")
        logging.getLogger(f"{name}.child").warning("SECRET-2")
        assert "SECRET" not in buf.getvalue()
    finally:
        root.removeHandler(handler)
        logger.propagate = True
        logger.disabled = False
