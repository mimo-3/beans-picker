"""Menu key equivalents learned from the target app itself."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeGuard

from beans_picker._json import dumps
from beans_picker._numbers import number_text
from beans_picker._proc import Runner, run
from beans_picker._text import NOT_LINE_END, WS, hash_bytes, trim
from beans_picker.errors import ProcessError
from beans_picker.menus.keyequiv import normalize_title
from beans_picker.paths import Paths

_log = logging.getLogger(__name__)

_SEP: Final = "\x01"
_HELPER_MAX_BYTES: Final = 4 * 1024 * 1024
_WS_RUN: Final = re.compile(f"{WS}+")
_BUNDLE_OF_EXECUTABLE: Final = re.compile(f"({NOT_LINE_END}*?\\.app)/Contents/MacOS/")
_MODIFIER_ORDER: Final = ("cmd", "ctrl", "option", "shift")
# Key equivalents written as characters (AppKit function-key constants live in the private use area).
_NAMED: Final[Mapping[str, str]] = {
    "\r": "return",
    "\n": "return",
    "\t": "tab",
    "\x1b": "escape",
    "\x08": "delete",
    "\x7f": "delete",
    " ": "space",
    chr(0xF700): "up",
    chr(0xF701): "down",
    chr(0xF702): "left",
    chr(0xF703): "right",
    chr(0xF729): "home",
    chr(0xF72B): "end",
    chr(0xF72C): "pageup",
    chr(0xF72D): "pagedown",
}
_F1: Final = 0xF704
_F12: Final = 0xF70F


@dataclass(frozen=True, slots=True, kw_only=True)
class RawMenuKey:
    """One entry of the helper's output: a menu item's localized path and its key equivalent."""

    path: list[str]
    key: str
    mods: list[str]
    top: int


@dataclass(frozen=True, slots=True, kw_only=True)
class LearnedKey:
    """A learned shortcut."""

    path: list[str]
    keys: list[str]
    english: list[str] | None = None


class MenuKeyTable:
    """The shortcuts learned for one app, looked up by observed menu path."""

    def __init__(self, items: Sequence[LearnedKey]) -> None:
        self.items: list[LearnedKey] = list(items)
        self._by_path: dict[str, LearnedKey] = {}
        self._english_by_leaf: dict[str, str] = {}
        for it in self.items:
            self._by_path[_SEP.join(it.path)] = it
            leaf = it.path[-1] if it.path else ""
            en = it.english[-1] if it.english else None
            if en and normalize_title(en) != leaf:
                self._english_by_leaf[leaf] = en

    @property
    def size(self) -> int:
        return len(self.items)

    def lookup(self, path: Sequence[str]) -> LearnedKey | None:
        """The learned shortcut for an observed menu path, matching titles after normalization."""
        p = [normalize_title(x) for x in path]
        found = self._by_path.get(_SEP.join(p))
        if found is None and len(p) > 1:
            found = self._by_path.get(_SEP.join(["", *p[1:]]))
        return found

    def english_title(self, leaf: str) -> str | None:
        """The English title of a localized item (from the same nib loaded in English)."""
        return self._english_by_leaf.get(normalize_title(leaf))


def hotkey_for(key: str, mods: Sequence[str]) -> list[str] | None:
    """A nib key equivalent as a cua-driver hotkey: modifiers first, one key last."""
    if len(key) != 1:
        return None
    m = set(mods)
    k = _NAMED.get(key)
    if k is None:
        cp = ord(key)
        if _F1 <= cp <= _F12:
            k = f"f{cp - _F1 + 1}"
        elif "A" <= key <= "Z":
            m.add("shift")
            k = key.lower()
        elif "!" <= key <= "~":
            k = key
    if k is None or not m:
        return None
    return [*(x for x in _MODIFIER_ORDER if x in m), k]


def table_from(local: Sequence[RawMenuKey], english: Sequence[RawMenuKey] | None = None) -> MenuKeyTable:
    """The table from the helper's output in the user's languages and, optionally, in English."""
    aligned = english if english and len(english) == len(local) else None
    items: list[LearnedKey] = []
    for i, it in enumerate(local):
        keys = hotkey_for(it.key, it.mods)
        if keys is None:
            continue
        path = [normalize_title(x) for x in it.path]
        if it.top == 0:
            if path:
                path[0] = ""
            else:
                path.append("")
        en = list(aligned[i].path) if aligned is not None else None
        items.append(LearnedKey(path=path, keys=keys, english=en))
    return MenuKeyTable(items)


def raw_menu_keys(value: object) -> list[RawMenuKey]:
    """Entries decoded from the helper's JSON output; ValueError when one is malformed."""
    if not isinstance(value, list):
        raise ValueError("menu keys: expected a list")
    return [_raw_menu_key(v) for v in value]


def _raw_menu_key(v: object) -> RawMenuKey:
    if not isinstance(v, dict):
        raise ValueError("menu keys: expected an object")
    path, key, mods, top = v.get("path"), v.get("key"), v.get("mods"), v.get("top")
    if (
        not _is_str_list(path)
        or not isinstance(key, str)
        or not _is_str_list(mods)
        or not isinstance(top, int)
        or isinstance(top, bool)
    ):
        raise ValueError("menu keys: malformed entry")
    return RawMenuKey(path=list(path), key=key, mods=list(mods), top=top)


def _is_str_list(v: object) -> TypeGuard[list[str]]:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _raw_json(items: Sequence[RawMenuKey]) -> list[object]:
    return [{"path": it.path, "key": it.key, "mods": it.mods, "top": it.top} for it in items]


class HelperBuilds(Protocol):
    """Where the compiled ``menukeys`` helper comes from (built once, None when it cannot be)."""

    async def menukeys_bin(self) -> Path | None: ...


class MenuKeys:
    """Learned menu-key tables per pid, for one session."""

    def __init__(self, helpers: HelperBuilds, paths: Paths, *, runner: Runner = run) -> None:
        self._helpers = helpers
        self._cache = paths.menukeys
        self._runner = runner
        self._tables: dict[int, MenuKeyTable] = {}
        self._pending: dict[int, asyncio.Task[MenuKeyTable | None]] = {}

    def table_for(self, pid: int) -> MenuKeyTable | None:
        """The table learned for `pid`, if learning finished."""
        return self._tables.get(pid)

    async def learn(self, pid: int) -> MenuKeyTable | None:
        """Learns the key equivalents of the app running as `pid`, once per pid."""
        done = self._tables.get(pid)
        if done is not None:
            return done
        task = self._pending.get(pid)
        if task is None:
            task = asyncio.create_task(self._learn_once(pid))
            self._pending[pid] = task
        # Shielded: a caller that goes away does not cancel the learning other callers share.
        return await asyncio.shield(task)

    async def close(self) -> None:
        """Cancels learning still in progress and waits for it to stop."""
        running = [task for task in self._pending.values() if not task.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running)

    async def _learn_once(self, pid: int) -> MenuKeyTable | None:
        try:
            table = await self._learn(pid)
        except Exception as err:  # learning is best effort: every failure means "no table"
            _log.debug("menu keys for pid %d: %s", pid, err)
            return None
        if table is not None:
            self._tables[pid] = table
        return table

    async def _learn(self, pid: int) -> MenuKeyTable | None:
        bundle = await self._bundle_path_of(pid)
        if bundle is None:
            return None
        info = Path(bundle) / "Contents" / "Info.plist"
        mtime = _mtime_ms(info)
        if mtime is None:
            return None
        langs = await self._languages()
        helper = await self._helpers.menukeys_bin()
        if helper is None:
            return None
        ident = f"{helper}{_SEP}{bundle}{_SEP}{number_text(mtime)}{_SEP}{langs}"
        key = hashlib.sha256(hash_bytes(ident)).hexdigest()[:16]
        cached = self._cache / f"table-{key}.json"
        table = _read_cached(cached)
        if table is not None:
            return table
        local = raw_menu_keys(await self._run_helper(helper, bundle, ()))
        if not local:
            return None
        english = raw_menu_keys(await self._run_helper(helper, bundle, ("-AppleLanguages", "(en)")))
        _write_cached(cached, dumps({"local": _raw_json(local), "english": _raw_json(english)}))
        return table_from(local, english)

    async def _bundle_path_of(self, pid: int) -> str | None:
        out = await self._runner(["ps", "-o", "comm=", "-p", str(pid)])
        m = _BUNDLE_OF_EXECUTABLE.match(trim(out.stdout))
        return m.group(1) if m else None

    async def _languages(self) -> str:
        try:
            out = await self._runner(["defaults", "read", "-g", "AppleLanguages"])
        except ProcessError:
            return ""
        return _WS_RUN.sub("", out.stdout)

    async def _run_helper(self, helper: Path, bundle: str, extra: Sequence[str]) -> object:
        try:
            out = await self._runner([str(helper), bundle, *extra], max_bytes=_HELPER_MAX_BYTES)
            value: object = json.loads(trim(out.stdout).split("\n")[-1])
        except (ProcessError, ValueError):
            return []
        return value if isinstance(value, list) else []


def _mtime_ms(info: Path) -> float | None:
    try:
        st = info.stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns / 1_000_000


def _read_cached(cached: Path) -> MenuKeyTable | None:
    try:
        data: object = json.loads(cached.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("menu keys cache: expected an object")
        english = data.get("english")
        return table_from(raw_menu_keys(data.get("local")), raw_menu_keys(english) if english is not None else None)
    except (OSError, ValueError):
        return None


def _write_cached(cached: Path, text: str) -> None:
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
