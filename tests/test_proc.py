from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from beans_picker._proc import Completed, Runner, run, run_until
from beans_picker.errors import ProcessError
from tests.fakes import fake_runner

PY = sys.executable


def _real(p: Path) -> Path:
    return p.resolve()


async def test_returns_stdout_and_code(tmp_path: Path) -> None:
    want = f"hi {_real(tmp_path)}\n"
    out = await run([PY, "-c", "import os; print('hi', os.getcwd())"], cwd=tmp_path)
    assert out.returncode == 0
    assert out.stdout == want


async def test_stdin_and_stderr_are_null() -> None:
    code = "import sys; sys.stderr.write('noise'); print(repr(sys.stdin.read()))"
    out = await run([PY, "-c", code])
    assert out.stdout == "''\n"


async def test_local_helpers_do_not_inherit_jev_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_API_KEY", "SENTINEL-primary")
    monkeypatch.setenv("TYPESAFE_API_KEY", "SENTINEL-alias")
    monkeypatch.setenv("BEANS_PICKER_TEST_ENV", "preserved")
    code = (
        "import os; print(os.getenv('JEV_API_KEY'), os.getenv('TYPESAFE_API_KEY'), os.getenv('BEANS_PICKER_TEST_ENV'))"
    )
    out = await run([PY, "-c", code])
    assert out.stdout == "None None preserved\n"
    assert os.environ["JEV_API_KEY"] == "SENTINEL-primary"


async def test_decodes_invalid_utf8_with_replacement() -> None:
    out = await run([PY, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"])
    assert out.stdout == "a\ufffdb"


async def test_nonzero_exit_raises_with_check() -> None:
    with pytest.raises(ProcessError) as info:
        await run([PY, "-c", "print('partial'); raise SystemExit(3)"])
    assert info.value.returncode == 3
    assert info.value.stdout == "partial\n"


async def test_nonzero_exit_without_check() -> None:
    out = await run([PY, "-c", "raise SystemExit(4)"], check=False)
    assert out == Completed(stdout="", returncode=4)


async def test_spawn_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(ProcessError, match="could not start"):
        await run([str(tmp_path / "missing-binary")])
    with pytest.raises(ProcessError, match="empty command"):
        await run([])


async def test_output_over_the_limit_kills_the_child() -> None:
    code = "import sys, time\nsys.stdout.write('x' * 5000); sys.stdout.flush(); time.sleep(30)"
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ProcessError, match="exceeded 1000 bytes"):
        await run([PY, "-c", code], max_bytes=1000)
    assert loop.time() - started < 20


async def test_cancellation_kills_and_reaps_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    code = f"import os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)"
    task = asyncio.create_task(run([PY, "-c", code]))
    for _ in range(500):
        if marker.exists() and marker.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_fake_runner_matches_the_real_contract() -> None:
    runner: Runner = fake_runner(
        {
            ("lsappinfo", "front"): Completed(stdout="ASN:0x0-0x1:\n", returncode=0),
            ("false",): Completed(stdout="", returncode=1),
            ("boom",): OSError("nope"),
        }
    )
    assert (await runner(["lsappinfo", "front"])).stdout == "ASN:0x0-0x1:\n"
    assert (await runner(["false"], check=False)).returncode == 1
    with pytest.raises(ProcessError):
        await runner(["false"])
    with pytest.raises(OSError, match="nope"):
        await runner(["boom"])
    with pytest.raises(ProcessError):
        await runner(["unknown"])


WAITS_FOR_SIGINT = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGINT, lambda *_: sys.exit(7))\n"
    "open(sys.argv[1], 'w').close()\n"
    "time.sleep(30)\n"
)


def _is_up(marker: Path) -> bool:
    return marker.exists()


async def _started(marker: Path) -> None:
    for _ in range(500):
        if _is_up(marker):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the child never started")


async def test_run_until_interrupts_the_child_when_asked_and_returns_its_code(tmp_path: Path) -> None:
    stop = asyncio.Event()
    marker = tmp_path / "started"
    task = asyncio.create_task(run_until([PY, "-c", WAITS_FOR_SIGINT, str(marker)], stop))
    await _started(marker)
    assert not task.done()
    stop.set()
    assert await task == 7


async def test_run_until_returns_when_the_child_exits_on_its_own() -> None:
    assert await run_until([PY, "-c", "import sys; sys.exit(3)"], asyncio.Event()) == 3


async def test_run_until_spawn_failures_raise(tmp_path: Path) -> None:
    with pytest.raises(ProcessError, match="empty command"):
        await run_until([], asyncio.Event())
    with pytest.raises(ProcessError, match="could not start"):
        await run_until([str(tmp_path / "missing")], asyncio.Event())


async def test_run_until_kills_the_child_when_cancelled(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    task = asyncio.create_task(run_until([PY, "-c", WAITS_FOR_SIGINT, str(marker)], asyncio.Event()))
    await _started(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_until_kills_a_child_that_does_not_stop_in_time(tmp_path: Path) -> None:
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'w').close()\n"
        "time.sleep(30)\n"
    )
    stop = asyncio.Event()
    marker = tmp_path / "started"
    task = asyncio.create_task(run_until([PY, "-c", code, str(marker)], stop, grace=0.1))
    await _started(marker)
    stop.set()
    assert await task == -signal.SIGKILL
