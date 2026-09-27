from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from beans_picker._json import JsonValue, dumps
from beans_picker.driver.mcp import Driver
from beans_picker.jev.client import AskResult, JevUsage, Question
from beans_picker.jev.state import JevState
from beans_picker.menus.menukeys import MenuKeys
from beans_picker.observe.helpers import Helpers
from beans_picker.observe.types import Snapshot, TextNode
from beans_picker.paths import Paths
from beans_picker.tools.args import TargetArgs
from beans_picker.tools.session import Target
from tests.fakes import FakeDriver


def _criteria(q: Question) -> dict[str, JsonValue]:
    criteria = getattr(q, "criteria", None)
    assert isinstance(criteria, Mapping)
    return {str(k): v for k, v in criteria.items()}


class JevPicking:
    def __init__(self, want: str | None, rival: str | None = None) -> None:
        self.want = want
        self.rival = rival
        self.calls = 0
        self.states: list[JevState] = []
        self.closed = False

    async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult:
        self.calls += 1
        self.states.append(state)
        answers: dict[str, JsonValue] = {}
        for qid, q in questions.items():
            criteria = _criteria(q)
            keys = list(criteria)

            def find(w: str | None, criteria: dict[str, JsonValue] = criteria, keys: list[str] = keys) -> str:
                return next((k for k in keys if w and w in dumps(criteria[k])), "none")

            hit, other = find(self.want), find(self.rival)
            probabilities: dict[str, JsonValue] = {}
            for k in keys:
                if self.rival is not None:
                    probabilities[k] = 0.45 if k in (hit, other) else 0.1 / len(keys)
                else:
                    probabilities[k] = 0.95 if k == hit else 0.05 / len(keys)
            choice = hit if hit in probabilities else keys[0]
            answers[qid] = {"type": "choice", "choice": choice, "confidence": 0.95, "probabilities": probabilities}
        return AskResult(answers=answers, ms=1, input_tokens=10)

    def take_usage(self) -> JevUsage:
        return JevUsage(calls=1, input_tokens=10, ms=1)

    async def aclose(self) -> None:
        self.closed = True


def jev_picking(want: str | None, rival: str | None = None) -> JevPicking:
    return JevPicking(want, rival)


class FakeJev:
    def __init__(self, want: str) -> None:
        self.want = want
        self.calls = 0

    async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult:
        self.calls += 1
        answers: dict[str, JsonValue] = {}
        for qid, q in questions.items():
            criteria = _criteria(q)
            hit = next((k for k, v in criteria.items() if f'"{self.want}"' in dumps(v)), "none")
            probabilities: dict[str, JsonValue] = {k: 0.9 if k == hit else 0.1 / len(criteria) for k in criteria}
            answers[qid] = {"type": "choice", "choice": hit, "confidence": 0.9, "probabilities": probabilities}
        return AskResult(answers=answers, ms=1, input_tokens=1)


def fake_jev(want: str) -> FakeJev:
    return FakeJev(want)


def shown(s: Snapshot, text: str) -> Snapshot:
    c = copy.deepcopy(s)
    c.texts = [TextNode(role="AXStaticText", value=text, raw=text, depth=5)]
    c.signature = f"shows:{text}"
    return c


class FakeSession:
    def __init__(
        self,
        snaps: Sequence[Snapshot] | Callable[[], Snapshot],
        jev: JevPicking | None = None,
        *,
        driver: FakeDriver | None = None,
        cache: Path | None = None,
        target: Target | None = None,
    ) -> None:
        self._snaps = snaps
        self._jev = jev if jev is not None else jev_picking(None)
        self.fake_driver = driver if driver is not None else FakeDriver()
        self._paths = Paths(cache=cache if cache is not None else Path("/nonexistent/beans-picker-test-cache"))
        self._menu_keys = MenuKeys(Helpers(self._paths), self._paths)
        self._target = target
        self.types_into_web_fields: set[int] = set()
        self.snapshots = 0
        self.jev_calls = 0

    @property
    def paths(self) -> Paths:
        return self._paths

    @property
    def menu_keys(self) -> MenuKeys:
        return self._menu_keys

    @property
    def calls(self) -> list[tuple[str, dict[str, object]]]:
        return self.fake_driver.calls

    async def target(self, args: TargetArgs) -> Target:
        if self._target is not None:
            return self._target
        first = self._snaps() if callable(self._snaps) else self._snaps[0]
        return Target(first.pid, first.window_id)

    async def snapshot(self, t: Target) -> Snapshot:
        i = self.snapshots
        self.snapshots += 1
        if callable(self._snaps):
            return self._snaps()
        return self._snaps[min(i, len(self._snaps) - 1)]

    async def driver(self) -> Driver:
        return self.fake_driver

    def jev(self) -> JevPicking:
        self.jev_calls += 1
        return self._jev
