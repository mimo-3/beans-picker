from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from cua_jev._json import JsonObject, JsonValue
from cua_jev.candidates.prune import MAX_OPTIONS
from cua_jev.jev.client import AskResult, Question
from cua_jev.jev.questions import action_question, action_question_forced
from cua_jev.jev.rank import Ambiguous, NotFound, Pick, Ranked, Ranking, RankSpec, gate, rank
from cua_jev.jev.state import JevState
from tests.tool_fakes import fake_jev


def ranking(ps: list[float], p_none: float, forced: int | None = None) -> Ranking[str]:
    ranked = [Ranked(f"x{i}", p) for i, p in enumerate(ps)]
    return Ranking(
        ranked=ranked,
        p_none=p_none,
        forced_confidence=0.6,
        forced=ranked[forced].item if forced is not None else None,
    )


def test_gate_takes_a_sure_pick_on_its_own() -> None:
    g = gate(ranking([0.85, 0.1], 0.05))
    assert isinstance(g, Pick)
    assert (g.item, g.rule) == ("x0", "strict")


def test_gate_takes_a_weaker_pick_only_when_the_forced_question_backs_it_and_it_leads_clearly() -> None:
    g = gate(ranking([0.6, 0.2], 0.1, 0))
    assert isinstance(g, Pick)
    assert g.rule == "agree"
    assert isinstance(gate(ranking([0.6, 0.2], 0.1, 1)), Ambiguous)
    assert isinstance(gate(ranking([0.5, 0.4], 0.1, 0)), Ambiguous)


def test_gate_returns_not_found_when_none_dominates() -> None:
    assert isinstance(gate(ranking([0.2, 0.1], 0.7)), NotFound)


def test_gate_lists_the_likely_ones_when_the_leader_is_unclear() -> None:
    g = gate(ranking([0.45, 0.4, 0.01], 0.1, 0))
    assert isinstance(g, Ambiguous)
    assert [x.item for x in g.shortlist] == ["x0", "x1"]


def test_gate_with_nothing_ranked_or_nothing_above_the_floor_is_not_found() -> None:
    assert gate(Ranking[str](ranked=[], p_none=0.1)) == NotFound(p_none=0.1)
    assert gate(ranking([0.02, 0.01], 0.4)) == NotFound(p_none=0.4)


def test_gate_shortlist_is_capped() -> None:
    g = gate(ranking([0.2] * 8, 0.1), shortlist_size=3)
    assert isinstance(g, Ambiguous)
    assert len(g.shortlist) == 3


def _spec() -> RankSpec[str]:
    def describe(s: str) -> JsonObject:
        return {"name": s}

    return RankSpec(describe=describe, question=action_question, forced=action_question_forced)


async def test_rank_asks_once_when_the_options_fit_one_question() -> None:
    jev = fake_jev("b")
    r = await rank(jev, {"instruction": "b"}, ["a", "b", "c"], _spec())
    assert r.ranked[0].item == "b"
    assert r.forced == "b"
    assert jev.calls == 1


async def test_rank_shards_a_long_list_and_runs_off_the_leaders() -> None:
    items = [f"item-{i}" for i in range(MAX_OPTIONS * 2 + 5)]
    jev = fake_jev("item-100")
    r = await rank(jev, {"instruction": "x"}, items, _spec())
    assert r.ranked[0].item == "item-100"
    assert jev.calls == 4


class _Scripted:
    def __init__(self, delays: list[float], fail_at: int | None = None) -> None:
        self.delays = delays
        self.fail_at = fail_at
        self.finished: list[int] = []
        self.calls = 0

    async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult:
        n = self.calls
        self.calls += 1
        await asyncio.sleep(self.delays[n] if n < len(self.delays) else 0)
        self.finished.append(n)
        if n == self.fail_at:
            raise RuntimeError(f"shard {n} failed")
        answers: dict[str, JsonValue] = {}
        for qid, q in questions.items():
            keys = [str(k) for k in getattr(q, "criteria", {})]
            probs: dict[str, JsonValue] = {k: (0.5 if k == "o0" else 0.0) for k in keys}
            answers[qid] = {"type": "choice", "choice": "o0", "confidence": 0.5, "probabilities": probs}
        return AskResult(answers=answers, ms=1, input_tokens=1)


async def test_rank_keeps_shard_order_whatever_order_the_answers_come_in() -> None:
    items = [f"i{i}" for i in range(MAX_OPTIONS + 1)]
    jev = _Scripted([0.05, 0])
    r = await rank(jev, {}, items, _spec())
    assert jev.finished[:2] == [1, 0]
    assert [x.item for x in r.ranked] == ["i0", f"i{MAX_OPTIONS}"]


async def test_rank_reports_a_failing_shard_only_after_every_shard_has_finished() -> None:
    items = [f"i{i}" for i in range(MAX_OPTIONS + 1)]
    jev = _Scripted([0, 0.05], fail_at=0)
    with pytest.raises(RuntimeError, match="shard 0 failed"):
        await rank(jev, {}, items, _spec())
    assert sorted(jev.finished) == [0, 1]


async def test_rank_without_finalists_returns_the_smallest_none() -> None:
    class Nothing:
        calls = 0

        async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult:
            self.calls += 1
            answers: dict[str, JsonValue] = {}
            for qid, q in questions.items():
                keys = [str(k) for k in getattr(q, "criteria", {})]
                probs: dict[str, JsonValue] = {k: 0.01 for k in keys}
                probs["none"] = 0.9 if self.calls == 1 else 0.7
                answers[qid] = {"type": "choice", "choice": "none", "confidence": 0.9, "probabilities": probs}
            return AskResult(answers=answers, ms=1, input_tokens=1)

    jev = Nothing()
    r = await rank(jev, {}, [f"i{i}" for i in range(MAX_OPTIONS + 1)], _spec())
    assert r == Ranking(ranked=[], p_none=0.7)
    assert jev.calls == 2


async def test_rank_orders_before_sharding_and_asks_forced_only_in_the_last_round() -> None:
    seen: list[list[str]] = []

    class Recording:
        async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult:
            seen.append(list(questions))
            answers: dict[str, JsonValue] = {}
            for qid, q in questions.items():
                keys = [str(k) for k in getattr(q, "criteria", {})]
                answers[qid] = {"type": "choice", "choice": "o0", "confidence": 1, "probabilities": {"o0": 1.0}}
                assert keys
            return AskResult(answers=answers, ms=1, input_tokens=1)

    def describe(s: str) -> JsonObject:
        return {"name": s}

    spec = RankSpec(
        describe=describe,
        question=action_question,
        forced=action_question_forced,
        order=lambda xs: sorted(xs, reverse=True),
    )
    r = await rank(Recording(), {}, ["a", "b", "c"], spec)
    assert r.ranked[0].item == "c"
    assert r.forced == "c"
    assert r.forced_confidence == 1
    assert seen == [["pick", "forced"]]
