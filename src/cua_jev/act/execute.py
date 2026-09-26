"""Maps a chosen candidate onto cua-driver tools without ever activating or raising the app."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, NotRequired, Protocol, TypedDict, assert_never

from cua_jev._aio import Clock
from cua_jev._json import quote
from cua_jev._numbers import round_half_up
from cua_jev.act.pixel import PixelMapper, ToolCaller
from cua_jev.menus.keyequiv import key_equivalent
from cua_jev.observe.normalize import normalize_text
from cua_jev.observe.snapshot import in_web_area, is_descendant

if TYPE_CHECKING:
    from cua_jev.candidates.types import ActionCandidate
    from cua_jev.driver.types import ToolResult
    from cua_jev.menus.menukeys import MenuKeyTable
    from cua_jev.observe.types import Snapshot, UINode

type SnapshotFn = Callable[[], Awaitable[Snapshot]]
type PathState = Literal["enabled", "disabled", "gone"]


class MenuKeyLearner(Protocol):
    """Learns an app's own menu key equivalents (`menus.menukeys.MenuKeys` implements it)."""

    async def learn(self, pid: int) -> MenuKeyTable | None: ...


@dataclass(slots=True, kw_only=True)
class ActResult:
    """What carrying out one candidate did."""

    ok: bool
    route: list[str]
    ms: int
    code: str | None = None
    effect: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Outcome:
    ok: bool
    code: str | None = None
    effect: str | None = None
    detail: str | None = None


class _ElementArgs(TypedDict):
    pid: int
    element_token: str


class _PointArgs(TypedDict):
    pid: int
    window_id: int
    x: int
    y: int
    modifier: NotRequired[list[str]]


class _WindowElementArgs(TypedDict):
    pid: int
    window_id: int
    element_token: str


class _SetValueArgs(TypedDict):
    pid: int
    element_token: str
    value: str


class _TypeTextArgs(TypedDict):
    pid: int
    element_token: str
    text: str


class _ScrollArgs(TypedDict):
    pid: int
    window_id: int
    element_token: str
    direction: str
    by: str
    amount: int


class _PressKeyArgs(TypedDict):
    pid: int
    window_id: int
    element_token: NotRequired[str]
    key: str
    modifiers: NotRequired[list[str]]


class _HotkeyArgs(TypedDict):
    pid: int
    window_id: int
    keys: list[str]


type _ArgsFor = Callable[[str], Mapping[str, object]]

_AX_FAILED: Final = re.compile(r"AX action failed", re.IGNORECASE | re.ASCII)
_STALE_MESSAGE: Final = re.compile(r"not found in cache|call get_window_state first", re.IGNORECASE | re.ASCII)
_KEYPAD_REBINDS: Final = 2


class Executor:
    """Carries out candidates on one window through cua-driver."""

    def __init__(
        self,
        driver: ToolCaller,
        reobserve: SnapshotFn,
        *,
        menu_keys: MenuKeyLearner,
        clock: Clock = time.perf_counter,
    ) -> None:
        self._driver = driver
        self._reobserve = reobserve
        self._menu_keys = menu_keys
        self._clock = clock
        self._pixels = PixelMapper(driver)

    async def execute(self, c: ActionCandidate, snap: Snapshot, modifiers: Sequence[str] = ()) -> ActResult:
        """Carries out `c` on the window `snap` shows."""
        t0 = self._clock()
        route: list[str] = []
        if modifiers:
            out = await self._modified_click(_target(c), snap, modifiers, route)
        else:
            out = await self._dispatch(c, snap, route)
        return self._result(out, route, t0)

    async def retype_field(self, c: ActionCandidate, snap: Snapshot) -> ActResult:
        """Retypes a web page's text field as a person would, for when an AXValue write did not reach the page."""
        t0 = self._clock()
        route: list[str] = []
        out = await self._retype(c, snap, route)
        return self._result(out, route, t0)

    def _result(self, out: _Outcome, route: list[str], t0: float) -> ActResult:
        ms = round_half_up((self._clock() - t0) * 1000)
        return ActResult(ok=out.ok, route=route, ms=ms, code=out.code, effect=out.effect, detail=out.detail)

    async def _dispatch(self, c: ActionCandidate, snap: Snapshot, route: list[str]) -> _Outcome:
        pid, window_id = snap.pid, snap.window_id
        match c.kind:
            case "click":
                target = _target(c)
                if in_popup_menu(snap, target):
                    return await self._press_item(target, snap, route)
                return await self._press(target, snap, route)
            case "toggle":
                return await self._press(_target(c), snap, route)
            case "context_menu":
                # AXShowMenu delivers a contextmenu event without activating the window.
                return await self._with_rebind(
                    _target(c),
                    route,
                    "right_click",
                    lambda token: _WindowElementArgs(pid=pid, window_id=window_id, element_token=token),
                )
            case "keypad":
                return await self._keypad(c, snap, route)
            case "choose_option":
                return await self._choose(c, snap, route)
            case "set_value":
                target = _target(c)
                if target.role == "AXIncrementor" and in_web_area(snap, target):
                    return await self._retype(c, snap, route)
                value = c.text if c.text is not None else ""
                return await self._with_rebind(
                    target,
                    route,
                    "set_value",
                    lambda token: _SetValueArgs(pid=pid, element_token=token, value=value),
                )
            case "type_into":
                text = c.text if c.text is not None else ""
                return await self._with_rebind(
                    _target(c),
                    route,
                    "type_text",
                    lambda token: _TypeTextArgs(pid=pid, element_token=token, text=text),
                )
            case "append":
                return await self._append(c, route)
            case "menu":
                return await self._menu(c, snap, route)
            case "scroll":
                # A background wheel event is the only way to move a nested scroller on a web page.
                direction = c.direction if c.direction is not None else "down"
                return await self._with_rebind(
                    _target(c),
                    route,
                    "scroll",
                    lambda token: _ScrollArgs(
                        pid=pid, window_id=window_id, element_token=token, direction=direction, by="page", amount=1
                    ),
                )
            case "key":
                return await self._key(c, snap, route)
            case _:
                assert_never(c.kind)

    async def _key(self, c: ActionCandidate, snap: Snapshot, route: list[str]) -> _Outcome:
        keys = c.keys
        if not keys:
            raise ValueError(f"key candidate {c.id} has no keys")
        key, modifiers = keys[-1], list(keys[:-1])

        def payload(token: str | None) -> _PressKeyArgs:
            if token is None:
                args = _PressKeyArgs(pid=snap.pid, window_id=snap.window_id, key=key)
            else:
                args = _PressKeyArgs(pid=snap.pid, window_id=snap.window_id, element_token=token, key=key)
            if modifiers:
                args["modifiers"] = modifiers
            return args

        if c.target is not None:
            return await self._with_rebind(c.target, route, "press_key", payload)
        route.append("press_key")
        return to_act(await self._driver.call("press_key", payload(None)))

    async def _press(self, node: UINode, snap: Snapshot, route: list[str]) -> _Outcome:
        r = await self._with_rebind(node, route, "click", lambda token: _ElementArgs(pid=snap.pid, element_token=token))
        # Controls without AXPress (text fields, custom views) still respond to a click at their centre.
        if not r.ok and _AX_FAILED.search(r.detail if r.detail is not None else ""):
            # AXPress can report failure after the app did act on it; a second press would repeat it.
            after = await self._reobserve()
            if after.signature != snap.signature:
                route.append("ax_acted")
                return _Outcome(ok=True, effect="unverifiable")
            pt = await self._pixels.point(snap, node)
            if pt is not None:
                route.append("pixel")
                return to_act(
                    await self._driver.call("click", _PointArgs(pid=snap.pid, window_id=snap.window_id, x=pt.x, y=pt.y))
                )
        return r

    async def _modified_click(
        self, node: UINode, snap: Snapshot, modifiers: Sequence[str], route: list[str]
    ) -> _Outcome:
        # AX actions carry no modifiers, so a modified click is a pixel click posted to the pid.
        pt = await self._pixels.point(snap, node)
        if pt is None:
            return _Outcome(
                ok=False,
                code="not_visible",
                detail="a click with modifier keys is a pixel click, and this control is not visible on the window "
                "(scroll it into view)",
            )
        route.extend(["pixel", "mod:" + "+".join(modifiers)])
        args = _PointArgs(pid=snap.pid, window_id=snap.window_id, x=pt.x, y=pt.y, modifier=list(modifiers))
        return to_act(await self._driver.call("click", args))

    async def _keypad(self, c: ActionCandidate, snap: Snapshot, route: list[str]) -> _Outcome:
        # Keys go by pixel where safely visible: an AX press costs about 2.5 s a key.
        presses = c.presses if c.presses is not None else []
        current = snap
        rebinds = 0
        i = 0
        while i < len(presses):
            # After a re-observe the key may have moved: its frame comes from the fresh snapshot too.
            want = presses[i]
            node = next((n for n in current.nodes if n.key == want.key), want)
            pt = await self._pixels.point(current, node)
            r: ToolResult
            if pt is not None:
                r = await self._driver.call(
                    "click", _PointArgs(pid=current.pid, window_id=current.window_id, x=pt.x, y=pt.y)
                )
            else:
                r = await self._driver.call("click", _ElementArgs(pid=current.pid, element_token=node.token))
            route.append("pixel" if pt is not None else "ax")
            if not r.ok:
                if not is_stale(r) or rebinds >= _KEYPAD_REBINDS:
                    return _Outcome(ok=False, code=r.code, detail=f"key {i + 1}/{len(presses)}: {r.message}")
                current = await self._reobserve()
                rebinds += 1
                route.append("rebind")
                continue
            i += 1
        return _Outcome(ok=True, effect="unverifiable")

    async def _choose(self, c: ActionCandidate, snap: Snapshot, route: list[str]) -> _Outcome:
        # A closed pop-up button has no AX children: press it open, then press the item titled `text`.
        target = _target(c)
        opened = await self._press(target, snap, route)
        if not opened.ok:
            return opened
        fresh = await self._reobserve()
        popup = next((n for n in fresh.nodes if n.key == target.key), None)
        items = (
            [
                n
                for n in fresh.nodes
                if n.role == "AXMenuItem" and not n.in_menu_bar and is_descendant(fresh, n, popup.index)
            ]
            if popup is not None
            else []
        )
        want = normalize_text(c.text)
        item = next((n for n in items if n.label == want), None)
        if item is not None:
            return await self._press_item(item, snap, route)
        if not items:
            return _Outcome(ok=False, code="option_not_found", detail="pressing the pop-up opened no menu")
        route.append("press_key")
        await self._driver.call("press_key", _PressKeyArgs(pid=snap.pid, window_id=snap.window_id, key="escape"))
        asked = quote(c.text if c.text is not None else "")
        titles = ", ".join(quote(n.label) for n in items)
        return _Outcome(ok=False, code="option_not_found", detail=f"the pop-up has no item {asked}; it lists {titles}")

    async def _press_item(self, item: UINode, snap: Snapshot, route: list[str]) -> _Outcome:
        r = await self._with_rebind(item, route, "click", lambda token: _ElementArgs(pid=snap.pid, element_token=token))
        # Some browsers leave the native menu open after the choice; Escape closes it and keeps the choice.
        if r.ok:
            after = await self._reobserve()
            if after.modal is not None and after.modal.role == "AXMenu":
                route.append("press_key")
                await self._driver.call(
                    "press_key", _PressKeyArgs(pid=snap.pid, window_id=snap.window_id, key="escape")
                )
        return r

    async def _menu(self, c: ActionCandidate, snap: Snapshot, route: list[str]) -> _Outcome:
        # cua-driver refuses AX presses on the menu bar (it belongs to the frontmost app), so the
        # command goes to our pid as its keyboard shortcut.
        m = c.menu
        if m is None:
            raise ValueError(f"menu candidate {c.id} has no menu item")
        path = m.path
        eq = key_equivalent(path, snap.menu, await self._menu_keys.learn(snap.pid))
        if eq is None:
            return _Outcome(
                ok=False,
                code="foreground_required",
                detail=f'"{" > ".join(path)}" has no keyboard shortcut, so it cannot run in the background',
            )
        fresh = await self._reobserve()
        match path_state(fresh, path):
            case "gone":
                return _Outcome(ok=False, code="menu_item_gone", detail=f"{' > '.join(path)} not found")
            case "disabled":
                return _Outcome(
                    ok=False,
                    code="menu_item_disabled",
                    detail=f"{' > '.join(path)} is disabled (the app has no key window for it in the background)",
                )
            case "enabled":
                pass
        keys = list(eq.keys)
        route.extend(["hotkey", "bg:" + "+".join(keys)])
        return to_act(await self._driver.call("hotkey", _HotkeyArgs(pid=snap.pid, window_id=snap.window_id, keys=keys)))

    async def _append(self, c: ActionCandidate, route: list[str]) -> _Outcome:
        # Insert at the end (cmd+down) so trimmed whitespace and rich-text formatting survive.
        fresh = await self._reobserve()
        key = c.target.key if c.target is not None else None
        node = next((n for n in fresh.nodes if n.key == key), None)
        if node is None:
            return _Outcome(
                ok=False, code="target_gone", detail=f"could not rebind {key if key is not None else 'undefined'}"
            )
        current = node.raw_value if node.raw_value is not None else node.value if node.value is not None else ""
        text = c.text if c.text is not None else ""
        route.append("press_key")
        caret = await self._driver.call(
            "press_key",
            _PressKeyArgs(
                pid=fresh.pid, window_id=fresh.window_id, element_token=node.token, key="down", modifiers=["cmd"]
            ),
        )
        if caret.ok:
            return await self._with_rebind(
                node, route, "type_text", lambda token: _TypeTextArgs(pid=fresh.pid, element_token=token, text=text)
            )
        if not node.exact:
            return _Outcome(
                ok=False,
                code="caret_refused",
                detail=f"{caret.message}; the field's exact text is unknown, so it is not rewritten",
            )
        route.append("set_value")
        return to_act(
            await self._driver.call(
                "set_value", _SetValueArgs(pid=fresh.pid, element_token=node.token, value=current + text)
            )
        )

    async def _retype(self, c: ActionCandidate, snap: Snapshot, route: list[str]) -> _Outcome:
        # Some web fields ignore an AXValue write, so the text is retyped as a person would.
        target = _target(c)

        async def key(k: str, modifiers: Sequence[str] = ()) -> _Outcome:
            mods = list(modifiers)
            return await self._with_rebind(
                target,
                route,
                "press_key",
                lambda token: _PressKeyArgs(
                    pid=snap.pid, window_id=snap.window_id, element_token=token, key=k, modifiers=mods
                ),
            )

        for k, mods in (("end", ()), ("home", ("shift",))):
            r = await key(k, mods)
            if not r.ok:
                return r
        text = c.text if c.text is not None else ""
        if not text:
            return await key("delete")
        return await self._with_rebind(
            target, route, "type_text", lambda token: _TypeTextArgs(pid=snap.pid, element_token=token, text=text)
        )

    async def _with_rebind(self, node: UINode, route: list[str], tool: str, args: _ArgsFor) -> _Outcome:
        route.append(tool)
        r = await self._driver.call(tool, args(node.token))
        if is_stale(r):
            fresh = await self._reobserve()
            again = next((n for n in fresh.nodes if n.key == node.key), None)
            if again is None:
                return _Outcome(ok=False, code="target_gone", detail=f"could not rebind {node.key}")
            route.append("rebind")
            r = await self._driver.call(tool, args(again.token))
        return to_act(r)


def is_stale(r: ToolResult) -> bool:
    """A refusal naming a token from an older snapshot."""
    if r.ok:
        return False
    return r.code == "stale_element_token" or _STALE_MESSAGE.search(r.message) is not None


def in_popup_menu(snap: Snapshot, node: UINode) -> bool:
    """Whether the node is an item of a pop-up button's own menu (not the menu bar)."""
    if node.role != "AXMenuItem" or node.in_menu_bar:
        return False
    by_index = {n.index: n for n in snap.nodes}
    seen: set[int] = set()
    cur = by_index.get(node.parent if node.parent is not None else -1)
    while cur is not None and cur.index not in seen:
        if cur.role == "AXPopUpButton":
            return True
        seen.add(cur.index)
        cur = by_index.get(cur.parent if cur.parent is not None else -1)
    return False


def path_state(snap: Snapshot, path: Sequence[str]) -> PathState:
    """Whether every level of a menu path below the menu bar title is present and enabled."""
    by_path = {"\x01".join(m.path): m for m in reversed(snap.menu)}
    for i in range(2, len(path) + 1):
        item = by_path.get("\x01".join(path[:i]))
        if item is None:
            return "gone"
        if not item.enabled:
            return "disabled"
    return "enabled"


def to_act(r: ToolResult) -> _Outcome:
    """A cua-driver result as an outcome; ok unless cua-driver reports the effect `failed`."""
    if not r.ok:
        return _Outcome(ok=False, code=r.code, detail=r.message)
    effect = r.data.get("effect")
    return _Outcome(ok=effect != "failed", effect=effect if isinstance(effect, str) and effect else None)


def _target(c: ActionCandidate) -> UINode:
    if c.target is None:
        raise ValueError(f"{c.kind} candidate {c.id} has no target")
    return c.target
