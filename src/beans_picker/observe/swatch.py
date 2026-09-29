"""Colour swatches: small unnamed buttons of a web page told apart by the colour they show."""

from __future__ import annotations

import colorsys
from collections.abc import Sequence
from typing import Final

from beans_picker._numbers import round_half_up
from beans_picker.driver.types import Frame
from beans_picker.observe.snapshot import in_web_area
from beans_picker.observe.types import Snapshot, UINode
from beans_picker.observe.visual import Shot, region_of

SWATCH_ROLES: Final = frozenset({"AXButton", "AXRadioButton"})
PALETTE: Final = 3
"""Swatches come in a row: fewer unnamed square buttons under one parent are icons, not a palette."""
_MIN_SIDE: Final = 8
_MAX_SIDE: Final = 48
_SPOTS: Final = ((0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7))
"""Where a swatch is sampled: inside its border, around a check mark drawn at its centre."""
_SAME: Final = 24
"""Largest channel difference between spots that show one colour."""
_HUES: Final = (
    (15, "red"),
    (45, "orange"),
    (70, "yellow"),
    (165, "green"),
    (195, "teal"),
    (250, "blue"),
    (290, "purple"),
    (335, "pink"),
    (360, "red"),
)


def swatches(snap: Snapshot) -> list[UINode]:
    """Unnamed, small, square buttons of a web page in a row of at least PALETTE: the colour each shows is all
    there is to tell them apart."""
    rows: dict[int | None, list[UINode]] = {}
    for n in snap.nodes:
        if _looks_like_swatch(n) and in_web_area(snap, n):
            rows.setdefault(n.parent, []).append(n)
    return [n for row in rows.values() if len(row) >= PALETTE for n in row]


def _looks_like_swatch(n: UINode) -> bool:
    f = n.frame
    if n.role not in SWATCH_ROLES or n.label or f is None:
        return False
    return min(f.w, f.h) >= _MIN_SIDE and max(f.w, f.h) <= _MAX_SIDE and abs(f.w - f.h) <= max(f.w, f.h) / 4


def name_swatches(nodes: Sequence[UINode], shot: Shot) -> None:
    """Sets `colour` on each node whose spots agree on one colour."""
    for n in nodes:
        if n.frame is not None:
            n.colour = colour_at(shot, n.frame)


def colour_at(shot: Shot, frame: Frame) -> str | None:
    """`purple #8B5CF6`; `None` when the spots disagree, or show white, gray or black (an icon's background)."""
    img = region_of(shot, frame)
    if img.width == 0 or img.height == 0:
        return None
    spots: list[tuple[int, int, int]] = []
    for fx, fy in _SPOTS:
        x, y = min(img.width - 1, int(img.width * fx)), min(img.height - 1, int(img.height * fy))
        i = (y * img.width + x) * 4
        spots.append((img.data[i], img.data[i + 1], img.data[i + 2]))
    first = spots[0]
    if any(max(abs(a - b) for a, b in zip(first, s, strict=True)) > _SAME for s in spots[1:]):
        return None
    r, g, b = (round_half_up(sum(s[c] for s in spots) / len(spots)) for c in range(3))
    name = colour_name(r, g, b)
    return None if name in _PLAIN else f"{name} #{r:02X}{g:02X}{b:02X}"


_PLAIN: Final = frozenset({"white", "gray", "black"})


def colour_name(r: int, g: int, b: int) -> str:
    """A plain English name for an sRGB colour."""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < 0.2:
        return "black"
    if s < 0.15:
        return "white" if v > 0.9 else "gray"
    hue = h * 360
    return next(name for top, name in _HUES if hue < top)
