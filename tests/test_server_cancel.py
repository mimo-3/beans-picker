from __future__ import annotations

import asyncio
from pathlib import Path

from cua_jev.candidates.build import build_candidates
from cua_jev.driver.types import ToolOk, ToolResult
from cua_jev.observe.types import Snapshot
from cua_jev.paths import Paths
from cua_jev.server import create_server
from cua_jev.tools.args import TargetArgs
from cua_jev.tools.session import Session, Target
from tests.fakes import FakeDriver
from tests.helpers import snap_fixture
from tests.raw_mcp import raw_client
from tests.tool_fakes import shown


class ScriptedSession(Session):
    def __init__(self, driver: FakeDriver, snaps: list[Snapshot], cache: Path) -> None:
        async def connect() -> FakeDriver:
            return driver

        super().__init__(connect, paths=Paths(cache=cache))
        self._snaps = snaps
        self._taken = 0

    async def target(self, args: TargetArgs) -> Target:
        return Target(self._snaps[0].pid, self._snaps[0].window_id)

    async def snapshot(self, t: Target) -> Snapshot:
        self._taken += 1
        return self._snaps[self._taken % len(self._snaps)]


async def _unlocked() -> bool:
    return False


async def test_a_cancelled_act_finishes_before_the_next_call_acts(tmp_path: Path) -> None:
    calc = snap_fixture("calculator")
    seven = next(c for c in build_candidates(calc) if "Seven" in c.summary)
    release = asyncio.Event()
    entered = asyncio.Event()
    finished: list[str] = []

    async def slow(tool: str, args: dict[str, object]) -> ToolResult:
        if tool == "click" and not release.is_set():
            entered.set()
            await release.wait()
            finished.append("first click done")
        return ToolOk(data={"effect": "confirmed"}, text="", ms=1)

    driver = FakeDriver(slow)
    session = ScriptedSession(driver, [calc, shown(calc, "7")], tmp_path)
    server = create_server(session, screen_locked=_unlocked)
    args = {"pid": calc.pid, "instruction": "7", "candidateId": seven.id}
    async with raw_client(server) as client:
        await client.initialize()
        first = await client.send_call("act", args)
        await asyncio.wait_for(entered.wait(), 2)
        await client.notify("notifications/cancelled", {"requestId": first, "reason": "user"})
        second = await client.send_call("act", args)
        await asyncio.sleep(0.05)
        assert driver.tools == ["click"]
        release.set()
        result = await asyncio.wait_for(client.result(second), 2)
        assert not client.answered(first)
    assert finished == ["first click done"]
    assert driver.tools == ["click", "click"]
    assert result["isError"] is False
    await session.close()
