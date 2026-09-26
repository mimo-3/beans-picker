from __future__ import annotations

import asyncio

import pytest

from cua_jev._aio import gather_settled


async def _value(v: int, delay: float = 0.0) -> int:
    await asyncio.sleep(delay)
    return v


async def _fail(msg: str, delay: float = 0.0) -> int:
    await asyncio.sleep(delay)
    raise ValueError(msg)


async def test_results_keep_order_whatever_finishes_first() -> None:
    assert await gather_settled(_value(1, 0.03), _value(2, 0.0), _value(3, 0.01)) == [1, 2, 3]


async def test_empty() -> None:
    assert await gather_settled() == []


async def test_waits_for_every_awaitable_before_raising_the_first_failure_by_position() -> None:
    finished: list[str] = []

    async def slow() -> int:
        await asyncio.sleep(0.05)
        finished.append("slow")
        return 0

    with pytest.raises(ValueError, match="second") as info:
        await gather_settled(slow(), _fail("second", 0.0), _fail("third", 0.0))
    assert finished == ["slow"]
    assert str(info.value) == "second"


async def test_cancelling_the_caller_cancels_the_children() -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def hang() -> int:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 0

    task = asyncio.create_task(gather_settled(hang()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [True]


async def test_a_cancelled_child_cancels_the_call() -> None:
    async def cancel_self() -> int:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await gather_settled(_value(1), cancel_self())
