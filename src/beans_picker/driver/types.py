"""Data shapes received from cua-driver, the arguments sent to it, and the activation record of the foreground guard."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from beans_picker._json import JsonObject


def as_obj(value: object) -> Mapping[str, object] | None:
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return value
    return None


def get_str(d: Mapping[str, object], key: str) -> str | None:
    value = d.get(key)
    return value if isinstance(value, str) else None


def get_num(d: Mapping[str, object], key: str) -> int | float | None:
    """A number (int or float, never a bool)."""
    value = d.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def get_int(d: Mapping[str, object], key: str) -> int | None:
    """An integer; a float with an integral value counts (JSON does not tell `5` from `5.0`)."""
    value = get_num(d, key)
    if isinstance(value, int):
        return value
    if value is not None and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def get_bool(d: Mapping[str, object], key: str) -> bool | None:
    value = d.get(key)
    return value if isinstance(value, bool) else None


def get_list(d: Mapping[str, object], key: str) -> list[object] | None:
    value = d.get(key)
    return list(value) if isinstance(value, list) else None


def get_obj(d: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    return as_obj(d.get(key))


def get_str_list(d: Mapping[str, object], key: str) -> list[str] | None:
    """A list of strings; entries of another type are dropped."""
    items = get_list(d, key)
    return None if items is None else [s for s in items if isinstance(s, str)]


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """An element's frame in screen points."""

    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowBounds:
    x: float
    y: float
    width: float
    height: float


def frame_of(value: object) -> Frame | None:
    """A `{x, y, w, h}` object, or None when it is not one."""
    d = as_obj(value)
    if d is None:
        return None
    x, y, w, h = (get_num(d, k) for k in ("x", "y", "w", "h"))
    if x is None or y is None or w is None or h is None:
        return None
    return Frame(x=x, y=y, w=w, h=h)


def bounds_of(value: object) -> WindowBounds | None:
    """A `{x, y, width, height}` object, or None when it is not one."""
    d = as_obj(value)
    if d is None:
        return None
    x, y, width, height = (get_num(d, k) for k in ("x", "y", "width", "height"))
    if x is None or y is None or width is None or height is None:
        return None
    return WindowBounds(x=x, y=y, width=width, height=height)


@dataclass(frozen=True, slots=True, kw_only=True)
class Window:
    """One entry of `list_windows`' `windows`."""

    window_id: int
    pid: int
    app_name: str | None = None
    title: str | None = None
    bounds: WindowBounds | None = None
    z_index: int | None = None
    is_on_screen: bool | None = None
    layer: int | None = None
    on_current_space: bool | None = None


def window_of(value: object) -> Window | None:
    """A window record, or None when it has no integer `window_id` and `pid`."""
    d = as_obj(value)
    if d is None:
        return None
    window_id = get_int(d, "window_id")
    pid = get_int(d, "pid")
    if window_id is None or pid is None:
        return None
    return Window(
        window_id=window_id,
        pid=pid,
        app_name=get_str(d, "app_name"),
        title=get_str(d, "title"),
        bounds=bounds_of(d.get("bounds")),
        z_index=get_int(d, "z_index"),
        is_on_screen=get_bool(d, "is_on_screen"),
        layer=get_int(d, "layer"),
        on_current_space=get_bool(d, "on_current_space"),
    )


def windows_of(data: Mapping[str, object]) -> list[Window] | None:
    """The window records of a `list_windows` or `launch_app` result, or None without a list."""
    items = get_list(data, "windows")
    if items is None:
        return None
    return [w for w in map(window_of, items) if w is not None]


type ElementValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Element:
    """One entry of `get_window_state`'s `elements`."""

    element_index: int
    role: str
    element_token: str | None = None
    subrole: str | None = None
    label: str | None = None
    title: str | None = None
    value: ElementValue = None
    identifier: str | None = None
    help: str | None = None
    enabled: bool | None = None
    selected: bool | None = None
    actions: list[str] | None = None
    frame: Frame | None = None
    parent_index: int | None = None
    depth: int | None = None


def _element_value(d: Mapping[str, object]) -> ElementValue:
    value = d.get("value")
    return value if isinstance(value, str | int | float | bool) else None


def element_of(value: object) -> Element | None:
    """An element record, or None when it has no integer `element_index` or string `role`."""
    d = as_obj(value)
    if d is None:
        return None
    index = get_int(d, "element_index")
    role = get_str(d, "role")
    if index is None or role is None:
        return None
    return Element(
        element_index=index,
        role=role,
        element_token=get_str(d, "element_token"),
        subrole=get_str(d, "subrole"),
        label=get_str(d, "label"),
        title=get_str(d, "title"),
        value=_element_value(d),
        identifier=get_str(d, "identifier"),
        help=get_str(d, "help"),
        enabled=get_bool(d, "enabled"),
        selected=get_bool(d, "selected"),
        actions=get_str_list(d, "actions"),
        frame=frame_of(d.get("frame")),
        parent_index=get_int(d, "parent_index"),
        depth=get_int(d, "depth"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowState:
    snapshot_id: str | None = None
    app_name: str | None = None
    window_title: str | None = None
    window_bounds: WindowBounds | None = None
    elements: list[Element] | None = None
    tree_markdown: str | None = None
    screenshot_width: float | None = None
    screenshot_height: float | None = None
    screenshot_scale: float | None = None
    screenshot_file_path: str | None = None


def window_state_of(data: Mapping[str, object]) -> WindowState:
    """The fields of a `get_window_state` result (entries of `elements` that are not element records are skipped)."""
    items = get_list(data, "elements")
    elements = None if items is None else [e for e in map(element_of, items) if e is not None]
    return WindowState(
        snapshot_id=get_str(data, "snapshot_id"),
        app_name=get_str(data, "app_name"),
        window_title=get_str(data, "window_title"),
        window_bounds=bounds_of(data.get("window_bounds")),
        elements=elements,
        tree_markdown=get_str(data, "tree_markdown"),
        screenshot_width=get_num(data, "screenshot_width"),
        screenshot_height=get_num(data, "screenshot_height"),
        screenshot_scale=get_num(data, "screenshot_scale"),
        screenshot_file_path=get_str(data, "screenshot_file_path"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionResult:
    """What cua-driver reports about an action it carried out."""

    effect: str | None = None
    route: str | None = None
    delivery_mode: str | None = None
    escalation_reason: str | None = None
    escalation_target: str | None = None
    value_readback: object = None


def action_result_of(data: Mapping[str, object]) -> ActionResult:
    delivery = get_obj(data, "delivery") or {}
    escalation = get_obj(data, "escalation") or {}
    return ActionResult(
        effect=get_str(data, "effect"),
        route=get_str(data, "route"),
        delivery_mode=get_str(delivery, "mode"),
        escalation_reason=get_str(escalation, "reason"),
        escalation_target=get_str(escalation, "target"),
        value_readback=data.get("value_readback"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOk:
    """cua-driver carried out the call; `data` is its `structuredContent` (`{}` when absent)."""

    data: JsonObject
    text: str
    ms: int
    ok: Literal[True] = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolRefused:
    """cua-driver (or the background-only guard) refused the call."""

    code: str
    message: str
    data: JsonObject
    text: str
    ms: int
    ok: Literal[False] = False


type ToolResult = ToolOk | ToolRefused


class ListWindowsArgs(TypedDict, total=False):
    pid: int


class GetWindowStateArgs(TypedDict):
    pid: int
    window_id: int
    include_screenshot: NotRequired[bool]
    max_elements: NotRequired[int]
    max_depth: NotRequired[int]
    screenshot_out_file: NotRequired[str]


class LaunchAppArgs(TypedDict, total=False):
    bundle_id: str
    name: str


class EndSessionArgs(TypedDict):
    session: str


class ClickArgs(TypedDict):
    """An accessibility press or a pixel click; `modifier` is a list despite its singular name."""

    pid: int
    window_id: NotRequired[int]
    element_token: NotRequired[str]
    x: NotRequired[int]
    y: NotRequired[int]
    modifier: NotRequired[list[str]]


class RightClickArgs(TypedDict):
    pid: int
    window_id: int
    element_token: str


class SetValueArgs(TypedDict):
    pid: int
    element_token: str
    value: str


class TypeTextArgs(TypedDict):
    pid: int
    element_token: str
    text: str


class ScrollArgs(TypedDict):
    pid: int
    window_id: int
    element_token: str
    direction: str
    by: str
    amount: int


class PressKeyArgs(TypedDict):
    pid: int
    window_id: NotRequired[int]
    element_token: NotRequired[str]
    key: str
    modifiers: NotRequired[list[str]]


class HotkeyArgs(TypedDict):
    pid: int
    window_id: int
    keys: list[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class Activation:
    """The app under test was seen in front: when, and during which driver call."""

    pid: int
    during: str
    at: str
