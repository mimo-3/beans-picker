from __future__ import annotations

import pytest

from beans_picker.errors import (
    AppLaunchError,
    BeansPickerError,
    DriverError,
    DriverTimeout,
    DriverUnavailable,
    JevBadResponse,
    JevError,
    JevUnavailable,
    ProcessError,
    ToolError,
    failure,
)


def test_driver_error_message() -> None:
    err = DriverError("get_window_state", "tool_error", "no such window")
    assert str(err) == "get_window_state refused (tool_error): no such window"
    assert (err.tool, err.code, err.message) == ("get_window_state", "tool_error", "no such window")


def test_tool_error_carries_code() -> None:
    err = ToolError("bad_target", "no app matches")
    assert str(err) == "no app matches"
    assert err.code == "bad_target"


def test_process_error_fields() -> None:
    err = ProcessError("x exited with code 2", returncode=2, stdout="out")
    assert (str(err), err.returncode, err.stdout) == ("x exited with code 2", 2, "out")
    assert ProcessError("y").returncode is None


def test_hierarchy() -> None:
    for cls in (
        ToolError,
        DriverError,
        DriverUnavailable,
        DriverTimeout,
        AppLaunchError,
        JevError,
        ProcessError,
    ):
        assert issubclass(cls, BeansPickerError)
    assert issubclass(JevUnavailable, JevError)
    assert issubclass(JevBadResponse, JevError)


@pytest.mark.parametrize(
    ("err", "code", "message"),
    [
        (ToolError("bad_target", "give app"), "bad_target", "give app"),
        (JevUnavailable("no key"), "jev_unavailable", "no key"),
        (JevBadResponse("bad"), "jev_bad_response", "bad"),
        (DriverUnavailable("missing"), "driver_unavailable", "missing"),
        (DriverTimeout("slow"), "driver_timeout", "slow"),
        (DriverError("click", "stale", "gone"), "driver_error", "click refused (stale): gone"),
        (RuntimeError("SENTINEL-exception-private-text"), "internal", "an internal error occurred"),
    ],
)
def test_failure_codes(err: Exception, code: str, message: str | None) -> None:
    assert failure(err) == (code, message if message is not None else str(err))
