"""Activation guard."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

from cua_jev._aio import Sleep
from cua_jev._proc import Runner, run
from cua_jev._text import DIGIT, WS, trim
from cua_jev.driver.types import Activation

type FrontSampler = Callable[[], Awaitable[int | None]]

_log = logging.getLogger(__name__)

PID_LINE: Final = re.compile(f'"pid"{WS}*={WS}*({DIGIT}+)')


async def front_pid(*, runner: Runner = run) -> int | None:
    """The pid of the frontmost app, from LaunchServices; None when it cannot be read."""
    try:
        asn = trim((await runner(("lsappinfo", "front"))).stdout)
        out = await runner(("lsappinfo", "info", "-only", "pid", asn))
    except Exception:
        return None
    m = PID_LINE.search(out.stdout)
    return int(m.group(1)) if m else None


def _now_utc() -> str:
    now = datetime.now(UTC)
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


class ActivationSentinel:
    """Watches pids and records the first time one of them is seen in front."""

    def __init__(
        self,
        sample_front: FrontSampler = front_pid,
        interval_ms: int = 80,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._sample_front = sample_front
        self._interval_s = interval_ms / 1000
        self._sleep = sleep
        self._pids: set[int] = set()
        self._ticker: asyncio.Task[None] | None = None
        self._periodic: set[asyncio.Task[Activation | None]] = set()
        self._probes: set[asyncio.Task[None]] = set()
        self._inflight: asyncio.Task[None] | None = None
        # Bumped by `stop`, so a probe that started under an earlier watch never records anything.
        self._generation = 0
        self._current = "start"
        self.violation: Activation | None = None

    @property
    def watching(self) -> bool:
        return bool(self._pids)

    async def watch(self, pid: int) -> None:
        """Start watching `pid`."""
        if not self._pids:
            self.violation = None
        if await self._sample_front() == pid:
            return
        self._pids.add(pid)
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick())

    def stop(self) -> None:
        """Stop watching every pid."""
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        for task in self._periodic:
            task.cancel()
        for probe in self._probes:
            probe.cancel()
        self._inflight = None
        self._generation += 1
        self._pids.clear()

    def begin(self, tool: str) -> None:
        """Name the driver call now running, so a violation says which route caused it."""
        self._current = tool

    def end(self, tool: str) -> None:
        self._current = f"after {tool}"

    async def sample(self) -> Activation | None:
        """Take one sample right now (on top of the periodic ones) and return the violation, if any."""
        if not self._pids or self.violation is not None:
            return self.violation
        if self._inflight is not None:
            await asyncio.wait({self._inflight})
            if not self._pids or self.violation is not None:
                return self.violation
        probe = asyncio.create_task(self._probe(self._current, self._generation))
        self._probes.add(probe)
        probe.add_done_callback(self._probe_done)
        self._inflight = probe
        # A caller that goes away does not cancel the probe; `stop` does.
        await asyncio.wait({probe})
        if not probe.cancelled() and (err := probe.exception()) is not None:
            raise err
        return self.violation

    def _probe_done(self, probe: asyncio.Task[None]) -> None:
        self._probes.discard(probe)
        if self._inflight is probe:
            self._inflight = None
        if not probe.cancelled():
            probe.exception()  # retrieved here too, for a probe whose caller went away

    async def _probe(self, during: str, generation: int) -> None:
        front = await self._sample_front()
        if generation != self._generation:
            return
        if front is not None and front in self._pids and self.violation is None:
            self.violation = Activation(pid=front, during=during, at=_now_utc())
            _log.debug("pid %d came to the front during %s", front, during)

    async def _tick(self) -> None:
        while True:
            await self._sleep(self._interval_s)
            task = asyncio.create_task(self.sample())
            self._periodic.add(task)
            task.add_done_callback(self._periodic_done)

    def _periodic_done(self, task: asyncio.Task[Activation | None]) -> None:
        self._periodic.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            _log.debug("periodic front sample failed: %s", err)
