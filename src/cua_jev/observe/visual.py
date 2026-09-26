"""Pixel observation of a background window.

Some effects never reach the accessibility tree that cua-driver reports: a checkbox's state, a
selection highlight, a font weight. cua-driver captures an inactive window's pixels without raising
it, so those effects can still be observed rather than assumed. Every function here answers
"unknown" (`None`) when the pixels cannot be compared, never "unchanged".
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cua_jev._numbers import round_half_up
from cua_jev.driver.mcp import Driver
from cua_jev.driver.types import Frame, GetWindowStateArgs, WindowBounds, bounds_of, get_num, windows_of
from cua_jev.observe.png import Rgba, crop, decode_png
from cua_jev.paths import Paths

type Box = tuple[int, int, int, int]
"""A box in region pixels, `(x0, y0, x1, y1)`, the far edges excluded."""

# Brightness difference (0..255) above which a pixel counts as changed.
_LUMA_DELTA: Final = 24
# A caret is one point wide: two or three pixels on a Retina capture, plus antialiasing.
_CARET_COLUMNS: Final = 6
_SIGNIFICANT_PIXELS: Final = 40


@dataclass(frozen=True, slots=True, kw_only=True)
class Shot:
    """A window capture."""

    img: Rgba
    bounds: WindowBounds
    scale: float
    """Screenshot pixels per screen point."""
    pointer: list[Frame]
    """Screen-point boxes the mouse pointer may cover in this capture. The pointer is drawn into
    window captures, so these pixels say nothing about the app and are left out of comparisons."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PixelChange:
    changed: int
    """Pixels whose brightness changed noticeably."""
    columns: int
    """Distinct pixel columns among them."""
    caret_only: bool
    """Only a narrow vertical strip changed: a blinking or moved caret, not an effect."""
    significant: bool
    """A change worth reporting as an effect."""


async def capture_window(driver: Driver, pid: int, window_id: int, *, paths: Paths) -> Shot | None:
    """A screenshot of one window, taken without raising or activating it; `None` when unavailable."""
    try:
        out = _shot_file(paths, pid, window_id)
    except OSError:
        return None
    try:
        p1 = await _pointer_box(driver)
        args: GetWindowStateArgs = {
            "pid": pid,
            "window_id": window_id,
            "max_elements": 1,
            "screenshot_out_file": str(out),
        }
        r = await driver.call("get_window_state", args)
        p2 = await _pointer_box(driver)
        if not r.ok:
            return None
        given = r.data.get("window_bounds")
        bounds = bounds_of(given) if given is not None else await window_bounds(driver, pid, window_id)
        if bounds is None or not bounds.width or p1 is None or p2 is None:
            return None
        # Decoding a full window takes long enough to stall every other request, so it runs aside.
        img = await asyncio.to_thread(_read_png, _screenshot_path(r.data.get("screenshot_file_path"), out), out)
        if img is None:
            return None
        return Shot(img=img, bounds=bounds, scale=img.width / bounds.width, pointer=[p1, p2])
    except Exception:
        return None
    finally:
        out.unlink(missing_ok=True)


def _shot_file(paths: Paths, pid: int, window_id: int) -> Path:
    paths.shots.mkdir(parents=True, exist_ok=True)
    return paths.shots / f"{pid}-{window_id}-{os.getpid()}-{time.time_ns() // 1_000_000}.png"


def _screenshot_path(given: object, out: Path) -> Path:
    """Where cua-driver wrote the screenshot: the requested file unless it names another."""
    if given is None:
        return out
    if not isinstance(given, str):
        raise TypeError("screenshot_file_path is not a string")
    return Path(given)


def _read_png(path: Path, out: Path) -> Rgba | None:
    """Decodes the screenshot; a file cua-driver wrote somewhere else than asked is removed."""
    img = decode_png(path.read_bytes())
    if path != out:
        path.unlink(missing_ok=True)
    return img


async def window_bounds(driver: Driver, pid: int, window_id: int) -> WindowBounds | None:
    """The window's frame in screen points, from the window list (get_window_state can leave it out)."""
    r = await driver.call("list_windows", {"pid": pid})
    windows = windows_of(r.data) if r.ok else None
    own = next((w for w in windows or [] if w.window_id == window_id), None)
    return own.bounds if own is not None else None


async def _pointer_box(driver: Driver) -> Frame | None:
    """Where the pointer's image can be: generous enough for the arrow (drawn down-right of its tip)
    and the I-beam (centred)."""
    r = await driver.call("get_cursor_position", {})
    if not r.ok:
        return None
    x, y = get_num(r.data, "x"), get_num(r.data, "y")
    if x is None or y is None:
        return None
    return Frame(x=x - 14, y=y - 18, w=40, h=48)


def region_of(shot: Shot, frame: Frame) -> Rgba:
    """The pixels of a screen-point frame inside the shot, clamped to the window."""
    b, s = shot.bounds, shot.scale
    return crop(shot.img, (frame.x - b.x) * s, (frame.y - b.y) * s, frame.w * s, frame.h * s)


def pointer_mask(frame: Frame, *shots: Shot) -> list[Box]:
    """The pointer boxes of the given shots, in the pixels of `frame`'s region of the first shot."""
    b, s = shots[0].bounds, shots[0].scale
    ox = max(0, round_half_up((frame.x - b.x) * s))
    oy = max(0, round_half_up((frame.y - b.y) * s))
    return [
        (
            math.floor((p.x - b.x) * s) - ox,
            math.floor((p.y - b.y) * s) - oy,
            math.ceil((p.x + p.w - b.x) * s) - ox,
            math.ceil((p.y + p.h - b.y) * s) - oy,
        )
        for shot in shots
        for p in shot.pointer
    ]


def region_change(a: Shot, b: Shot, frame: Frame) -> PixelChange | None:
    """Pixel change inside `frame` between two shots, with both shots' pointer boxes left out.

    `None` when the window moved or the captures differ in scale.
    """
    if abs(a.scale - b.scale) > 1e-6 or a.bounds.x != b.bounds.x or a.bounds.y != b.bounds.y:
        return None
    return pixel_change(region_of(a, frame), region_of(b, frame), pointer_mask(frame, a, b))


def pixel_change(a: Rgba, b: Rgba, mask: Sequence[Box] = ()) -> PixelChange | None:
    """Compares two equally sized regions, skipping masked boxes; `None` when their sizes differ
    (the window moved or resized) or they are empty."""
    if a.width != b.width or a.height != b.height or not a.width or not a.height:
        return None
    width = a.width
    da, db = a.data, b.data
    changed = 0
    cols = bytearray(width)
    for p in range(width * a.height):
        x, y = p % width, p // width
        if mask and any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in mask):
            continue
        i = p * 4
        luma_a = 0.299 * da[i] + 0.587 * da[i + 1] + 0.114 * da[i + 2]
        luma_b = 0.299 * db[i] + 0.587 * db[i + 1] + 0.114 * db[i + 2]
        if abs(luma_a - luma_b) > _LUMA_DELTA:
            changed += 1
            cols[x] = 1
    columns = sum(cols)
    caret_only = changed > 0 and columns <= _CARET_COLUMNS
    return PixelChange(
        changed=changed,
        columns=columns,
        caret_only=caret_only,
        significant=not caret_only and changed >= _SIGNIFICANT_PIXELS,
    )
