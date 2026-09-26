"""Ranking by Jev."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from cua_jev._aio import gather_settled
from cua_jev._json import JsonObject, JsonValue
from cua_jev.candidates.prune import MAX_OPTIONS
from cua_jev.config import THRESHOLDS
from cua_jev.errors import JevBadResponse
from cua_jev.jev.client import AskResult, Question
from cua_jev.jev.questions import Options

if TYPE_CHECKING:
    from cua_jev.jev.state import JevState

type GateRule = Literal["strict", "agree"]


class Asker(Protocol):
    """Anything that answers questions about a state: the Jev client, or a stand-in."""

    async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult: ...


@dataclass(frozen=True, slots=True)
class Ranked[T]:
    item: T
    p: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Ranking[T]:
    """Every option of the final round, most likely first, and the mass on `none`."""

    ranked: list[Ranked[T]]
    p_none: float
    forced_confidence: float = 0.0
    forced: T | None = None
    """The forced question's pick (no `none` offered), when it was asked and picked an option."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Pick[T]:
    item: T
    p: float
    rule: GateRule


@dataclass(frozen=True, slots=True, kw_only=True)
class Ambiguous[T]:
    shortlist: list[Ranked[T]]
    p_none: float


@dataclass(frozen=True, slots=True, kw_only=True)
class NotFound:
    p_none: float


type Gate[T] = Pick[T] | Ambiguous[T] | NotFound


@dataclass(frozen=True, slots=True, kw_only=True)
class RankSpec[T]:
    """How items become options and which questions are asked about them."""

    describe: Callable[[T], JsonObject]
    question: Callable[[Options], Question]
    forced: Callable[[Options], Question] | None = None
    """Asked with the last round when set (actions): a second opinion without `none`."""
    order: Callable[[Sequence[T]], Sequence[T]] | None = None
    """Orders the options before sharding (best first)."""


_RUNOFF_TOP = 3
_RUNOFF_MIN_P = 0.02


async def rank[T](jev: Asker, state: JevState, items: Sequence[T], spec: RankSpec[T]) -> Ranking[T]:
    """One question when the items fit; otherwise every shard at once, then a runoff of their leaders."""
    ordered = spec.order(items) if spec.order is not None else items
    if len(ordered) <= MAX_OPTIONS:
        return await _round(jev, state, ordered, spec, final=True)
    shards = [ordered[i : i + MAX_OPTIONS] for i in range(0, len(ordered), MAX_OPTIONS)]
    results = await gather_settled(*(_round(jev, state, s, spec, final=False) for s in shards))
    finalists = [x.item for r in results for x in r.ranked[:_RUNOFF_TOP] if x.p >= _RUNOFF_MIN_P]
    if not finalists:
        return Ranking(ranked=[], p_none=min(r.p_none for r in results))
    return await _round(jev, state, finalists, spec, final=True)


def _number(v: JsonValue | None) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0


def _probabilities(answer: JsonValue | None) -> Mapping[str, JsonValue]:
    probs = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probs, dict):
        raise JevBadResponse("the ranking answer has no probabilities")
    return probs


async def _round[T](jev: Asker, state: JevState, items: Sequence[T], spec: RankSpec[T], *, final: bool) -> Ranking[T]:
    ids = {f"o{i}": item for i, item in enumerate(items)}
    options = {oid: spec.describe(item) for oid, item in ids.items()}
    questions: dict[str, Question] = {"pick": spec.question(options)}
    if final and spec.forced is not None:
        questions["forced"] = spec.forced(options)
    answers = (await jev.ask(state, questions)).answers
    probs = _probabilities(answers.get("pick"))
    ranked = sorted((Ranked(item, _number(probs.get(oid))) for oid, item in ids.items()), key=lambda x: -x.p)
    forced = answers.get("forced")
    if not isinstance(forced, dict):
        return Ranking(ranked=ranked, p_none=_number(probs.get("none")))
    choice = forced.get("choice")
    return Ranking(
        ranked=ranked,
        p_none=_number(probs.get("none")),
        forced_confidence=_number(forced.get("confidence")),
        forced=ids.get(choice) if isinstance(choice, str) else None,
    )


def gate[T](r: Ranking[T], shortlist_size: int = 5) -> Gate[T]:
    """Whether the leader is clear enough to act on."""
    t = THRESHOLDS
    top = r.ranked[0] if r.ranked else None
    second = r.ranked[1] if len(r.ranked) > 1 else None
    if top is None or (r.p_none >= t.not_found and r.p_none > top.p):
        return NotFound(p_none=r.p_none)
    if top.p >= t.strict and r.p_none <= t.max_none:
        return Pick(item=top.item, p=top.p, rule="strict")
    lead = top.p >= t.lead_ratio * (second.p if second is not None else 0)
    if r.forced is not None and r.forced is top.item and top.p >= t.agree and lead and r.p_none <= t.max_none:
        return Pick(item=top.item, p=top.p, rule="agree")
    shortlist = [x for x in r.ranked if x.p >= t.shortlist_min][:shortlist_size]
    if not shortlist:
        return NotFound(p_none=r.p_none)
    return Ambiguous(shortlist=shortlist, p_none=r.p_none)
