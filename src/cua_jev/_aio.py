"""Small asyncio helpers and the time seams (`Sleep`, `Clock`) that waiting code takes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]


async def gather_settled[T](*aws: Awaitable[T]) -> list[T]:
    """Wait for every awaitable, then return their results in order, or raise the first failure
    by position. Nothing is cancelled when one of them fails, so nothing outlives the call.

    If the caller is cancelled, the awaitables still running are cancelled and awaited.
    """
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        if tasks:
            await asyncio.wait(tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    # Retrieve every exception first so none is reported as never retrieved.
    failures = [None if t.cancelled() else t.exception() for t in tasks]
    results: list[T] = []
    for t, exc in zip(tasks, failures, strict=True):
        if t.cancelled():
            raise asyncio.CancelledError
        if exc is not None:
            raise exc
        results.append(t.result())
    return results
