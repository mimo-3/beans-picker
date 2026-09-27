"""Exact state of editable fields and toggles, read by the native axtext helper."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Literal

from beans_picker._aio import Clock
from beans_picker._numbers import scalar_text
from beans_picker._proc import Runner, run
from beans_picker._text import trim
from beans_picker.observe.helpers import AxtextBuild, axtext_binary
from beans_picker.observe.normalize import normalize_text
from beans_picker.observe.types import UINode
from beans_picker.paths import Paths

# AXIncrementor is a number field on a web page (<input type="number">) or a native stepper.
EDITABLE_ROLES: Final = frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXIncrementor"})
TOGGLE_ROLES: Final = frozenset({"AXCheckBox", "AXRadioButton", "AXSwitch"})

_RETRY_AFTER_S: Final = 30.0
_DIRECT_MAX_BYTES: Final = 32 * 1024 * 1024

type _Via = Literal["direct", "app", "none"]


@dataclass(frozen=True, slots=True)
class _Mode:
    via: _Via
    at: float


class ExactText:
    """Runs the helper for one session, remembering which way of running it worked last."""

    def __init__(
        self, helpers: AxtextBuild, paths: Paths, *, runner: Runner = run, clock: Clock = time.monotonic
    ) -> None:
        self._helpers = helpers
        self._paths = paths
        self._runner = runner
        self._clock = clock
        self._mode: _Mode | None = None

    async def read_fields(self, pid: int) -> list[object] | None:
        """The helper's `fields` list for the app (entries unchecked), or `None` when it cannot be read."""
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
        try:
            with TemporaryDirectory(prefix="read-", dir=self._paths.axtext) as directory:
                out = Path(directory) / "fields.json"
                await self._runner(("open", "-W", "-g", "-n", str(app), "--args", str(pid), str(out)))
                via_app = _parse(_read_text(out))
        except Exception:
            self._mode = _Mode("none", self._clock())
            return None
        self._mode = _Mode("app" if via_app is not None else "none", self._clock())
        return via_app

    async def _run_direct(self, binary: Path, pid: int) -> list[object] | None:
        try:
            done = await self._runner((str(binary), str(pid)), max_bytes=_DIRECT_MAX_BYTES)
        except Exception:
            return None
        return _parse(done.stdout)


def _read_text(path: Path) -> str | None:
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _DIRECT_MAX_BYTES:
                return None
            content = source.read(_DIRECT_MAX_BYTES + 1)
            return content if len(content) <= _DIRECT_MAX_BYTES else None
    finally:
        os.close(directory)


def _parse(text: str | None) -> list[object] | None:
    if text is None:
        return None
    try:
        doc: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    fields: object = doc.get("fields")
    windows: object = doc.get("windows")
    if not isinstance(fields, list) or not isinstance(windows, list) or any(not isinstance(w, str) for w in windows):
        return None
    counts = Counter(normalize_text(w) for w in windows)
    return [
        {**f, "window": normalize_text(f["window"])}
        for f in fields
        if isinstance(f, dict) and isinstance(f.get("window"), str) and counts[normalize_text(f["window"])] == 1
    ]


async def prompt_for_access(helpers: AxtextBuild, *, runner: Runner = run) -> str:
    """Asks macOS to list the helper under Accessibility (the dialog opens System Settings)."""
    app = await helpers.axtext_app()
    if app is None:
        return "the helper could not be built (is clang installed?)"
    await runner(("open", "-W", "-n", str(app), "--args", "--prompt"))
    return f"asked macOS to list {app} under Privacy & Security > Accessibility; turn it on there"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Field:
    window: object
    role: object
    title: object
    value: object
    document: object

    def text(self) -> str:
        if not isinstance(self.value, str):
            raise TypeError("an exact field has no text value")
        return self.value


def _field(entry: object) -> _Field:
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
    """Gives editable nodes and toggles their exact state (in place)."""
    entries = [_field(f) for f in fields]
    in_window = [f for f in entries if isinstance(f.window, str) and f.window == window_title]
    # Never use another window's values when this window is absent from the helper response.
    pool = in_window
    text_nodes = [n for n in nodes if n.role in EDITABLE_ROLES and n.subrole != "AXSecureTextField"]
    text_fields = [f for f in pool if _role_in(f, EDITABLE_ROLES)]
    # A missing control or multiple windows with the same title makes positional pairing unsafe.
    if len(text_nodes) == len(text_fields):
        _pair_text(text_nodes, text_fields)
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
        title = _normalized(f.title) if isinstance(f.title, str) else ""
        label = n.raw_label or n.title or ""
        if title and label and title != label:
            return False
        if n.raw_value is None:
            return f.role == n.role and bool(title) and title == label
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
        matches = [f for f in pool if agrees(n, f) and trim(f.text()) == shown]
        if len({f.text() for f in matches}) == 1:
            _set_exact(n, matches[0])


def _set_exact(n: UINode, f: _Field) -> None:
    raw = f.value if isinstance(f.value, str) or f.value is None else scalar_text(f.value)
    # An untitled empty field's placeholder is not its name.
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
    return role if role in ("AXTextArea", "AXIncrementor") else "line"


def _same_family(a: object, b: object) -> bool:
    return _family(a) == _family(b)


def _normalized(v: object) -> str:
    return normalize_text(v if isinstance(v, str | int | float) else scalar_text(v))


def _truthy(v: object) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, int | float):
        return v != 0 and not math.isnan(v)
    if isinstance(v, str):
        return v != ""
    return True
