"""Exceptions that cross layers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beans_picker.driver.types import Activation


class BeansPickerError(Exception):
    """Base of every exception this package raises on purpose."""


class ToolError(BeansPickerError):
    """A tool-level refusal, reported as `{"status": "failed", "code": code, ...}`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DriverError(BeansPickerError):
    """cua-driver refused a call that had to succeed: `"{tool} refused ({code}): {message}"`."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool} refused ({code}): {message}")
        self.tool = tool
        self.code = code
        self.message = message


class DriverUnavailable(BeansPickerError):
    """cua-driver could not be started; reported with the code `driver_unavailable`."""


class DriverTimeout(BeansPickerError):
    """cua-driver did not answer within the configured time; reported with the code `driver_timeout`."""


class AppLaunchError(BeansPickerError):
    """The target app could not be launched, or showed no window."""


class ForegroundViolation(BeansPickerError):
    """The app under test came to the front while it was being driven."""

    def __init__(self, activation: Activation) -> None:
        super().__init__(
            f"foreground_violation: the app under test (pid {activation.pid}) came to the front "
            f"during {activation.during} at {activation.at}; the app in front before was "
            f"{'put back' if activation.restored else 'not put back'}"
        )
        self.activation = activation


class JevError(BeansPickerError):
    """Base of the Jev client's errors."""


class JevUnavailable(JevError):
    """Jev cannot be reached or refused the request; reported with the code `jev_unavailable`."""


class JevBadResponse(JevError):
    """Jev answered with something that is not a valid answer to the questions asked."""


class ProcessError(BeansPickerError):
    """A subprocess could not be started, exited non-zero, or wrote too much output."""

    def __init__(self, message: str, *, returncode: int | None = None, stdout: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout


def failure(err: Exception) -> tuple[str, str]:
    """The `code` and `message` a tool reports for an exception it did not handle itself."""
    match err:
        case ToolError():
            return err.code, err.message
        case JevUnavailable():
            return "jev_unavailable", str(err)
        case JevBadResponse():
            return "jev_bad_response", str(err)
        case DriverUnavailable():
            return "driver_unavailable", str(err)
        case DriverTimeout():
            return "driver_timeout", str(err)
        case DriverError():
            return "driver_error", str(err)
        case ForegroundViolation():
            return "foreground_violation", str(err)
        case _:
            return "internal", "an internal error occurred"
