from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from cua_jev._json import JsonObject
from cua_jev._proc import DEFAULT_MAX_BYTES, Completed
from cua_jev.driver.sentinel import ActivationSentinel
from cua_jev.driver.types import ToolOk, ToolRefused, ToolResult
from cua_jev.errors import DriverError, ProcessError


class FakeRunner:
    def __init__(self, outputs: Mapping[tuple[str, ...], Completed | Exception] | None = None) -> None:
        self.outputs: dict[tuple[str, ...], Completed | Exception] = dict(outputs or {})
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[Path | None] = []

    async def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        check: bool = True,
        cwd: Path | None = None,
    ) -> Completed:
        key = tuple(argv)
        self.calls.append(key)
        self.cwds.append(cwd)
        out = self.outputs.get(key)
        if out is None:
            raise ProcessError(f"{argv[0] if argv else ''}: no fake output for {key!r}")
        if isinstance(out, Exception):
            raise out
        if check and out.returncode != 0:
            raise ProcessError(
                f"{argv[0]} exited with code {out.returncode}", returncode=out.returncode, stdout=out.stdout
            )
        return out


def fake_runner(outputs: Mapping[tuple[str, ...], Completed | Exception] | None = None) -> FakeRunner:
    return FakeRunner(outputs)


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_front() -> int | None:
    return None


type OnCall = Callable[[str, dict[str, object]], ToolResult | Awaitable[ToolResult]]


class FakeDriver:
    def __init__(self, on_call: OnCall | None = None) -> None:
        self.on_call = on_call
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.sentinel = ActivationSentinel(sample_front=no_front)
        self.closed = False

    @property
    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    async def call(self, tool: str, args: Mapping[str, object] | None = None) -> ToolResult:
        payload = dict(args) if args is not None else {}
        self.calls.append((tool, payload))
        if self.on_call is None:
            return ToolOk(data={"effect": "confirmed"}, text="", ms=1)
        answer = self.on_call(tool, payload)
        if isinstance(answer, ToolOk | ToolRefused):
            return answer
        assert inspect.isawaitable(answer)
        return await answer

    async def must(self, tool: str, args: Mapping[str, object] | None = None) -> JsonObject:
        r = await self.call(tool, args)
        if not r.ok:
            raise DriverError(tool, r.code, r.message)
        return r.data

    async def close(self) -> None:
        self.sentinel.stop()
        self.closed = True
