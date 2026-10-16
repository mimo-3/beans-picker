from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

import pytest

from beans_picker._proc import Completed
from beans_picker.driver.sentinel import MAX_ACTIVATIONS, ActivationSentinel, front_pid, watching
from beans_picker.driver.types import carried_foreground
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
    assert await s.sample() == ()
    assert front.calls == 1


async def test_a_watched_app_in_front_is_recorded() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    assert s.watching
    assert await s.sample() == ()
    front.pid = 5
    (a,) = await s.sample()
    assert (a.pid, a.during) == (5, "start")
    assert AT.match(a.at)
    assert s.activations == (a,)
    assert s.watching
    assert (await s.stop()).activations == (a,)


async def test_activations_name_the_call_running_or_just_finished() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    s.begin("click")
    s.end("click")
    front.pid = 5
    (a,) = await s.sample()
    assert a.during == "after click"
    await s.stop()

    front2 = Front(1)
    s2 = ActivationSentinel(front2, sleep=never)
    await s2.watch(5)
    s2.begin("press_key")
    front2.pid = 5
    (a2,) = await s2.sample()
    assert a2.during == "press_key"
    await s2.stop()


async def test_an_app_staying_in_front_is_one_activation() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    await s.watch(6)
    front.pid = 5
    first = await s.sample()
    s.begin("click")
    assert await s.sample() == first
    front.pid = 6
    # Another watched pid while one was in front: the same stretch in front, not a new activation.
    assert await s.sample() == first
    await s.stop()


async def test_each_activation_is_recorded_and_put_back() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    s.begin("press_key")
    front.pid = 5
    await s.sample()
    s.begin("hotkey")
    front.pid = 5
    seen = await s.sample()
    assert [(a.pid, a.during, a.restored) for a in seen] == [(5, "press_key", True), (5, "hotkey", True)]
    assert restore.pids == [1, 1]
    assert front.pid == 1
    assert s.watching
    assert (await s.stop()).activations == seen


async def test_an_app_left_in_front_is_recorded_again_after_it_went_behind() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    await s.sample()
    await s.sample()
    front.pid = 2
    await s.sample()
    front.pid = 5
    seen = await s.sample()
    assert [a.restored for a in seen] == [False, False]
    await s.stop()


async def test_the_record_is_bounded() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never, restore=Restore(front))
    await s.watch(5)
    for _ in range(MAX_ACTIVATIONS + 3):
        front.pid = 5
        await s.sample()
    assert len(s.activations) == MAX_ACTIVATIONS
    assert front.pid == 1
    log = await s.stop()
    assert log.truncated
    assert log.fields().get("foregroundTruncated") is True


async def test_stop_hands_over_the_activations_and_the_next_watch_starts_empty() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    seen = await s.sample()
    assert (await s.stop()).activations == seen
    assert not s.watching
    assert s.activations == ()
    assert await s.sample() == ()
    front.pid = 1
    await s.watch(5)
    assert s.activations == ()
    await s.stop()


async def test_watching_another_pid_while_watching_keeps_the_activations() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    seen = await s.sample()
    front.pid = 1
    await s.watch(6)
    assert s.activations == seen
    await s.stop()


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
    assert await first == ()
    (a,) = await second
    assert (a.pid, a.during) == (5, "click")
    assert calls == 3
    await s.stop()


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
    await s.stop()


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
    await until(lambda: bool(s.activations))
    assert s.activations[0].during == "start"
    await s.stop()
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
    await s.stop()
    for _ in range(3):
        await asyncio.sleep(0)
    assert s.activations == ()


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

    s = ActivationSentinel(sampler, sleep=never, stop_wait_s=0)
    await s.watch(5)
    s.begin("old-click")
    old = asyncio.ensure_future(s.sample())
    await blocked.wait()
    await s.stop()
    old.cancel()
    await asyncio.wait({old})
    answers.append(1)
    await s.watch(5)
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert s.activations == ()
    await s.stop()


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


async def test_the_app_in_front_before_is_put_back() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 5
    seen = await s.sample()
    assert [(a.pid, a.restored) for a in seen] == [(5, True)]
    assert await s.sample() == seen
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
    (v,) = await s.sample()
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
    (v,) = await s.sample()
    assert v.restored is False


async def test_nothing_is_put_back_without_a_restorer_or_a_known_front_app() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)
    await s.watch(5)
    front.pid = 5
    (v,) = await s.sample()
    assert v.restored is False
    await s.stop()

    # The app in front during an earlier watch is forgotten.
    restore = Restore(front)
    s.restore = restore
    front.pid = None
    await s.watch(5)
    front.pid = 5
    (v,) = await s.sample()
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
    await until(lambda: bool(s.activations))
    second = asyncio.create_task(s.sample())
    await asyncio.sleep(0)
    assert not second.done()
    gate.set()
    (a,) = await first
    assert a.restored is True
    assert await second == (a,)


async def test_the_app_put_back_is_the_last_one_seen_in_front() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 2
    assert await s.sample() == ()
    front.pid = 5
    (v,) = await s.sample()
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
    (v,) = await s.sample()
    assert v.restored is False
    assert restore.pids == []


async def test_a_foreground_delivery_is_not_interrupted_by_a_put_back() -> None:
    front = Front(1)
    restore = Restore(front)
    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    s.begin("press_key", foreground=True)
    front.pid = 5
    (a,) = await s.sample()
    await s.sample()
    assert (a.during, a.restored) == ("press_key", False)
    assert restore.pids == []
    s.end("press_key")
    (a,) = await s.sample()
    assert a.restored is True
    assert restore.pids == [1]
    await s.stop()


async def test_a_failed_put_back_is_tried_again_once_after_the_next_call() -> None:
    front = Front(1)
    restore = Restore(front, works=False)
    s = ActivationSentinel(front, sleep=never, restore=restore, restore_sleep=RecordingSleep())
    await s.watch(5)
    front.pid = 5
    (a,) = await s.sample()
    await s.sample()
    assert a.restored is False
    assert restore.pids == [1]
    restore.works = True
    s.begin("click")
    s.end("click")
    (a,) = await s.sample()
    assert a.restored is True
    assert restore.pids == [1, 1]
    await s.stop()


async def test_stop_lets_a_put_back_under_way_finish() -> None:
    front = Front(1)
    gate = asyncio.Event()

    async def restore(pid: int) -> bool:
        await gate.wait()
        front.pid = pid
        return True

    s = ActivationSentinel(front, sleep=never, restore=restore)
    await s.watch(5)
    front.pid = 5
    sampling = asyncio.create_task(s.sample())
    await until(lambda: bool(s.activations))
    stopping = asyncio.create_task(s.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    gate.set()
    log = await stopping
    assert [a.restored for a in log.activations] == [True]
    assert front.pid == 1
    await sampling


async def test_watching_carries_the_activations_onto_a_failure() -> None:
    front = Front(1)
    s = ActivationSentinel(front, sleep=never)

    async def body() -> None:
        async with watching(s, 5):
            front.pid = 5
            await s.sample()
            raise TimeoutError

    with pytest.raises(TimeoutError) as info:
        await body()
    assert [a.pid for a in carried_foreground(info.value).activations] == [5]
    assert not s.watching
