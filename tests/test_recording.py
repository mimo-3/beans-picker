from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from mcp import Client, MCPError
from mcp.types import TextContent

from beans_picker.errors import ToolError
from beans_picker.observe.helpers import Helpers
from beans_picker.recording import Recorder
from beans_picker.server import create_server
from beans_picker.tools.session import Session
from tests.fakes import FakeDriver


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


class Build:
    def __init__(self, binary: Path | None = Path("/bp-impl-winrec")) -> None:
        self.binary = binary

    async def winrec_bin(self) -> Path | None:
        return self.binary


class Process:
    def __init__(
        self,
        *,
        ready: bytes = b'{"status":"recording"}\n',
        duration: object = 2.5,
        exit_code: int = 0,
        hang: bool = False,
    ) -> None:
        self.stdout = asyncio.StreamReader()
        if ready:
            self.stdout.feed_data(ready)
        self.returncode: int | None = None
        self.finished = json.dumps({"durationSeconds": duration}).encode()
        self.exit_code = exit_code
        self.hang = hang
        self.signals: list[int] = []
        self.killed = False
        self.reaped = False

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.reaped = True
        assert self.returncode is not None
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hang:
            await asyncio.Event().wait()
        self.returncode = self.exit_code
        self.reaped = True
        return self.finished, b""


class Spawn:
    def __init__(self, process: Process, payload: bytes = b"bp-impl-video") -> None:
        self.process = process
        self.payload = payload
        self.calls: list[tuple[Path, int, int, Path]] = []

    async def __call__(self, binary: Path, pid: int, window_id: int, out: Path) -> asyncio.subprocess.Process:
        self.calls.append((binary, pid, window_id, out))
        assert out.parent.name.startswith("bp-impl-")
        assert out.name.startswith("bp-impl-")
        assert out.parent.stat().st_mode & 0o077 == 0
        await asyncio.to_thread(out.write_bytes, self.payload)
        await asyncio.to_thread(out.chmod, 0o666)
        return cast(asyncio.subprocess.Process, self.process)


def recorder(spawn: Spawn, binary: Path | None = Path("/bp-impl-winrec")) -> Recorder:
    return Recorder(cast(Helpers, Build(binary)), spawn=spawn)


async def test_recording_waits_for_ready_and_publishes_only_after_stop(tmp_path: Path, permissive_umask: None) -> None:
    out = tmp_path / "bp-impl-video.mp4"
    process = Process()
    spawn = Spawn(process)
    owned = recorder(spawn)
    started = await owned.start(7, 10, out)
    assert started == {"status": "recording", "pid": 7, "windowId": 10, "path": str(out)}
    assert spawn.calls[0][:3] == (Path("/bp-impl-winrec"), 7, 10)
    assert not out.exists()
    result = await owned.stop()
    assert result == {"status": "done", "path": str(out), "durationSeconds": 2.5, "bytes": len(spawn.payload)}
    assert out.read_bytes() == spawn.payload
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert process.signals == [signal.SIGINT]
    assert process.reaped
    assert not spawn.calls[0][3].parent.exists()


async def test_recording_rejects_concurrent_start_and_stop_without_start(tmp_path: Path) -> None:
    spawn = Spawn(Process())
    owned = recorder(spawn)
    with pytest.raises(ToolError, match="no active recording"):
        await owned.stop()
    await owned.start(7, 10, tmp_path / "bp-impl-one.mp4")
    with pytest.raises(ToolError) as info:
        await owned.start(8, 11, tmp_path / "bp-impl-two.mp4")
    assert info.value.code == "recording_active"
    assert len(spawn.calls) == 1
    await owned.stop()
    with pytest.raises(ToolError) as info:
        await owned.stop()
    assert info.value.code == "recording_not_active"


@pytest.mark.parametrize("detector_fails", [False, True])
async def test_mcp_stop_finalizes_active_recording_even_without_unlocked_screen(
    detector_fails: bool, tmp_path: Path
) -> None:
    process = Process()
    spawn = Spawn(process)
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-lock.mp4"
    await owned.start(7, 10, out)
    connections: list[bool] = []
    checks: list[bool] = []

    async def connect() -> FakeDriver:
        connections.append(True)
        return FakeDriver()

    async def locked() -> bool:
        checks.append(True)
        if detector_fails:
            raise RuntimeError("lock probe unavailable")
        return True

    session = Session(connect)
    session.recorder = owned
    async with Client(create_server(session, screen_locked=locked), mode="legacy") as client:
        for tool, args in (
            ("record_start", {"pid": 7, "windowId": 10, "out": str(tmp_path / "bp-impl-refused.mp4")}),
            ("screenshot", {"scope": "screen"}),
        ):
            result = await client.call_tool(tool, args)
            assert result.is_error
        assert len(checks) == 2
        try:
            invalid = await client.call_tool("record_stop", {"pid": 7})
        except MCPError:
            pass
        else:
            assert invalid.is_error
        assert process.signals == []
        assert len(checks) == 2
        stopped = await client.call_tool("record_stop", {})
        assert isinstance(stopped.content[0], TextContent)
        assert json.loads(stopped.content[0].text)["status"] == "done"
        assert len(checks) == 2
    assert out.read_bytes() == spawn.payload
    assert process.signals == [signal.SIGINT]
    assert process.reaped
    assert not spawn.calls[0][3].parent.exists()
    assert connections == []
    await session.close()


async def test_cancellation_during_spawn_retains_child_ownership_and_reaps_it(tmp_path: Path) -> None:
    process = Process()
    entered = asyncio.Event()
    release = asyncio.Event()
    stages: list[Path] = []
    cancelled: list[bool] = []

    async def slow_spawn(binary: Path, pid: int, window_id: int, out: Path) -> asyncio.subprocess.Process:
        stages.append(out.parent)
        await asyncio.to_thread(out.write_bytes, b"video")
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return cast(asyncio.subprocess.Process, process)

    owned = Recorder(cast(Helpers, Build()), spawn=slow_spawn)
    out = tmp_path / "bp-impl-spawn-cancel.mp4"
    task = asyncio.create_task(owned.start(7, 10, out))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == []
    assert process.killed
    assert process.reaped
    assert not stages[0].exists()
    assert not out.exists()
    await owned.close()


async def test_recording_refuses_existing_output_or_missing_helper_before_spawn(tmp_path: Path) -> None:
    spawn = Spawn(Process())
    out = tmp_path / "bp-impl-existing.mp4"
    out.write_bytes(b"preserve")
    with pytest.raises(ToolError):
        await recorder(spawn).start(7, 10, out)
    with pytest.raises(ToolError) as info:
        await recorder(spawn, binary=None).start(7, 10, tmp_path / "bp-impl-new.mp4")
    assert info.value.code == "recording_unavailable"
    assert out.read_bytes() == b"preserve"
    assert spawn.calls == []


async def test_failed_readiness_kills_reaps_and_allows_a_later_start(tmp_path: Path) -> None:
    process = Process(ready=b'{"status":"failed"}\n')
    spawn = Spawn(process)
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-refused.mp4"
    with pytest.raises(ToolError) as info:
        await owned.start(7, 10, out)
    assert info.value.code == "recording_failed"
    assert process.killed
    assert process.reaped
    assert not out.exists()
    assert not spawn.calls[0][3].parent.exists()
    spawn.process = Process()
    await owned.start(7, 10, out)
    await owned.stop()
    assert out.exists()


async def test_readiness_timeout_kills_and_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("beans_picker.recording.START_WAIT_S", 0.001)
    process = Process(ready=b"")
    spawn = Spawn(process)
    with pytest.raises(ToolError) as info:
        await recorder(spawn).start(7, 10, tmp_path / "bp-impl-timeout.mp4")
    assert info.value.code == "recording_failed"
    assert process.killed
    assert process.reaped
    assert not spawn.calls[0][3].parent.exists()


@pytest.mark.parametrize("duration", [0, -1, True, None, "2.5", float("inf"), float("nan")])
async def test_stop_rejects_invalid_duration_without_publishing(duration: object, tmp_path: Path) -> None:
    spawn = Spawn(Process(duration=duration))
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-invalid.mp4"
    await owned.start(7, 10, out)
    with pytest.raises(ToolError) as info:
        await owned.stop()
    assert info.value.code == "recording_failed"
    assert not out.exists()
    assert not spawn.calls[0][3].parent.exists()


@pytest.mark.parametrize(("exit_code", "payload"), [(3, b"video"), (0, b"")])
async def test_stop_rejects_failed_or_empty_video(exit_code: int, payload: bytes, tmp_path: Path) -> None:
    spawn = Spawn(Process(exit_code=exit_code), payload)
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-invalid.mp4"
    await owned.start(7, 10, out)
    with pytest.raises(ToolError):
        await owned.stop()
    assert not out.exists()


async def test_stop_timeout_kills_and_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("beans_picker.recording.STOP_GRACE_S", 0.001)
    process = Process(hang=True)
    spawn = Spawn(process)
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-stop-timeout.mp4"
    await owned.start(7, 10, out)
    with pytest.raises(ToolError) as info:
        await owned.stop()
    assert info.value.code == "recording_failed"
    assert process.signals == [signal.SIGINT]
    assert process.killed
    assert process.reaped
    assert not out.exists()


async def test_close_is_idempotent_and_finalizes_active_recording(tmp_path: Path) -> None:
    spawn = Spawn(Process())
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-close.mp4"
    await owned.start(7, 10, out)
    await owned.close()
    await owned.close()
    assert out.read_bytes() == spawn.payload
    assert spawn.process.signals == [signal.SIGINT]


async def test_session_shutdown_closes_owned_recorder() -> None:
    closed: list[bool] = []

    class Owned:
        async def close(self) -> None:
            closed.append(True)

    async def connect() -> FakeDriver:
        return FakeDriver()

    session = Session(connect)
    session.recorder = cast(Recorder, Owned())
    await session.close()
    await session.close()
    assert closed == [True]


@pytest.mark.parametrize("ready", [b"not JSON\n", b"[]\n", b"{}\n"])
async def test_malformed_readiness_is_explicit_recording_failure(ready: bytes, tmp_path: Path) -> None:
    process = Process(ready=ready)
    spawn = Spawn(process)
    out = tmp_path / "bp-impl-malformed.mp4"
    with pytest.raises(ToolError) as info:
        await recorder(spawn).start(7, 10, out)
    assert info.value.code == "recording_failed"
    assert process.killed
    assert process.reaped
    assert not out.exists()


@pytest.mark.parametrize("finished", [b"not JSON", b"[]", b"{}"])
async def test_malformed_finalization_is_explicit_recording_failure(finished: bytes, tmp_path: Path) -> None:
    process = Process()
    process.finished = finished
    owned = recorder(Spawn(process))
    out = tmp_path / "bp-impl-malformed.mp4"
    await owned.start(7, 10, out)
    with pytest.raises(ToolError) as info:
        await owned.stop()
    assert info.value.code == "recording_failed"
    assert not out.exists()


async def test_output_created_while_recording_is_preserved(tmp_path: Path) -> None:
    spawn = Spawn(Process())
    owned = recorder(spawn)
    out = tmp_path / "bp-impl-race.mp4"
    await owned.start(7, 10, out)
    out.write_bytes(b"keep")
    with pytest.raises(ToolError) as info:
        await owned.stop()
    assert info.value.code == "output_exists"
    assert out.read_bytes() == b"keep"
    assert not spawn.calls[0][3].parent.exists()


@pytest.mark.parametrize(
    ("pid", "window_id"),
    [
        (0, 10),
        (-1, 10),
        (2**31, 10),
        (True, 10),
        (7, 0),
        (7, -1),
        (7, 2**32),
        (7, True),
    ],
)
async def test_recording_refuses_ids_that_cannot_identify_exact_native_window(
    pid: int, window_id: int, tmp_path: Path
) -> None:
    spawn = Spawn(Process())
    owned = recorder(spawn)
    try:
        with pytest.raises(ToolError):
            await owned.start(pid, window_id, tmp_path / "bp-impl-invalid-id.mp4")
        assert spawn.calls == []
    finally:
        await owned.close()


async def test_recording_accepts_native_id_upper_bounds_without_truncation(tmp_path: Path) -> None:
    spawn = Spawn(Process())
    owned = recorder(spawn)
    await owned.start(2**31 - 1, 2**32 - 1, tmp_path / "bp-impl-max-ids.mp4")
    assert spawn.calls[0][1:3] == (2**31 - 1, 2**32 - 1)
    await owned.stop()
