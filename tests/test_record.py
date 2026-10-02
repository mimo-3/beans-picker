from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from beans_picker._json import JsonObject
from beans_picker.driver.mcp import Connect, Driver
from beans_picker.driver.types import ToolOk, ToolRefused, ToolResult
from beans_picker.record import RecordArgs, parse_args, record
from tests.fakes import FakeDriver, FakeRunner

BIN = Path("/cache/winrec/winrec-abc")


class _Build:
    def __init__(self, binary: Path | None = BIN) -> None:
        self.binary = binary

    async def winrec_bin(self) -> Path | None:
        return self.binary

    async def activate_bin(self) -> Path | None:
        return None


class _Until:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[tuple[str, ...], asyncio.Event]] = []

    async def __call__(self, argv: Sequence[str], stop: asyncio.Event) -> int:
        self.calls.append((tuple(argv), stop))
        return self.code


def _launched(tool: str, args: dict[str, object]) -> ToolResult:
    if tool == "launch_app":
        window: JsonObject = {"window_id": 10, "pid": 7, "title": "Calculator", "layer": 0, "z_index": 1}
        return ToolOk(data={"pid": 7, "name": "Calculator", "windows": [window]}, text="", ms=1)
    return ToolRefused(code="unknown", message=f"no {tool}", data={}, text="", ms=1)


def _connect(driver: FakeDriver) -> Connect:
    async def connect() -> Driver:
        return driver

    return connect


@pytest.mark.parametrize(
    "argv",
    [
        ["--app", "Calculator", "--out", "demo.mp4"],
        ["--out", "demo.mp4", "--app", "Calculator"],
    ],
)
def test_parse_args_takes_both_flags_in_either_order(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert parse_args(argv) == RecordArgs(app="Calculator", out=tmp_path.resolve() / "demo.mp4")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--app", "Calculator"],
        ["--app", "Calculator", "--app", "TextEdit"],
        ["--app", "", "--out", "demo.mp4"],
        ["--app", "Calculator", "--out", ""],
        ["--app", "Calculator", "--out", "demo.mp4", "extra"],
        ["Calculator", "--app", "demo.mp4", "--out"],
    ],
)
def test_parse_args_refuses_anything_else(argv: list[str]) -> None:
    assert parse_args(argv) is None


async def test_records_the_apps_window_until_stopped(tmp_path: Path) -> None:
    out = tmp_path / "demo.mp4"
    driver = FakeDriver(_launched)
    until = _Until()
    stop = asyncio.Event()
    said: list[str] = []
    code = await record(
        RecordArgs("Calculator", out),
        stop,
        helpers=_Build(),
        say=said.append,
        connect=_connect(driver),
        runner=FakeRunner(),
        until=until,
    )
    assert code == 0
    assert until.calls == [((str(BIN), "10", str(out)), stop)]
    assert driver.tools == ["launch_app"]
    assert driver.closed
    assert said == [f"recording window 10 of Calculator to {out}; stop with Ctrl-C"]


async def test_a_recorder_that_fails_is_exit_code_one(tmp_path: Path) -> None:
    code = await record(
        RecordArgs("my-app", tmp_path / "demo.mp4"),
        asyncio.Event(),
        helpers=_Build(),
        say=lambda _: None,
        connect=_connect(FakeDriver(_launched)),
        runner=FakeRunner(),
        until=_Until(-2),
    )
    assert code == 1


async def test_refuses_before_recording(tmp_path: Path) -> None:
    out = tmp_path / "demo.mp4"
    until = _Until()
    said: list[str] = []

    async def go(helpers: _Build, driver: FakeDriver) -> int:
        return await record(
            RecordArgs("Calculator", out),
            asyncio.Event(),
            helpers=helpers,
            say=said.append,
            connect=_connect(driver),
            runner=FakeRunner(),
            until=until,
        )

    refusing = FakeDriver(lambda tool, args: ToolRefused(code="unknown", message="no", data={}, text="", ms=1))
    assert await go(_Build(None), FakeDriver(_launched)) == 1
    assert await go(_Build(), refusing) == 1
    assert refusing.closed
    out.write_bytes(b"")
    assert await go(_Build(), FakeDriver(_launched)) == 1
    assert said == [
        "the recorder could not be built (it needs clang and the macOS 15 SDK)",
        "no window to record: could not launch com.apple.calculator: no",
        f"{out} already exists",
    ]
    assert until.calls == []


async def test_refuses_a_file_in_a_directory_that_is_not_there(tmp_path: Path) -> None:
    said: list[str] = []
    driver = FakeDriver(_launched)
    code = await record(
        RecordArgs("Calculator", tmp_path / "missing" / "demo.mp4"),
        asyncio.Event(),
        helpers=_Build(),
        say=said.append,
        connect=_connect(driver),
        runner=FakeRunner(),
        until=_Until(),
    )
    assert code == 1
    assert said == [f"{tmp_path / 'missing'} is not a directory"]
    assert driver.calls == []


async def test_a_stop_while_the_app_is_launched_ends_the_command_without_recording(tmp_path: Path) -> None:
    stop = asyncio.Event()
    launching = asyncio.Event()

    async def hang(tool: str, args: dict[str, object]) -> ToolResult:
        launching.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    driver = FakeDriver(hang)
    until = _Until()
    said: list[str] = []
    task = asyncio.create_task(
        record(
            RecordArgs("Calculator", tmp_path / "demo.mp4"),
            stop,
            helpers=_Build(),
            say=said.append,
            connect=_connect(driver),
            runner=FakeRunner(),
            until=until,
        )
    )
    await launching.wait()
    stop.set()
    assert await task == 1
    assert said == ["stopped before the recording started"]
    assert until.calls == []
    assert driver.closed
