from __future__ import annotations

from cua_jev.driver.types import Activation
from cua_jev.errors import (
    AppLaunchError,
    CuaJevError,
    DriverError,
    ForegroundViolation,
    JevBadResponse,
    JevError,
    JevUnavailable,
    ProcessError,
    ToolError,
)


def test_foreground_violation_message() -> None:
    a = Activation(pid=42, during="after click", at="12:34:56.789")
    err = ForegroundViolation(a)
    assert str(err) == (
        "foreground_violation: the app under test (pid 42) came to the front during after click at 12:34:56.789"
    )
    assert err.activation is a


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
    for cls in (ToolError, DriverError, AppLaunchError, ForegroundViolation, JevError, ProcessError):
        assert issubclass(cls, CuaJevError)
    assert issubclass(JevUnavailable, JevError)
    assert issubclass(JevBadResponse, JevError)
