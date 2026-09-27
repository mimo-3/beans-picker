from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.macos  # the server runs on macOS only

ROOT = Path(__file__).resolve().parent.parent


def _driver_wrapper(tmp_path: Path) -> Path:
    state = tmp_path / "driver-state"
    state.mkdir()
    script = tmp_path / "cua-driver"
    fake = ROOT / "tests" / "fake_driver.py"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" --state "{state}" "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def test_serves_over_stdio_and_exits_on_end_of_input(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CUA_", "TYPESAFE_")) and k != "JEV_API_KEY"}
    env.update({"CUA_DRIVER_BIN": str(_driver_wrapper(tmp_path)), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "beans_picker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=tmp_path,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    frames: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def answer(rid: int) -> dict[str, Any]:
        assert proc.stdout is not None
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 20)
            frame: dict[str, Any] = json.loads(line)
            frames.append(frame)
            if frame.get("id") == rid:
                return frame

    try:
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        init = await answer(1)
        assert init["result"]["serverInfo"]["name"] == "beans-picker"
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = await answer(2)
        assert [t["name"] for t in tools["result"]["tools"]] == ["observe", "act", "extract"]
        await send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "observe", "arguments": {}}})
        call = await answer(3)
        assert call["result"]["isError"] is True
        assert json.loads(call["result"]["content"][0]["text"])["code"] in ("bad_target", "screen_locked")
        proc.stdin.close()
        rest = await asyncio.wait_for(proc.stdout.read(), 20)
        assert await asyncio.wait_for(proc.wait(), 20) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    frames.extend(json.loads(line) for line in rest.decode().splitlines() if line.strip())
    assert all(f.get("jsonrpc") == "2.0" for f in frames)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_a_signal_ends_serving_while_stdin_stays_open(tmp_path: Path, sig: signal.Signals) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CUA_", "TYPESAFE_")) and k != "JEV_API_KEY"}
    env.update({"CUA_DRIVER_BIN": str(_driver_wrapper(tmp_path)), "XDG_CONFIG_HOME": str(tmp_path / "config")})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "beans_picker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=tmp_path,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
        assert json.loads(await asyncio.wait_for(proc.stdout.readline(), 20))["id"] == 1
        proc.send_signal(sig)
        assert await asyncio.wait_for(proc.wait(), 10) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
