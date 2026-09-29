"""act: one operation, or a short sequence of them, per call."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Literal, NotRequired, TypedDict, cast

from beans_picker import config
from beans_picker._numbers import fixed, round3
from beans_picker.act.execute import Executor
from beans_picker.candidates.build import BuildOptions, build_candidates
from beans_picker.candidates.describe import describe
from beans_picker.candidates.prune import in_shard_order
from beans_picker.candidates.types import TEXT_KINDS, ActionCandidate
from beans_picker.driver.mcp import Driver
from beans_picker.errors import DriverError, DriverTimeout, ForegroundViolation, ToolError, failure
from beans_picker.jev.client import JevUsageOut
from beans_picker.jev.questions import action_question, action_question_forced
from beans_picker.jev.rank import Ambiguous, NotFound, Pick, RankSpec, gate, rank
from beans_picker.jev.state import CHANGED_TEXTS, Change, bare_role, build_state, change_of, screen_text
from beans_picker.observe.snapshot import in_web_area
from beans_picker.observe.types import Snapshot
from beans_picker.observe.visual import Shot, capture_window, region_change
from beans_picker.tools.args import ActArgs, Modifier, Step
from beans_picker.tools.present import SCREEN_LINES, ShownCandidate, ShownWindow, show_candidate, show_window
from beans_picker.tools.session import Target, ToolSession
from beans_picker.verify.effect import EffectVerdict, VerifyEffect, verify_effect

_log = logging.getLogger(__name__)

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
_SETTLED_AT_ONCE: Final = frozenset({"set_value", "type_into", "append", "keypad", "toggle"})
"""Kinds whose effect is the control's own value: once it shows, there is nothing more to wait for."""
SETTLE_RETAKES: Final = 4
"""How many more snapshots act takes, at most, while the window's controls keep changing after the effect showed."""


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
    screenText: NotRequired[list[str]]
    newCandidates: NotRequired[list[ShownCandidate]]
    newTotal: NotRequired[int]
    pick: NotRequired[PickOut]
    jev: NotRequired[JevUsageOut]
    steps: NotRequired[list[ActOutput]]
    skipped: NotRequired[int]


async def act_tool(session: ToolSession, args: ActArgs) -> ActOutput:
    t = await session.target(args)
    before = await session.snapshot(t)
    first, after = await _act_once(session, t, args, before)
    then = args.get("then")
    if not then:
        return _with_new_candidates(session, t, first, before, after)
    outs = [first]
    for step in then:
        if outs[-1]["status"] not in CARRY_ON:
            break
        # The previous step's last snapshot already shows its effect: it is this step's "before".
        try:
            out, after = await _act_once(session, t, step, after)
        except Exception as err:
            # Earlier steps already changed the window: their results must reach the caller.
            code, message = failure(err)
            if not isinstance(err, ToolError):
                _log.warning("act step %d failed (%s, %s)", len(outs) + 1, code, type(err).__name__)
            out = {"status": "failed", "code": code, "message": message}
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
    return _with_new_candidates(session, t, result, before, after)


def _with_new_candidates(
    session: ToolSession, t: Target, out: ActOutput, before: Snapshot, after: Snapshot
) -> ActOutput:
    """What the action brought up (a dialog, a menu, the next page): its texts and its controls with their ids, so
    the next step can use them without another observe."""
    if after is before or out["status"] not in CARRY_ON:
        return out
    opts = BuildOptions(list_text_kinds=True, learned=session.menu_keys.table_for(t.pid))
    had = {c.key for c in build_candidates(before, opts)}
    now = build_candidates(after, opts)
    new = [c for c in now if c.key not in had and c.kind not in ("menu", "key")]
    gone = len(had - {c.key for c in now})
    # More new texts than `change` names: the page's content changed, so its text is shown too.
    was = {x.value for x in before.texts}
    if new or gone or sum(x.value not in was for x in after.texts) > CHANGED_TEXTS:
        out["screenText"] = screen_text(after, SCREEN_LINES)
    if new:
        out["newCandidates"] = [show_candidate(c) for c in new[:NEW_CANDIDATES]]
        if len(new) > NEW_CANDIDATES:
            out["newTotal"] = len(new)
    return out


NEW_CANDIDATES: Final = 80
"""As many as a plain observe lists, so a step that opens a new page needs no observe."""


def _without_window(out: ActOutput) -> ActOutput:
    rest = out.copy()
    rest.pop("window", None)
    return rest


class _Seen:
    __slots__ = ("after",)

    def __init__(self, before: Snapshot) -> None:
        self.after = before


async def _act_once(session: ToolSession, t: Target, args: Step, before: Snapshot) -> tuple[ActOutput, Snapshot]:
    seen = _Seen(before)
    out = await _step(session, t, args, before, seen)
    return out, seen.after


def _shown_action(c: ActionCandidate) -> ShownAction:
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
            with_pick(
                await _execute(
                    session,
                    t,
                    driver,
                    executor,
                    chosen,
                    modifiers,
                    before,
                    window,
                    seen,
                    allow_destructive=bool(args.get("allowDestructive")),
                )
            )
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
    *,
    allow_destructive: bool = False,
) -> ActOutput:
    target = chosen.target
    # A toggle whose state cua-driver does not report is judged by its pixels as well.
    shot = (
        await capture_window(driver, t.pid, t.window_id, paths=session.paths)
        if chosen.kind == "toggle" and target is not None and target.exact is not True and target.frame is not None
        else None
    )
    # A web page's own copy of a field's value (a React form's state) follows typing, not an AXValue write: the write
    # shows in the field, yet the page saves what it had.
    retype = _retypeable(chosen, before)
    res = (
        await executor.retype_field(chosen, before)
        if retype
        else await executor.execute(chosen, before, modifiers, allow_destructive=allow_destructive)
    )
    action = _shown_action(chosen)
    action["route"] = list(res.route)
    if not res.ok:
        if res.code == "needs_confirmation":
            return {
                "status": "needs_confirmation",
                "window": window,
                "action": action,
                "message": "the selected option may be destructive; repeat with allowDestructive: true if intended",
            }
        code = res.code if res.code is not None else "failed"
        return _failed(code, window, res.detail if res.detail is not None else _REFUSED, action)

    def snapshot() -> Awaitable[Snapshot]:
        return session.snapshot(t)

    pixels = _toggled_pixels(session, driver, t, shot, chosen) if shot is not None else None
    verdict, after, snapshots = await _judge(chosen, before, snapshot, pixels)
    seen.after = after
    if retype and verdict.effect in ("wrong", "none"):
        # A keystroke delivered in the background can be dropped: type the text once more.
        again = await executor.retype_field(chosen, after)
        action["route"] = [*action.get("route", []), *again.route]
        if not again.ok:
            code = again.code if again.code is not None else "failed"
            detail = again.detail if again.detail is not None else _REFUSED
            return _failed(code, show_window(after), detail, action)
        verdict, after, more = await _judge(chosen, before, snapshot)
        seen.after = after
        snapshots += more
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
    target = c.target
    if c.kind != "set_value" or target is None or not in_web_area(snap, target):
        return False
    # A typed line break is a Return, which submits a form or sends a message: text with one is written instead.
    return target.role in ("AXTextField", "AXSearchField", "AXTextArea") and "\n" not in (c.text or "")


async def _judge(
    c: ActionCandidate,
    before: Snapshot,
    snapshot: Callable[[], Awaitable[Snapshot]],
    pixels: Callable[[], Awaitable[EffectVerdict | None]] | None = None,
) -> tuple[EffectVerdict, Snapshot, int]:
    """Retakes snapshots until the effect shows, then until the window's controls stop changing; the count decides,
    not a wait."""
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
            break
        after = await snapshot()
        n += 1
    if verdict.effect != "ok" or c.kind in _SETTLED_AT_ONCE:
        return verdict, after, n
    # A page may still be loading what the step brought up: wait until two snapshots offer the same controls. Their
    # values and texts are left out, so a clock or a spinner does not keep the wait going.
    for _ in range(SETTLE_RETAKES):
        try:
            later = await snapshot()
        except (ToolError, DriverError, DriverTimeout):
            # The step already ran and its effect showed: a snapshot that fails now only ends the wait.
            break
        n += 1
        same = _controls(later) == _controls(after)
        after = later
        if same:
            break
    again = verify_effect(c, before, after)
    # A chosen option is judged by its value, which the page may have put back meanwhile; a click by any change,
    # which a passing notice may have taken away again.
    return (again if again.effect == "ok" or c.kind in TEXT_KINDS else verdict), after, n


def _controls(snap: Snapshot) -> list[tuple[str, str]]:
    """Roles and names only: a look-alike's key can carry the page's state, which a spinner keeps changing."""
    modal = [(snap.modal.role, snap.modal.label)] if snap.modal is not None else []
    return modal + [(n.role, n.label) for n in snap.nodes]


def _toggled_pixels(
    session: ToolSession, driver: Driver, t: Target, before: Shot, c: ActionCandidate
) -> Callable[[], Awaitable[EffectVerdict | None]]:
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
