from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from beans_picker import __version__, cli, record


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr("beans_picker.config.env_dirs", lambda: [tmp_path])
    monkeypatch.setattr("beans_picker.log.configure", lambda: events.append("log"))

    async def serve() -> None:
        events.append("serve")

    async def grant() -> str:
        events.append("grant")
        return "listed under Accessibility"

    async def recording(args: record.RecordArgs) -> int:
        events.append(f"record {args.app}")
        return 0

    monkeypatch.setattr(cli, "_record", recording)
    monkeypatch.setattr(cli, "_serve", serve)
    monkeypatch.setattr(cli, "_grant_ax", grant)
    return events


def test_version_is_printed_anywhere(capsys: pytest.CaptureFixture[str], no_side_effects: list[str]) -> None:
    assert cli.main(["--version"], platform="linux") == 0
    assert capsys.readouterr().out == f"{__version__}\n"
    assert no_side_effects == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_usage_instead_of_serving(
    flag: str, capsys: pytest.CaptureFixture[str], no_side_effects: list[str]
) -> None:
    assert cli.main([flag], platform="linux") == 0
    out = capsys.readouterr().out
    assert out == cli.USAGE + "\n"
    assert "grant-ax" in out
    assert no_side_effects == []


def test_refuses_to_serve_off_macos(capsys: pytest.CaptureFixture[str], no_side_effects: list[str]) -> None:
    assert cli.main([], platform="linux") == 1
    captured = capsys.readouterr()
    assert captured.err == "beans-picker runs on macOS only\n"
    assert captured.out == ""
    assert no_side_effects == []


@pytest.mark.parametrize("args", [["--verison"], ["serve"], ["grant-ax", "extra"]])
def test_unknown_arguments_print_the_usage_and_fail(
    args: list[str], capsys: pytest.CaptureFixture[str], no_side_effects: list[str]
) -> None:
    assert cli.main(args, platform="darwin") == 2
    captured = capsys.readouterr()
    assert captured.err == cli.USAGE + "\n"
    assert captured.out == ""
    assert no_side_effects == []


def test_serves_without_arguments(
    capsys: pytest.CaptureFixture[str], no_side_effects: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_BASE_URL", "")
    monkeypatch.delenv("TYPESAFE_BASE_URL")
    (tmp_path / ".env.local").write_text("TYPESAFE_BASE_URL=http://localhost:1\n", encoding="utf-8")
    assert cli.main([], platform="darwin") == 0
    assert no_side_effects == ["log", "serve"]
    assert os.environ["TYPESAFE_BASE_URL"] == "http://localhost:1"
    assert capsys.readouterr().out == ""


def test_grant_ax_prints_the_helpers_answer(capsys: pytest.CaptureFixture[str], no_side_effects: list[str]) -> None:
    assert cli.main(["grant-ax"], platform="darwin") == 0
    assert capsys.readouterr().out == "listed under Accessibility\n"
    assert no_side_effects == ["log", "grant"]


def test_record_runs_the_recorder(no_side_effects: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["record", "--app", "Calculator", "--out", "demo.mp4"], platform="darwin") == 0
    assert no_side_effects == ["log", "record Calculator"]
    assert capsys.readouterr().out == ""


def test_record_without_its_arguments_prints_its_usage(
    no_side_effects: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["record", "--app", "Calculator"], platform="darwin") == 2
    assert capsys.readouterr().err == record.USAGE + "\n"
    assert no_side_effects == []


async def test_the_record_command_stops_on_a_signal_and_closes_its_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    seen: list[bool] = []

    async def fake(args: record.RecordArgs, stop: asyncio.Event, **kwargs: object) -> int:
        say = kwargs["say"]
        assert callable(say)
        say("recording")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 5)
        seen.append(stop.is_set())
        return 0

    monkeypatch.setattr(record, "record", fake)
    assert await cli._record(record.RecordArgs("Calculator", tmp_path / "demo.mp4")) == 0
    assert seen == [True]
    assert capsys.readouterr().err == "recording\n"


def test_record_is_refused_off_macos(no_side_effects: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["record", "--app", "Calculator", "--out", "demo.mp4"], platform="linux") == 1
    assert capsys.readouterr().err == "beans-picker runs on macOS only\n"
    assert no_side_effects == []
