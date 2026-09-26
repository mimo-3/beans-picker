"""Pixel fast path: an AX press costs about 2.5 s per click, a pixel click about 0.2 s."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, TypedDict

from cua_jev._aio import Clock
from cua_jev._numbers import round_half_up

if TYPE_CHECKING:
    from cua_jev.driver.types import ToolResult
    from cua_jev.observe.types import Snapshot, UINode

GEOMETRY_TTL_S: Final = 3.0
"""How long a measured window geometry is reused while the window's shape looks the same."""


class ToolCaller(Protocol):
    """The part of a cua-driver connection that acting needs (`driver.mcp.Driver` has it)."""

    async def call(self, tool: str, args: Mapping[str, object] | None = None) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class Point:
    """A window-local screenshot pixel."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class _Bounds:
    x: float
    y: float
    width: float
    height: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.width and self.y <= y <= self.y + self.height


@dataclass(frozen=True, slots=True, kw_only=True)
class _Window:
    window_id: int | None
    pid: int | None
    bounds: _Bounds | None
    on_screen: bool
    z_index: float | None
    layer: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Geometry:
    bounds: _Bounds
    scale: float
    z: float
    layer: float
    others: tuple[_Window, ...]


@dataclass(frozen=True, slots=True)
class _Cached:
    at: float
    shape: str
    geometry: _Geometry | None


class _WindowStateArgs(TypedDict):
    pid: int
    window_id: int
    max_elements: int


class PixelMapper:
    """Maps a node's centre to a screenshot pixel of its window, when a pixel click there is safe."""

    def __init__(self, driver: ToolCaller, *, clock: Clock = time.monotonic) -> None:
        self._driver = driver
        self._clock = clock
        self._geometry: dict[int, _Cached] = {}

    async def point(self, snap: Snapshot, node: UINode) -> Point | None:
        """The window-local pixel of the node's centre, or None when the pixel path is unsafe."""
        f = node.frame
        if f is None or f.w <= 1 or f.h <= 1:
            return None
        g = await self._geometry_of(snap)
        if g is None:
            return None
        cx = f.x + f.w / 2
        cy = f.y + f.h / 2
        if not g.bounds.contains(cx, cy):
            return None
        if any(_covers(w, g, cx, cy) for w in g.others):
            return None
        return Point(round_half_up((cx - g.bounds.x) * g.scale), round_half_up((cy - g.bounds.y) * g.scale))

    async def _geometry_of(self, snap: Snapshot) -> _Geometry | None:
        # Sheets or new windows can cover the point, so the cache holds only while the window set is unchanged.
        shape = window_shape(snap)
        cached = self._geometry.get(snap.window_id)
        if cached is not None and cached.shape == shape and self._clock() - cached.at < GEOMETRY_TTL_S:
            return cached.geometry
        try:
            g = await self._measure(snap)
        except Exception:
            g = None
        self._geometry[snap.window_id] = _Cached(self._clock(), shape, g)
        return g

    async def _measure(self, snap: Snapshot) -> _Geometry | None:
        listed = await self._driver.call("list_windows", {})
        if not listed.ok:
            return None
        windows = _windows(listed.data.get("windows"))
        me = next((w for w in windows if w.window_id == snap.window_id), None)
        if me is None or me.bounds is None or not me.on_screen or me.z_index is None:
            return None
        shot = await self._driver.call(
            "get_window_state", _WindowStateArgs(pid=snap.pid, window_id=snap.window_id, max_elements=1)
        )
        if not shot.ok:
            return None
        width = _number(shot.data.get("screenshot_width"))
        if not width:
            return None
        reported = shot.data.get("window_bounds")
        frame = _bounds(reported) if reported is not None else me.bounds
        if frame is None:
            return None
        scale = width / frame.width
        # Pop-up lists float above our window, so every on-screen window of the app counts, whatever its level.
        others = tuple(
            w
            for w in windows
            if w.window_id != snap.window_id and w.pid == snap.pid and (w.layer if w.layer is not None else 0) >= 0
        )
        return _Geometry(
            bounds=me.bounds,
            scale=scale,
            z=me.z_index,
            layer=me.layer if me.layer is not None else 0,
            others=others,
        )


def window_shape(snap: Snapshot) -> str:
    """What identifies the window set for the geometry cache: title, modal and the app's other windows."""
    modal = f"{snap.modal.role}:{snap.modal.label}" if snap.modal is not None else "-"
    others = snap.app_windows if snap.app_windows is not None else []
    return "\x01".join([snap.window_title, modal, *others])


def _covers(w: _Window, g: _Geometry, x: float, y: float) -> bool:
    if not w.on_screen or w.bounds is None or not w.bounds.contains(x, y):
        return False
    layer = w.layer if w.layer is not None else 0
    z = w.z_index if w.z_index is not None else -1
    return layer > g.layer or z > g.z


def _number(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return v


def _int(v: object) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _bounds(v: object) -> _Bounds | None:
    if not isinstance(v, Mapping):
        return None
    x, y, w, h = (_number(v.get(k)) for k in ("x", "y", "width", "height"))
    if x is None or y is None or w is None or h is None:
        return None
    return _Bounds(x, y, w, h)


def _windows(v: object) -> list[_Window]:
    if not isinstance(v, Sequence) or isinstance(v, str):
        return []
    out: list[_Window] = []
    for item in v:
        if not isinstance(item, Mapping):
            continue
        out.append(
            _Window(
                window_id=_int(item.get("window_id")),
                pid=_int(item.get("pid")),
                bounds=_bounds(item.get("bounds")),
                on_screen=item.get("is_on_screen") is True,
                z_index=_number(item.get("z_index")),
                layer=_number(item.get("layer")),
            )
        )
    return out
