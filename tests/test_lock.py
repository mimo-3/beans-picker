from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from beans_picker._proc import DEFAULT_MAX_BYTES, Completed
from beans_picker.driver.lock import screen_locked
from beans_picker.errors import ProcessError, ToolError

IOREG = ("ioreg", "-n", "Root", "-d1", "-a")


class Ioreg:
    def __init__(self, out: Completed | Exception) -> None:
        self.out = out
        self.seen: list[tuple[tuple[str, ...], int]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        check: bool = True,
        cwd: Path | None = None,
    ) -> Completed:
        self.seen.append((tuple(argv), max_bytes))
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def _session(**values: object) -> Completed:
    return Completed(plistlib.dumps([{"IOConsoleUsers": [values]}]).decode(), 0)


async def test_locked_when_the_session_says_so() -> None:
    runner = Ioreg(_session(CGSSessionScreenIsLocked=True))
    assert await screen_locked(runner=runner)
    assert runner.seen == [(IOREG, 8 * 1024 * 1024)]


@pytest.mark.parametrize("values", [{"CGSSessionScreenIsLocked": False}, {}])
async def test_a_valid_session_without_a_true_lock_flag_is_unlocked(values: dict[str, object]) -> None:
    assert not await screen_locked(runner=Ioreg(_session(**values)))


async def test_a_failed_lock_check_refuses_instead_of_assuming_unlocked() -> None:
    with pytest.raises(ToolError) as err:
        await screen_locked(runner=Ioreg(ProcessError("SENTINEL-ioreg-failed")))
    assert err.value.code == "screen_lock_unavailable"
    assert "SENTINEL" not in str(err.value)


@pytest.mark.parametrize("output", ["", "not a plist", "<dict>", "<key>CGSSessionScreenIsLocked</key>x<true/>"])
async def test_malformed_lock_status_refuses_instead_of_assuming_unlocked(output: str) -> None:
    with pytest.raises(ToolError) as err:
        await screen_locked(runner=Ioreg(Completed(output, 0)))
    assert err.value.code == "screen_lock_unavailable"


@pytest.mark.parametrize("data", [[], {}, [{"IOConsoleUsers": []}], [{"IOConsoleUsers": ["invalid"]}]])
async def test_missing_session_information_is_not_assumed_unlocked(data: list[object] | dict[str, object]) -> None:
    with pytest.raises(ToolError) as err:
        await screen_locked(runner=Ioreg(Completed(plistlib.dumps(data).decode(), 0)))
    assert err.value.code == "screen_lock_unavailable"
