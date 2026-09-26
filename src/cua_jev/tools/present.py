"""How candidates and windows are shown to the MCP caller: short, and only what it needs to choose."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from cua_jev._numbers import round3
from cua_jev.candidates.types import TEXT_KINDS, ActionCandidate
from cua_jev.jev.state import Field, bare_role, fields_of, screen_text
from cua_jev.observe.normalize import truncate
from cua_jev.observe.types import Snapshot


class ShownCandidate(TypedDict):
    id: str
    kind: str
    does: str
    value: NotRequired[str]
    needsText: NotRequired[bool]
    destructive: NotRequired[bool]
    shortcut: NotRequired[str]
    needsForeground: NotRequired[bool]
    p: NotRequired[float]


class ShownWindow(TypedDict):
    app: str
    pid: int
    windowId: int
    title: str
    modal: NotRequired[str]
    otherWindows: NotRequired[list[str]]


class ShownScreen(TypedDict):
    screenText: list[str]
    fields: list[Field]


def show_candidate(c: ActionCandidate, p: float | None = None) -> ShownCandidate:
    """The candidate as the caller sees it; `p` (rounded to three decimals) when it was ranked."""
    out: ShownCandidate = {"id": c.id, "kind": c.kind, "does": c.summary}
    value = c.target.value if c.target is not None else None
    if value is not None and value != "" and c.kind != "menu":
        out["value"] = truncate(value, 60)
    if c.kind in TEXT_KINDS and c.text is None:
        out["needsText"] = True
    if c.destructive:
        out["destructive"] = True
    if c.shortcut is not None:
        out["shortcut"] = "+".join(c.shortcut.keys)
    if c.needs_foreground:
        out["needsForeground"] = True
    if p is not None:
        out["p"] = round3(p)
    return out


def show_window(snap: Snapshot) -> ShownWindow:
    out: ShownWindow = {"app": snap.app_name, "pid": snap.pid, "windowId": snap.window_id, "title": snap.window_title}
    if snap.modal is not None:
        out["modal"] = f'{bare_role(snap.modal.role)} "{snap.modal.label}"'
    if snap.app_windows:
        out["otherWindows"] = snap.app_windows
    return out


def show_screen(snap: Snapshot) -> ShownScreen:
    return {"screenText": screen_text(snap, 30), "fields": fields_of(snap)}
