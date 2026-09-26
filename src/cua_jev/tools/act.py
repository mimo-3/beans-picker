"""act: one operation, or a short sequence of them, per call.

The target is the caller's candidateId, or Jev's pick for the instruction; each effect is judged on
fresh snapshots, a fixed number of them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Literal, NotRequired, TypedDict, cast

from cua_jev import config
from cua_jev._numbers import fixed, round3
from cua_jev.act.execute import Executor
from cua_jev.candidates.build import BuildOptions, build_candidates
from cua_jev.candidates.describe import describe
from cua_jev.candidates.prune import in_shard_order
from cua_jev.candidates.types import TEXT_KINDS, ActionCandidate
from cua_jev.driver.mcp import Driver
from cua_jev.errors import ForegroundViolation
from cua_jev.jev.client import JevUsageOut
from cua_jev.jev.questions import action_question, action_question_forced
from cua_jev.jev.rank import Ambiguous, NotFound, Pick, RankSpec, gate, rank
from cua_jev.jev.state import Change, bare_role, build_state, change_of
from cua_jev.observe.snapshot import in_web_area
from cua_jev.observe.types import Snapshot
from cua_jev.observe.visual import Shot, capture_window, region_change
from cua_jev.tools.args import ActArgs, Modifier, Step
from cua_jev.tools.present import ShownCandidate, ShownWindow, show_candidate, show_window
from cua_jev.tools.session import Target, ToolSession
from cua_jev.verify.effect import EffectVerdict, VerifyEffect, verify_effect

type ActStatus = Literal[
    "done", "unverified", "no_effect", "mismatch", "ambiguous", "needs_confirmation", "not_found", "failed"
]

CARRY_ON: Final[frozenset[ActStatus]] = frozenset({"done", "unverified"})
"""A sequence carries on past these; any other status stops it."""
CLICK_KINDS: Final = frozenset({"click", "toggle"})
"""Kinds a click with modifier keys applies to."""
STATUS: Final[dict[VerifyEffect, ActStatus]] = {
    "ok": "done",
    "unverified": "unverified",
    "wrong": "mismatch",
    "none": "no_effect",
}
_REFUSED: Final = "cua-driver refused the action"


class ShownAction(ShownCandidate):
    route: NotRequired[list[str]]


class Verification(TypedDict):
    snapshots: int
    exact: NotRequired[bool]


class PickOut(TypedDict):
    p: float
    rule: str


class ActOutput(TypedDict):
    """One step's result; a sequence has `steps` (each without `window`) and `skipped`."""

    status: ActStatus
    code: NotRequired[str]
    message: str
    window: NotRequired[ShownWindow]
    action: NotRequired[ShownAction]
    verification: NotRequired[Verification]
    change: NotRequired[Change]
    candidates: NotRequired[list[ShownCandidate]]
    pick: NotRequired[PickOut]
    jev: NotRequired[JevUsageOut]
    steps: NotRequired[list[ActOutput]]
    skipped: NotRequired[int]


async def act_tool(session: ToolSession, args: ActArgs) -> ActOutput:
    t = await session.target(args)
    first, after = await _act_once(session, t, args, await session.snapshot(t))
    then = args.get("then")
    if not then:
        return first
    outs = [first]
    for step in then:
        if outs[-1]["status"] not in CARRY_ON:
            break
        # The previous step's last snapshot already shows its effect: it is this step's "before".
        out, after = await _act_once(session, t, step, after)
        outs.append(out)
    last = outs[-1]
    total = len(then) + 1
    skipped = total - len(outs)
    message = (
        f"stopped at step {len(outs)} of {total} ({last['status']}); the rest were not run"
        if skipped
        else f"ran all {total} steps"
    )
    result: ActOutput = {"status": last["status"], "message": message}
    if "window" in last:
        result["window"] = last["window"]
    result["steps"] = [_without_window(o) for o in outs]
    if skipped:
        result["skipped"] = skipped
    return result


def _without_window(out: ActOutput) -> ActOutput:
    rest = out.copy()
    rest.pop("window", None)
    return rest


class _Seen:
    """The window as the step left it: `before` until a snapshot was taken after the action."""

    __slots__ = ("after",)

    def __init__(self, before: Snapshot) -> None:
        self.after = before


async def _act_once(session: ToolSession, t: Target, args: Step, before: Snapshot) -> tuple[ActOutput, Snapshot]:
    seen = _Seen(before)
    out = await _step(session, t, args, before, seen)
    return out, seen.after


def _shown_action(c: ActionCandidate) -> ShownAction:
    """The candidate as shown in `action`; the route is added once the action ran."""
    # show_candidate never sets `route`, so its result is a ShownAction without one.
    return cast("ShownAction", show_candidate(c))


def _failed(code: str, window: ShownWindow, message: str, action: ShownAction) -> ActOutput:
    return {"status": "failed", "code": code, "window": window, "message": message, "action": action}


async def _step(session: ToolSession, t: Target, args: Step, before: Snapshot, seen: _Seen) -> ActOutput:
    window = show_window(before)
    learned = session.menu_keys.table_for(t.pid)
    text = args.get("text")
    candidate_id = args.get("candidateId")
    cands = build_candidates(
        before, BuildOptions(instruction=args["instruction"], text=text, list_text_kinds=True, learned=learned)
    )

    def with_usage(out: ActOutput) -> ActOutput:
        """Jev's usage goes with every step that asked Jev (no candidateId), whatever its outcome."""
        if candidate_id is None:
            out["jev"] = session.jev().take_usage().as_output()
        return out

    pick: PickOut | None = None
    if candidate_id is not None:
        chosen = next((c for c in cands if c.id == candidate_id), None)
        if chosen is None:
            message = f"candidate {candidate_id} is not on the window now; call observe for current ids"
            return {"status": "not_found", "window": window, "message": message}
        if chosen.kind in TEXT_KINDS and text is None:
            return _failed("text_required", window, f"{chosen.kind} needs `text`", _shown_action(chosen))
        if chosen.kind not in TEXT_KINDS and text is not None:
            message = f"{chosen.kind} enters no text; leave `text` out or pick a text candidate"
            return _failed("text_not_used", window, message, _shown_action(chosen))
    else:
        # With a text to enter, only the actions that enter text are in the running.
        pool = [c for c in cands if (c.kind in TEXT_KINDS) == (text is not None)]
        if not pool:
            message = (
                "no field, pop-up or keypad on this window can take text"
                if text is not None
                else "no actions on this window"
            )
            return with_usage({"status": "not_found", "window": window, "message": message})
        r = await rank(
            session.jev(),
            build_state(args["instruction"], before, text),
            pool,
            RankSpec(
                describe=lambda c: describe(c, before, learned),
                question=action_question,
                forced=action_question_forced,
                order=in_shard_order,
            ),
        )
        match gate(r):
            case NotFound(p_none=p_none):
                return with_usage(
                    {
                        "status": "not_found",
                        "window": window,
                        "message": f"Jev found no action for this instruction (p(none)={fixed(p_none, 2)})",
                        "candidates": [show_candidate(x.item, x.p) for x in r.ranked[:5]],
                    }
                )
            case Ambiguous(shortlist=shortlist):
                return with_usage(
                    {
                        "status": "ambiguous",
                        "window": window,
                        "message": "no clear leader; call act again with the candidateId of the right one",
                        "candidates": [show_candidate(x.item, x.p) for x in shortlist],
                    }
                )
            case Pick(item=item, p=p, rule=rule):
                chosen = item
                pick = {"p": round3(p), "rule": rule}

    def with_pick(out: ActOutput) -> ActOutput:
        if pick is not None:
            out["pick"] = pick
        return out

    modifiers = args.get("modifiers", [])
    if modifiers and chosen.kind not in CLICK_KINDS:
        message = f"modifier keys apply to a click or toggle, not to {chosen.kind}"
        return with_usage(_failed("modifiers_not_used", window, message, _shown_action(chosen)))
    if chosen.destructive and not args.get("allowDestructive"):
        message = (
            "this action may not be undoable; call act again with this candidateId and allowDestructive: true "
            "if it is intended"
        )
        return with_usage(
            with_pick(
                {"status": "needs_confirmation", "window": window, "message": message, "action": _shown_action(chosen)}
            )
        )
    if chosen.needs_foreground:
        message = "this menu command has no keyboard shortcut, so it cannot run in the background"
        return with_usage(_failed("foreground_required", window, message, _shown_action(chosen)))

    driver = await session.driver()
    executor = Executor(driver, lambda: session.snapshot(t), menu_keys=session.menu_keys)
    await driver.sentinel.watch(t.pid)
    try:
        return with_usage(
            with_pick(await _execute(session, t, driver, executor, chosen, modifiers, before, window, seen))
        )
    except ForegroundViolation as err:
        return with_usage(_failed("foreground_violation", window, str(err), _shown_action(chosen)))
    finally:
        driver.sentinel.stop()


async def _execute(
    session: ToolSession,
    t: Target,
    driver: Driver,
    executor: Executor,
    chosen: ActionCandidate,
    modifiers: Sequence[Modifier],
    before: Snapshot,
    window: ShownWindow,
    seen: _Seen,
) -> ActOutput:
    """Runs the action and judges its effect; `pick` and usage are added by the caller."""
    target = chosen.target
    # A toggle whose state cua-driver does not report is judged by its pixels as well.
    shot = (
        await capture_window(driver, t.pid, t.window_id, paths=session.paths)
        if chosen.kind == "toggle" and target is not None and target.exact is not True and target.frame is not None
        else None
    )
    # This app's pages already ignored an AXValue write once: type straight away rather than write and wait.
    retype = _retypeable(chosen, before)
    type_first = retype and t.pid in session.types_into_web_fields
    res = (
        await executor.retype_field(chosen, before) if type_first else await executor.execute(chosen, before, modifiers)
    )
    action = _shown_action(chosen)
    action["route"] = list(res.route)
    if not res.ok:
        code = res.code if res.code is not None else "failed"
        return _failed(code, window, res.detail if res.detail is not None else _REFUSED, action)

    def snapshot() -> Awaitable[Snapshot]:
        return session.snapshot(t)

    pixels = _toggled_pixels(session, driver, t, shot, chosen) if shot is not None else None
    verdict, after, snapshots = await _judge(chosen, before, snapshot, pixels)
    seen.after = after
    if not type_first and retype and verdict.effect in ("wrong", "none"):
        # The page kept its own copy of the value and ignored the AXValue write (or put it back): type it instead.
        again = await executor.retype_field(chosen, after)
        action["route"] = [*action.get("route", []), *again.route]
        if not again.ok:
            code = again.code if again.code is not None else "failed"
            detail = again.detail if again.detail is not None else _REFUSED
            return _failed(code, show_window(after), detail, action)
        verdict, after, more = await _judge(chosen, before, snapshot)
        seen.after = after
        snapshots += more
        if verdict.effect == "ok":
            session.types_into_web_fields.add(t.pid)
    # A row's selection is often not in the accessibility tree: an unchanged window does not show it was not selected.
    if (
        verdict.effect == "none"
        and chosen.kind == "click"
        and target is not None
        and target.role in ("AXRow", "AXCell")
    ):
        verdict = EffectVerdict(
            effect="unverified",
            detail="nothing on the window changed, but a row's selection is often not shown in the accessibility "
            "tree, so it may be selected now",
        )
    verification: Verification = {"snapshots": snapshots}
    if verdict.exact is not None:
        verification["exact"] = verdict.exact
    return {
        "status": STATUS[verdict.effect],
        "window": show_window(after),
        "message": verdict.detail,
        "action": action,
        "verification": verification,
        "change": change_of(before, after),
    }


def _retypeable(c: ActionCandidate, snap: Snapshot) -> bool:
    """A set_value on a web page's one-line text field, which a person's keystrokes can replace."""
    target = c.target
    return (
        c.kind == "set_value"
        and target is not None
        and target.role in ("AXTextField", "AXSearchField")
        and in_web_area(snap, target)
    )


async def _judge(
    c: ActionCandidate,
    before: Snapshot,
    snapshot: Callable[[], Awaitable[Snapshot]],
    pixels: Callable[[], Awaitable[EffectVerdict | None]] | None = None,
) -> tuple[EffectVerdict, Snapshot, int]:
    """Fresh snapshots, up to the configured number of them, until the effect asked for shows.

    The count decides, not a wait: a window unchanged across all of them is no_effect. `pixels` is
    extra evidence for an effect the snapshot cannot show.
    """
    retakes = config.effect_retakes()
    after = await snapshot()
    n = 1
    while True:
        verdict = verify_effect(c, before, after)
        if verdict.effect != "ok" and pixels is not None:
            seen = await pixels()
            if seen is not None:
                verdict = seen
        if verdict.effect in ("ok", "unverified") or n >= retakes:
            return verdict, after, n
        after = await snapshot()
        n += 1


def _toggled_pixels(
    session: ToolSession, driver: Driver, t: Target, before: Shot, c: ActionCandidate
) -> Callable[[], Awaitable[EffectVerdict | None]]:
    """Whether the toggle's own pixels changed since `before` (its state is not in the tree)."""

    async def check() -> EffectVerdict | None:
        target = c.target
        now = await capture_window(driver, t.pid, t.window_id, paths=session.paths)
        if now is None or target is None or target.frame is None:
            return None
        change = region_change(before, now, target.frame)
        if change is None or not change.significant:
            return None
        return EffectVerdict(
            effect="ok",
            detail=f'{bare_role(target.role)} "{target.label}" changed its appearance '
            "(its state is not in the accessibility tree)",
        )

    return check
