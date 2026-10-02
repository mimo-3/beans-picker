from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

import pytest

from beans_picker._proc import Completed
from beans_picker.driver.sentinel import ActivationSentinel, front_pid
from tests.fakes import RecordingSleep, fake_runner

AT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\Z")


class Front:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid
        self.calls = 0

    async def __call__(self) -> int | None:
        self.calls += 1
        return self.pid


async def until(done: Callable[[], bool]) -> None:
    for _ in range(1000):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


async def hold() -> None:
    await asyncio.Event().wait()


async def never(_: float) -> None:
    await hold()


async def test_an_app_already_in_front_is_not_watched() -> None:
    front = Front(5)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    assert not s.watching
    assert await s.sample() is None
    assert front.calls == 1


async def test_the_first_time_a_watched_app_is_in_front_is_a_violation() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    assert s.watching
    assert await s.sample() is None
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert (v.pid, v.during) == (5, "start")
    assert AT.match(v.at)
    assert s.violation is v
    s.stop()


async def test_violations_name_the_call_running_or_just_finished() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    s.begin("click")
    s.end("click")
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.during == "after click"
    s.stop()

    front2 = Front(1)
    s2 = ActivationSentinel(front2, sleep=never)
    await s2.watch(5)
    s2.begin("press_key")
    front2.pid = 5
    v2 = await s2.sample()
    assert v2 is not None
    assert v2.during == "press_key"
    s2.stop()


async def test_the_first_violation_is_kept() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    await s.watch(6)
    front.pid = 5
    first = await s.sample()
    front.pid = 6
    s.begin("click")
    calls = front.calls
    assert await s.sample() is first
    assert front.calls == calls
    s.stop()


async def test_stop_keeps_the_violation_and_the_next_watch_clears_it() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    s.stop()
    assert not s.watching
    assert s.violation is v
    assert await s.sample() is v
    front.pid = 5
    await s.watch(5)
    assert s.violation is None
    assert not s.watching


async def test_watching_another_pid_while_watching_keeps_the_violation() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    front.pid = 1
    await s.watch(6)
    assert s.violation is v
    s.stop()


async def test_a_sample_in_flight_is_awaited_before_a_fresh_one() -> None:
    release = asyncio.Event()
    calls = 0

    async def slow_front() -> int | None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the first sample after watch() hangs until released
            await release.wait()
        return 5 if calls == 3 else 1

    s = ActivationSentinel(slow_front, sleep=never)
    await s.watch(5)
    first = asyncio.create_task(s.sample())
    await asyncio.sleep(0)
    second = asyncio.create_task(s.sample())
    await asyncio.sleep(0)
    assert calls == 2
    s.begin("click")
    release.set()
    assert await first is None
    v = await second
    assert v is not None
    assert (v.pid, v.during) == (5, "click")
    assert calls == 3
    s.stop()


async def test_a_failing_sampler_fails_the_sample() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)

    async def broken() -> int | None:
        raise RuntimeError("no lsappinfo")

    s._sample_front = broken
    with pytest.raises(RuntimeError, match="no lsappinfo"):
        await s.sample()
    assert s._inflight is None
    s.stop()


async def test_the_ticker_samples_periodically_until_stopped() -> None:
    front = Front(1)
    delays: list[float] = []

    async def tick(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    s = ActivationSentinel(front, interval_ms=80, sleep=tick)
    await s.watch(5)
    await until(lambda: front.calls >= 4)
    front.pid = 5
    await until(lambda: s.violation is not None)
    assert s.violation is not None
    assert s.violation.during == "start"
    s.stop()
    assert s._ticker is None
    await asyncio.sleep(0)
    calls = front.calls
    for _ in range(5):
        await asyncio.sleep(0)
    assert front.calls == calls
    assert set(delays) == {0.08}


async def test_periodic_failures_are_swallowed() -> None:
    calls = 0

    async def flaky() -> int | None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("flaky")
        return 1

    async def tick(_: float) -> None:
        await asyncio.sleep(0)

    s = ActivationSentinel(flaky, sleep=tick)
    await s.watch(5)
    await until(lambda: calls >= 5)
    s.stop()
    for _ in range(3):
        await asyncio.sleep(0)
    assert s.violation is None


async def test_front_pid_reads_launch_services() -> None:
    runner = fake_runner(
        {
            ("lsappinfo", "front"): Completed(" ASN:0x0-0x1d01d:\n", 0),
            ("lsappinfo", "info", "-only", "pid", "ASN:0x0-0x1d01d:"): Completed('"pid" = 812\n', 0),
        }
    )
    assert await front_pid(runner=runner) == 812
    assert runner.calls == [("lsappinfo", "front"), ("lsappinfo", "info", "-only", "pid", "ASN:0x0-0x1d01d:")]


async def test_front_pid_is_none_without_a_pid_line_or_on_failure() -> None:
    no_line = fake_runner(
        {
            ("lsappinfo", "front"): Completed("ASN:1\n", 0),
            ("lsappinfo", "info", "-only", "pid", "ASN:1"): Completed("nothing\n", 0),
        }
    )
    assert await front_pid(runner=no_line) is None
    assert await front_pid(runner=fake_runner({("lsappinfo", "front"): Completed("", 1)})) is None


async def test_a_probe_from_a_stopped_watch_does_not_reach_the_next_one() -> None:
    answers: list[int] = [1]
    release = asyncio.Event()
    blocked = asyncio.Event()

    async def sampler() -> int | None:
        if not answers:
            blocked.set()
            await release.wait()
            return 5
        return answers.pop()

    s = ActivationSentinel(sampler, sleep=never)
    await s.watch(5)
    s.begin("old-click")
    old = asyncio.ensure_future(s.sample())
    await blocked.wait()
    s.stop()
    old.cancel()
    await asyncio.wait({old})
    answers.append(1)
    await s.watch(5)
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert s.violation is None
    s.stop()


class Restore:
    """Puts `pid` in front of `front` when it works."""

    def __init__(self, front: Front, *, works: bool = True, moves: bool = True) -> None:
        self.front = front
        self.works = works
        self.moves = moves
        self.pids: list[int] = []

    async def __call__(self, pid: int) -> bool:
        self.pids.append(pid)
        if self.works and self.moves:
            self.front.pid = pid
        return self.works


async def test_the_app_in_front_before_is_put_back_once() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert (v.pid, v.restored) == (5, True)
    assert await s.sample() is v
    assert restore.pids == [1]
    assert front.pid == 1


@pytest.mark.parametrize(("works", "moves"), [(False, False), (True, False)])
async def test_an_app_that_is_still_behind_is_not_reported_as_put_back(works: bool, moves: bool) -> None:
    front = Front(1)
    restore = Restore(front, works=works, moves=moves)
    waits = RecordingSleep()
    s = ActivationSentinel(front, sleep=never, restore=restore, restore_sleep=waits)
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.restored is False
    assert restore.pids == [1]
    assert waits.delays == ([0.05] * 6 if works else [])


async def test_a_restore_that_raises_is_not_put_back() -> None:
    front = Front(1)

    async def restore(pid: int) -> bool:
        raise OSError("no helper")

    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.restored is False


async def test_nothing_is_put_back_without_a_restorer_or_a_known_front_app() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.restored is False
    s.stop()

    # The app in front during an earlier watch is forgotten.
    restore = Restore(front)
    s.restore = restore
    front.pid = None
    await s.watch(5)
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.restored is False
    assert restore.pids == []


async def test_a_sample_during_a_restore_waits_for_how_it_ended() -> None:
    front = Front(1)
    gate = asyncio.Event()

    async def restore(pid: int) -> bool:
        await gate.wait()
        front.pid = pid
        return True

    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 5
    first = asyncio.create_task(s.sample())
    await until(lambda: s.violation is not None)
    second = asyncio.create_task(s.sample())
    await asyncio.sleep(0)
    assert not second.done()
    gate.set()
    a, b = await first, await second
    assert a is not None
    assert a.restored is True
    assert b is a


async def test_the_app_put_back_is_the_last_one_seen_in_front() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 2
    assert await s.sample() is None
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.restored is True
    assert restore.pids == [2]


async def test_a_watched_app_is_never_brought_forward() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 9
    await s.watch(1)
    front.pid = 1
    await s.watch(9)
    front.pid = 5
    v = await s.sample()
    assert v is not None
    assert v.restored is False
    assert restore.pids == []
