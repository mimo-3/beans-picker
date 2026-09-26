"""Exceptions that cross layers. `str(err)` is always the exact message a tool reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cua_jev.driver.types import Activation


class CuaJevError(Exception):
    """Base of every exception this package raises on purpose."""


class ToolError(CuaJevError):
    """A tool-level refusal, reported as `{"status": "failed", "code": code, ...}`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DriverError(CuaJevError):
    """cua-driver refused a call that had to succeed: `"{tool} refused ({code}): {message}"`."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool} refused ({code}): {message}")
        self.tool = tool
        self.code = code
        self.message = message


class AppLaunchError(CuaJevError):
    """The target app could not be launched, or showed no window."""


class ForegroundViolation(CuaJevError):
    """The app under test came to the front while it was being driven."""

    def __init__(self, activation: Activation) -> None:
        super().__init__(
            f"foreground_violation: the app under test (pid {activation.pid}) came to the front "
            f"during {activation.during} at {activation.at}"
        )
        self.activation = activation


class JevError(CuaJevError):
    """Base of the Jev client's errors."""


class JevUnavailable(JevError):
    """Jev cannot be reached or refused the request; reported with the code `jev_unavailable`."""


class JevBadResponse(JevError):
    """Jev answered with something that is not a valid answer to the questions asked."""


class ProcessError(CuaJevError):
    """A subprocess could not be started, exited non-zero, or wrote too much output."""

    def __init__(self, message: str, *, returncode: int | None = None, stdout: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
