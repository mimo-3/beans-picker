"""Exact state of editable fields and toggles, read by the native axtext helper.

cua-driver trims whitespace at both ends of a value, reports an empty field's placeholder as its
value and leaves out a checkbox's state. The read-only helper copies AXValue as it is (nothing is
messaged, activated or raised; secure fields are skipped).

The helper needs the Accessibility permission. It is tried as a plain child process first (that
works when the host running this server has the permission), then as a background-only app of its
own, launched with `open -g`, which macOS lists under Privacy & Security > Accessibility by its own
name once `cua-jev grant-ax` has asked for it. Without either, values stay as cua-driver gives them
and no node is marked `exact`.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from cua_jev._aio import Clock
from cua_jev._numbers import scalar_text
from cua_jev._proc import Runner, run
from cua_jev._text import trim
from cua_jev.observe.helpers import AxtextBuild, axtext_binary
from cua_jev.observe.normalize import normalize_text
from cua_jev.observe.types import UINode
from cua_jev.paths import Paths

# AXIncrementor is a number field on a web page (<input type="number">) or a native stepper.
EDITABLE_ROLES: Final = frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXIncrementor"})
TOGGLE_ROLES: Final = frozenset({"AXCheckBox", "AXRadioButton", "AXSwitch"})

# After a round in which neither way of running the helper worked, it is not tried again for this long.
_RETRY_AFTER_S: Final = 30.0
_DIRECT_MAX_BYTES: Final = 32 * 1024 * 1024

type _Via = Literal["direct", "app", "none"]


@dataclass(frozen=True, slots=True)
class _Mode:
    via: _Via
    at: float


class ExactText:
    """Runs the helper for one session, remembering which way of running it worked last.

    A way that failed is probed again at most every 30 seconds.
    """

    def __init__(
        self, helpers: AxtextBuild, paths: Paths, *, runner: Runner = run, clock: Clock = time.monotonic
    ) -> None:
        self._helpers = helpers
        self._paths = paths
        self._runner = runner
        self._clock = clock
        self._mode: _Mode | None = None

    async def read_fields(self, pid: int) -> list[object] | None:
        """The helper's `fields` list for the app (entries unchecked), or `None` when it cannot
        be read. An empty list is a successful read."""
        app = await self._helpers.axtext_app()
        if app is None:
            return None
        if self._mode is not None and self._mode.via == "none" and self._clock() - self._mode.at < _RETRY_AFTER_S:
            return None
        if self._mode is None or self._mode.via != "app":
            direct = await self._run_direct(axtext_binary(app), pid)
            if direct is not None:
                self._mode = _Mode("direct", self._clock())
                return direct
        out = self._paths.axtext / f"out-{os.getpid()}-{time.time_ns() // 1_000_000}.json"
        try:
            await self._runner(("open", "-W", "-g", "-n", str(app), "--args", str(pid), str(out)))
            via_app = _parse(_read_text(out))
        except Exception:
            self._mode = _Mode("none", self._clock())
            return None
        finally:
            out.unlink(missing_ok=True)
        self._mode = _Mode("app" if via_app is not None else "none", self._clock())
        return via_app

    async def _run_direct(self, binary: Path, pid: int) -> list[object] | None:
        try:
            done = await self._runner((str(binary), str(pid)), max_bytes=_DIRECT_MAX_BYTES)
        except Exception:
            return None
        return _parse(done.stdout)


def _read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None


def _parse(text: str | None) -> list[object] | None:
    """The `fields` list of the helper's JSON output; `None` when there is none (or it is not a list)."""
    if text is None:
        return None
    try:
        doc: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    fields: object = doc.get("fields")
    return list(fields) if isinstance(fields, list) else None


async def prompt_for_access(helpers: AxtextBuild, *, runner: Runner = run) -> str:
    """Asks macOS to list the helper under Accessibility (the dialog opens System Settings)."""
    app = await helpers.axtext_app()
    if app is None:
        return "the helper could not be built (is clang installed?)"
    await runner(("open", "-W", "-n", str(app), "--args", "--prompt"))
    return f"asked macOS to list {app} under Privacy & Security > Accessibility; turn it on there"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Field:
    """One element's exact state, in tree order."""

    window: object
    role: object
    title: object
    value: object
    document: object

    def text(self) -> str:
        """The field's text; the helper always writes it as a string."""
        if not isinstance(self.value, str):
            raise TypeError("an exact field has no text value")
        return self.value


def _field(entry: object) -> _Field:
    """Reads one helper entry. A missing key reads as absent; `null` is not an entry."""
    if entry is None:
        raise TypeError("an exact field is null")
    d = entry if isinstance(entry, dict) else {}
    return _Field(
        window=d.get("window"),
        role=d.get("role"),
        title=d.get("title"),
        value=d.get("value"),
        document=d.get("document"),
    )


def apply_exact_text(nodes: Sequence[UINode], fields: Sequence[object], window_title: str) -> None:
    """Gives editable nodes and toggles their exact state (in place).

    The helper lists elements in tree order, as cua-driver does, so they are paired by position
    when both list the same number and every pair agrees (the same text trimmed, or an empty field
    that cua-driver shows with its placeholder). Otherwise a node takes the one field whose trimmed
    text is its value, when there is exactly one. Only fields of the window titled `window_title`
    are used, unless none is.
    """
    entries = [_field(f) for f in fields]
    in_window = [f for f in entries if isinstance(f.window, str) and f.window == window_title]
    pool = in_window or entries
    _pair_text([n for n in nodes if n.role in EDITABLE_ROLES], [f for f in pool if _role_in(f, EDITABLE_ROLES)])
    toggles = [n for n in nodes if n.role in TOGGLE_ROLES]
    states = [f for f in pool if _role_in(f, TOGGLE_ROLES)]
    if len(toggles) == len(states) and all(
        not _truthy(s.title) or _normalized(s.title) == (n.raw_label if n.raw_label is not None else "")
        for n, s in zip(toggles, states, strict=True)
    ):
        for n, s in zip(toggles, states, strict=True):
            _set_exact(n, s)


def _pair_text(nodes: list[UINode], pool: list[_Field]) -> None:
    # cua-driver reports no value at all for some fields (a number field): then only the role can agree.
    def agrees(n: UINode, f: _Field) -> bool:
        if not _same_family(f.role, n.role):
            return False
        if n.raw_value is None:
            return f.role == n.role
        return trim(f.text()) == trim(n.raw_value) or f.text() == ""

    if len(nodes) == len(pool) and all(agrees(n, f) for n, f in zip(nodes, pool, strict=True)):
        for n, f in zip(nodes, pool, strict=True):
            _set_exact(n, f)
        return
    for n in nodes:
        # With no value of its own to match, a node could take any other field's text.
        if n.raw_value is None:
            continue
        shown = trim(n.raw_value)
        matches = [f for f in pool if _same_family(f.role, n.role) and trim(f.text()) == shown]
        if len({f.text() for f in matches}) == 1:
            _set_exact(n, matches[0])


def _set_exact(n: UINode, f: _Field) -> None:
    raw = f.value if isinstance(f.value, str) or f.value is None else scalar_text(f.value)
    # An empty field shows its placeholder in cua-driver's reading; when the field has no title of
    # its own, that text is no name (it goes away as soon as the field is typed in).
    if raw == "" and n.value and isinstance(f.title, str) and normalize_text(f.title) != n.value:
        n.placeholder = n.value
    n.raw_value = raw
    n.value = normalize_text(raw)
    n.exact = True
    if _truthy(f.document):
        n.document = f.document if isinstance(f.document, str) else scalar_text(f.document)


def _role_in(f: _Field, roles: frozenset[str]) -> bool:
    return isinstance(f.role, str) and f.role in roles


def _family(role: object) -> object:
    """A text area pairs only with a text area and a number field with a number field; the
    single-line roles (field, search field, combo box) pair with each other."""
    return role if role in ("AXTextArea", "AXIncrementor") else "line"


def _same_family(a: object, b: object) -> bool:
    return _family(a) == _family(b)


def _normalized(v: object) -> str:
    return normalize_text(v if isinstance(v, str | int | float) else scalar_text(v))


def _truthy(v: object) -> bool:
    """Truth as a JSON value has it: `null`, `false`, `0`, NaN and `""` are false; lists and
    objects (even empty) are true."""
    if v is None or v is False:
        return False
    if isinstance(v, int | float):
        return v != 0 and not math.isnan(v)
    if isinstance(v, str):
        return v != ""
    return True
