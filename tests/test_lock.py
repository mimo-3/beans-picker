from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from beans_picker._proc import DEFAULT_MAX_BYTES, Completed
from beans_picker.driver.lock import screen_locked
from beans_picker.errors import ProcessError

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


async def test_locked_when_the_session_says_so() -> None:
    runner = Ioreg(Completed("<dict>\n\t<key>CGSSessionScreenIsLocked</key>\n\t<true/>\n</dict>", 0))
    assert await screen_locked(runner=runner)
    assert runner.seen == [(IOREG, 8 * 1024 * 1024)]


async def test_not_locked_otherwise() -> None:
    assert not await screen_locked(runner=Ioreg(Completed("<key>CGSSessionScreenIsLocked</key><false/>", 0)))
    assert not await screen_locked(runner=Ioreg(Completed("<dict></dict>", 0)))
    assert not await screen_locked(runner=Ioreg(Completed("<key>CGSSessionScreenIsLocked</key>x<true/>", 0)))


async def test_a_failure_counts_as_unlocked() -> None:
    assert not await screen_locked(runner=Ioreg(ProcessError("ioreg: output exceeded 8388608 bytes")))
