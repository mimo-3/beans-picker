from __future__ import annotations

from pathlib import Path

from beans_picker._proc import Completed
from beans_picker.driver.activate import Activator
from beans_picker.errors import ProcessError
from tests.fakes import fake_runner


class _Build:
    def __init__(self, binary: Path | None, *, fails: bool = False) -> None:
        self.binary = binary
        self.fails = fails

    async def activate_bin(self) -> Path | None:
        if self.fails:
            raise OSError("no cache")
        return self.binary


BIN = Path("/cache/activate/activate-abc")


async def test_runs_the_helper_with_the_pid() -> None:
    runner = fake_runner({(str(BIN), "42"): Completed("", 0)})
    assert await Activator(_Build(BIN), runner=runner)(42) is True
    assert runner.calls == [(str(BIN), "42")]


async def test_a_helper_that_fails_or_cannot_be_built_is_false() -> None:
    refused = fake_runner({(str(BIN), "42"): ProcessError("activate exited with code 1", returncode=1)})
    assert await Activator(_Build(BIN), runner=refused)(42) is False
    unbuilt = fake_runner()
    assert await Activator(_Build(None), runner=unbuilt)(42) is False
    assert await Activator(_Build(BIN, fails=True), runner=unbuilt)(42) is False
    assert unbuilt.calls == []
