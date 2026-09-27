"""The small `state` object Jev sees (never the whole tree), and what changed between two snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, NotRequired, TypedDict

from beans_picker._json import JsonValue, quote
from beans_picker.observe.exacttext import EDITABLE_ROLES
from beans_picker.observe.menudiff import enabled_changes, relabeled_menu_items
from beans_picker.observe.normalize import truncate
from beans_picker.observe.types import Snapshot, TextNode, UINode

type JevState = dict[str, JsonValue]

NAMED_CONTROLS: Final = frozenset({"AXButton", "AXLink", "AXPopUpButton", "AXMenuButton"})
"""Controls a page can name with no text inside them (a file-name button in a table row)."""


def bare_role(role: str) -> str:
    """The role without its leading `AX` (`AXButton` -> `Button`)."""
    return role[2:] if role.startswith("AX") else role


def _by_index(snap: Snapshot) -> dict[int, UINode]:
    return {n.index: n for n in snap.nodes}


def _up(by_index: dict[int, UINode], index: int | None) -> Iterator[UINode]:
    cur = by_index.get(index if index is not None else -1)
    while cur is not None:
        yield cur
        cur = by_index.get(cur.parent if cur.parent is not None else -1)


def reading_texts(snap: Snapshot) -> list[TextNode]:
    """The window's texts in reading order, with unlabeled table-row controls named in place."""
    by_index = _by_index(snap)
    holds_text = {n.index for t in snap.texts for n in _up(by_index, t.parent_index)}
    named = [
        n
        for n in snap.nodes
        if n.role in NAMED_CONTROLS
        and n.label
        and n.index not in holds_text
        and any(a.role == "AXRow" for a in _up(by_index, n.parent))
    ]
    if not named:
        return snap.texts
    out = list(snap.texts)
    for n in named:
        # Texts come in tree order, so the control's place is before the first text anchored after it.
        at = next(
            (i for i, t in enumerate(out) if (t.parent_index if t.parent_index is not None else -1) > n.index),
            len(out),
        )
        out.insert(
            at,
            TextNode(
                role="AXStaticText",
                value=n.label,
                raw=n.raw_label if n.raw_label is not None else n.label,
                depth=n.depth + 1,
                parent_index=n.index,
            ),
        )
    return out


def screen_text(snap: Snapshot, limit: int = 20) -> list[str]:
    """The window's texts in order, repeats left out."""
    by_index = _by_index(snap)

    def row_of(t: TextNode) -> int | None:
        return next((n.index for n in _up(by_index, t.parent_index) if n.role == "AXRow"), None)

    lines: list[tuple[int | None, list[str]]] = []
    for t in reading_texts(snap):
        row = row_of(t)
        if row is not None and lines and lines[-1][0] == row:
            lines[-1][1].append(truncate(t.value, 60))
        else:
            lines.append((row, [truncate(t.value, 80)]))
    seen: set[str] = set()
    out: list[str] = []
    for _, texts in lines:
        v = truncate(" | ".join(texts), 200)
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= limit:
            break
    return out


class Field(TypedDict):
    role: str
    label: str
    value: str


def fields_of(snap: Snapshot, limit: int = 12) -> list[Field]:
    """The editable fields, each with the name a reader would give it and its (display) value."""
    out: list[Field] = []
    for n in [n for n in snap.nodes if n.role in EDITABLE_ROLES][:limit]:
        name = n.raw_label if n.raw_label and n.raw_label != n.value else n.identifier
        out.append(
            {
                "role": n.role,
                "label": truncate(name if name is not None else "", 40),
                "value": truncate(n.value if n.value is not None else "", 200),
            }
        )
    return out


def build_state(instruction: str, snap: Snapshot, text: str | None = None) -> JevState:
    """What Jev is told about the window."""
    state: JevState = {"instruction": instruction}
    if text is not None:
        state["text"] = text
    state["app"] = snap.app_name
    state["window"] = snap.window_title
    state["modal"] = (
        {"kind": bare_role(snap.modal.role).lower(), "title": snap.modal.label} if snap.modal is not None else None
    )
    state["screen_text"] = list[JsonValue](screen_text(snap))
    state["fields"] = [{"role": f["role"], "label": f["label"], "value": f["value"]} for f in fields_of(snap)]
    if snap.app_windows:
        state["other_windows"] = list[JsonValue](snap.app_windows)
    return state


class Change(TypedDict):
    """What changed between two snapshots; the optional keys are present only when non-empty."""

    appeared: list[str]
    disappeared: list[str]
    fields_changed: NotRequired[list[str]]
    texts_changed: NotRequired[list[str]]
    window_title: NotRequired[str]
    menu_items_retitled: NotRequired[list[str]]
    menu_items_enabled: NotRequired[list[str]]
    menu_items_disabled: NotRequired[list[str]]


def _field_value(n: UINode) -> str:
    if n.raw_value is not None:
        return n.raw_value
    return n.value if n.value is not None else ""


def _field_change(n: UINode, was: str) -> str:
    name = f' "{truncate(n.raw_label, 30)}"' if n.raw_label and n.raw_label != n.value else ""
    return f"{bare_role(n.role)}{name}: {quote(truncate(was, 60))} \u2192 {quote(truncate(_field_value(n), 60))}"


def _controls_and_windows(snap: Snapshot) -> dict[str, None]:
    names = dict.fromkeys(controls_of(snap, 500))
    names.update(dict.fromkeys(f"Window {w}" for w in snap.app_windows or []))
    return names


def change_of(before: Snapshot, after: Snapshot) -> Change:
    """Controls that appeared or disappeared (12 at most each), fields, texts and menu titles."""
    a = _controls_and_windows(before)
    b = _controls_and_windows(after)
    retitled = [
        f'{" > ".join(r.parent)}: "{r.from_}" is now "{r.to}"' for r in relabeled_menu_items(before.menu, after.menu)
    ]
    # Enabled states are only comparable when both menu bars belonged to our window.
    both_front = before.frontmost is True and after.frontmost is True
    toggled = enabled_changes(before.menu, after.menu) if both_front else {"enabled": [], "disabled": []}
    was = {n.key: _field_value(n) for n in before.nodes if n.role in EDITABLE_ROLES}
    fields = [
        _field_change(n, was[n.key])
        for n in after.nodes
        if n.role in EDITABLE_ROLES and n.key in was and was[n.key] != _field_value(n)
    ][:4]
    before_texts = {t.value for t in before.texts}
    texts = [truncate(t, 60) for t in dict.fromkeys(t.value for t in after.texts) if t not in before_texts][:6]
    out: Change = {
        "appeared": [x for x in b if x not in a][:12],
        "disappeared": [x for x in a if x not in b][:12],
    }
    if fields:
        out["fields_changed"] = fields
    if texts:
        out["texts_changed"] = texts
    if before.window_title != after.window_title:
        out["window_title"] = after.window_title
    if retitled:
        out["menu_items_retitled"] = retitled
    if toggled["enabled"]:
        out["menu_items_enabled"] = toggled["enabled"]
    if toggled["disabled"]:
        out["menu_items_disabled"] = toggled["disabled"]
    return out


def changed(c: Change) -> bool:
    """Whether anything changed: a non-empty list, or a window title (even an empty one)."""
    return any(len(v) > 0 if isinstance(v, list) else v is not None for v in c.values())


def controls_of(snap: Snapshot, limit: int = 40) -> list[str]:
    """Distinct names of the visible controls, with their on/selected state."""
    out: dict[str, None] = {}
    for n in snap.nodes:
        if n.role in EDITABLE_ROLES or n.role == "AXWindow" or not n.label:
            continue
        ident = n.identifier
        shown_id = f" ({truncate(ident, 40)})" if ident and ident != n.label and not ident.startswith("_") else ""
        is_set = n.value in ("1", "true")
        if n.role in ("AXCheckBox", "AXSwitch") and is_set:
            on = " (on)"
        elif n.role in ("AXRadioButton", "AXTab") and (is_set or n.selected is True):
            on = " (selected)"
        else:
            on = ""
        out[f"{bare_role(n.role)} {truncate(n.label, 30)}{shown_id}{on}"] = None
        if len(out) >= limit:
            break
    return list(out)
