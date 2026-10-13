"""Session-owned MCP recording of an existing exact window using the CLI's winrec helper."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from beans_picker._proc import STOP_GRACE_S, _child_env
from beans_picker.errors import ToolError
from beans_picker.observe.helpers import Helpers
from beans_picker.tools.artifacts import output_path, publish

type Spawn = Callable[[Path, int, int, Path], Awaitable[asyncio.subprocess.Process]]
START_WAIT_S = 20.0


async def _spawn(binary: Path, pid: int, window_id: int, out: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        str(binary),
        str(window_id),
        str(out),
        str(pid),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_child_env(),
        limit=4096,
    )


class Recorder:
    def __init__(self, helpers: Helpers, *, spawn: Spawn = _spawn) -> None:
        self._helpers = helpers
        self._spawn = spawn
        self._process: asyncio.subprocess.Process | None = None
        self._stage: TemporaryDirectory[str] | None = None
        self._out: Path | None = None

    async def start(self, pid: int, window_id: int, out: Path) -> dict[str, object]:
        if self._process is not None:
            raise ToolError("recording_active", "stop the active recording before starting another")
        if (
            isinstance(pid, bool)
            or isinstance(window_id, bool)
            or not 1 <= pid <= 2147483647
            or not 1 <= window_id <= 4294967295
        ):
            raise ToolError("bad_target", "pid and windowId must fit macOS process and window identifiers")
        out = output_path(str(out), suffix=".mp4")
        binary = await self._helpers.winrec_bin()
        if binary is None:
            raise ToolError("recording_unavailable", "recording requires clang and the macOS 15 SDK")
        self._stage = TemporaryDirectory(prefix="bp-impl-record-", dir=out.parent)
        self._out = out
        try:
            spawn_task = asyncio.ensure_future(
                self._spawn(binary, pid, window_id, Path(self._stage.name) / "bp-impl-video.mp4")
            )
            try:
                process = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                # Creation may have launched the child before yielding its handle. Finish acquiring
                # ownership before the outer cleanup reaps it, even with repeated cancellation.
                while not spawn_task.done():
                    try:
                        await asyncio.shield(spawn_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                with contextlib.suppress(Exception):
                    self._process = spawn_task.result()
                raise
            self._process = process
            stream = process.stdout
            if stream is None:
                raise ToolError("recording_failed", "the recorder has no readiness stream")
            async with asyncio.timeout(START_WAIT_S):
                ready = await stream.readline()
            if json.loads(ready).get("status") != "recording":
                raise ToolError("recording_failed", "the recorder did not confirm capture started")
            return {"status": "recording", "pid": pid, "windowId": window_id, "path": str(out)}
        except (TimeoutError, ValueError, AttributeError, OSError) as err:
            await self._discard()
            raise ToolError("recording_failed", "the recorder did not confirm capture started") from err
        except BaseException:
            await self._discard()
            raise

    async def stop(self) -> dict[str, object]:
        process, stage, out = self._process, self._stage, self._out
        if process is None or stage is None or out is None:
            raise ToolError("recording_not_active", "there is no active recording in this MCP session")
        try:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.send_signal(signal.SIGINT)
            async with asyncio.timeout(STOP_GRACE_S):
                stdout, _ = await process.communicate()
            if process.returncode != 0:
                raise ToolError("recording_failed", "the recorder did not finalize the video successfully")
            finished = json.loads(stdout)
            duration = finished.get("durationSeconds")
            if isinstance(duration, bool) or not isinstance(duration, int | float) or not math.isfinite(duration):
                raise ToolError("recording_failed", "the recorder returned no finite video duration")
            if duration <= 0:
                raise ToolError("recording_failed", "the recording contains no video duration")
            size = publish(Path(stage.name) / "bp-impl-video.mp4", out)
            if size == 0:
                out.unlink()
                raise ToolError("recording_failed", "the recorder produced an empty file")
            return {"status": "done", "path": str(out), "durationSeconds": duration, "bytes": size}
        except (TimeoutError, ValueError, AttributeError, OSError) as err:
            raise ToolError("recording_failed", "the recorder could not finalize a verified video") from err
        finally:
            await self._discard()

    async def close(self) -> None:
        if self._process is not None:
            try:
                await self.stop()
            except Exception:
                await self._discard()

    async def _discard(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.shield(process.wait())
        self._process = None
        if self._stage is not None:
            self._stage.cleanup()
        self._stage = None
        self._out = None
