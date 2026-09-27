from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from beans_picker._json import JsonObject, JsonValue
from beans_picker.candidates.types import ActionCandidate, ActionKind, ScrollDirection
from beans_picker.driver.types import Frame, ToolOk, ToolRefused, ToolResult
from beans_picker.menus.menukeys import MenuKeyTable
from beans_picker.observe.types import MenuItem, Modal, Snapshot, TextNode, UINode


def ok(data: JsonObject | None = None) -> ToolOk:
    return ToolOk(data=data if data is not None else {"effect": "confirmed"}, text="", ms=1)


def refused(code: str, message: str) -> ToolRefused:
    return ToolRefused(code=code, message=message, data={}, text="", ms=1)


def frame(x: float, y: float, w: float = 20, h: float = 20) -> Frame:
    return Frame(x=x, y=y, w=w, h=h)


def node(
    index: int,
    role: str,
    label: str = "",
    *,
    key: str | None = None,
    tok: str | None = None,
    parent: int | None = None,
    value: str | None = None,
    raw_value: str | None = None,
    exact: bool | None = None,
    frame: Frame | None = None,
    in_menu_bar: bool = False,
) -> UINode:
    return UINode(
        index=index,
        token=tok if tok is not None else f"t:{index}",
        role=role,
        label=label,
        enabled=True,
        actions=["press"],
        depth=1 if parent is None else 2,
        in_menu_bar=in_menu_bar,
        within=[],
        key=key if key is not None else f"{role}:{label}",
        value=value,
        raw_value=raw_value,
        exact=exact,
        frame=frame,
        parent=parent,
    )


def snap(
    nodes: Sequence[UINode] = (),
    *,
    signature: str = "sig",
    pid: int = 1,
    window_id: int = 1,
    title: str = "Window",
    menu: Sequence[MenuItem] = (),
    modal: Modal | None = None,
    texts: Sequence[TextNode] = (),
    app_windows: list[str] | None = None,
) -> Snapshot:
    return Snapshot(
        id="s1",
        pid=pid,
        window_id=window_id,
        app_name="App",
        window_title=title,
        nodes=list(nodes),
        texts=list(texts),
        menu=list(menu),
        signature=signature,
        taken_at=0.0,
        ms=1,
        modal=modal,
        app_windows=app_windows,
    )


def with_signature(s: Snapshot, signature: str) -> Snapshot:
    return replace(s, signature=signature)


def menu_item(*path: str, enabled: bool = True) -> MenuItem:
    return MenuItem(path=list(path), enabled=enabled, key=" > ".join(path))


def cand(
    kind: ActionKind,
    target: UINode | None = None,
    *,
    text: str | None = None,
    keys: list[str] | None = None,
    direction: ScrollDirection | None = None,
    presses: list[UINode] | None = None,
    menu: MenuItem | None = None,
) -> ActionCandidate:
    return ActionCandidate(
        id="c0000001",
        kind=kind,
        key=target.key if target is not None else kind,
        summary=kind,
        destructive=False,
        lexical=0,
        target=target,
        menu=menu,
        text=text,
        keys=keys,
        direction=direction,
        presses=presses,
    )


type Answer = ToolResult | Exception | Callable[[Mapping[str, object]], ToolResult]


class CallDriver:
    def __init__(self, scripts: Mapping[str, Sequence[Answer]] | None = None) -> None:
        self.scripts: dict[str, list[Answer]] = {k: list(v) for k, v in (scripts or {}).items()}
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]

    async def call(self, tool: str, args: Mapping[str, object] | None = None) -> ToolResult:
        payload = dict(args or {})
        self.calls.append((tool, payload))
        script = self.scripts.get(tool)
        if not script:
            return ok()
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, ToolOk | ToolRefused):
            return answer
        return answer(payload)


class Reobserve:
    def __init__(self, *snaps: Snapshot) -> None:
        self.snaps = list(snaps)
        self.count = 0

    async def __call__(self) -> Snapshot:
        self.count += 1
        if not self.snaps:
            raise AssertionError("no snapshot was expected")
        return self.snaps[min(self.count, len(self.snaps)) - 1]


class FakeMenuKeys:
    def __init__(self, table: MenuKeyTable | None = None) -> None:
        self.table = table
        self.asked: list[int] = []

    async def learn(self, pid: int) -> MenuKeyTable | None:
        self.asked.append(pid)
        return self.table


def windows_data(*windows: JsonObject) -> JsonObject:
    wins: list[JsonValue] = list(windows)
    return {"windows": wins}


def window(
    window_id: int,
    pid: int = 1,
    *,
    bounds: tuple[float, float, float, float] | None = (100, 50, 400, 300),
    on_screen: bool = True,
    z: int | None = 10,
    layer: int | None = 0,
) -> JsonObject:
    w: JsonObject = {"window_id": window_id, "pid": pid, "is_on_screen": on_screen}
    if bounds is not None:
        x, y, width, height = bounds
        w["bounds"] = {"x": x, "y": y, "width": width, "height": height}
    if z is not None:
        w["z_index"] = z
    if layer is not None:
        w["layer"] = layer
    return w


def geometry_driver(
    *others: JsonObject,
    screenshot_width: float = 800,
    me: JsonObject | None = None,
    window_bounds: JsonObject | None = None,
    scripts: Mapping[str, Sequence[Answer]] | None = None,
) -> CallDriver:
    shot: JsonObject = {"screenshot_width": screenshot_width}
    if window_bounds is not None:
        shot["window_bounds"] = window_bounds
    base: dict[str, Sequence[Answer]] = {
        "list_windows": [ok(windows_data(me if me is not None else window(1), *others))],
        "get_window_state": [ok(shot)],
    }
    base.update(scripts or {})
    return CallDriver(base)
