"""observe: the window's candidate actions, optionally ranked by Jev for an instruction."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from cua_jev._numbers import round3
from cua_jev.candidates.build import BuildOptions, build_candidates
from cua_jev.candidates.describe import describe
from cua_jev.candidates.prune import in_shard_order
from cua_jev.jev.client import JevUsageOut
from cua_jev.jev.questions import action_question
from cua_jev.jev.rank import RankSpec, rank
from cua_jev.jev.state import Field, build_state
from cua_jev.tools.args import ObserveArgs
from cua_jev.tools.present import ShownCandidate, ShownWindow, show_candidate, show_screen, show_window
from cua_jev.tools.session import ToolSession

DEFAULT_LIMIT = 80
DEFAULT_RANKED_LIMIT = 10


class ObserveOutput(TypedDict):
    window: ShownWindow
    screenText: list[str]
    fields: list[Field]
    total: int
    pNone: NotRequired[float]
    candidates: list[ShownCandidate]
    jev: NotRequired[JevUsageOut]


async def observe_tool(session: ToolSession, args: ObserveArgs) -> ObserveOutput:
    t = await session.target(args)
    snap = await session.snapshot(t)
    learned = session.menu_keys.table_for(t.pid)
    instruction = args.get("instruction")
    cands = build_candidates(snap, BuildOptions(instruction=instruction, list_text_kinds=True, learned=learned))
    screen = show_screen(snap)
    if instruction is None:
        limit = args.get("limit", DEFAULT_LIMIT)
        return {
            "window": show_window(snap),
            "screenText": screen["screenText"],
            "fields": screen["fields"],
            "total": len(cands),
            "candidates": [show_candidate(c) for c in cands[:limit]],
        }
    jev = session.jev()
    r = await rank(
        jev,
        build_state(instruction, snap),
        cands,
        RankSpec(describe=lambda c: describe(c, snap, learned), question=action_question, order=in_shard_order),
    )
    limit = args.get("limit", DEFAULT_RANKED_LIMIT)
    return {
        "window": show_window(snap),
        "screenText": screen["screenText"],
        "fields": screen["fields"],
        "total": len(cands),
        "pNone": round3(r.p_none),
        "candidates": [show_candidate(x.item, x.p) for x in r.ranked[:limit]],
        "jev": jev.take_usage().as_output(),
    }
