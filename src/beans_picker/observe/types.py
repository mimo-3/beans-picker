"""The observed state of one window: its controls, visible text, menu bar and modal state."""

from __future__ import annotations

from dataclasses import dataclass, field

from beans_picker.driver.types import Frame


@dataclass(slots=True, kw_only=True)
class UINode:
    """One indexed element of the window (menu-bar elements are kept apart)."""

    index: int
    """cua-driver's element index in this snapshot."""
    token: str
    """cua-driver's element token, or `""`."""
    role: str
    label: str
    """Display name: the label, else the humanized identifier, else a short help, else the value."""
    enabled: bool
    actions: list[str]
    """Accessibility actions without the `AX` prefix, lowercased (`press`, `showmenu`)."""
    depth: float
    in_menu_bar: bool
    within: list[str]
    """Nearest named container ancestors, innermost first, at most three (`"AXSheet: Save"`)."""
    key: str
    """Identity that stays the same across snapshots."""
    subrole: str | None = None
    raw_label: str | None = None
    title: str | None = None
    value: str | None = None
    """The value, normalized."""
    raw_value: str | None = None
    """The value as reported, or the exact field text once the exact-text reader supplied it."""
    exact: bool | None = None
    """Whether `raw_value` is the exact field text."""
    placeholder: str | None = None
    """What cua-driver showed in a field the exact-text reader found empty and untitled: its
    placeholder, which cua-driver also gives as the field's label."""
    document: str | None = None
    """File URL of the window's document."""
    identifier: str | None = None
    help: str | None = None
    selected: bool | None = None
    frame: Frame | None = None
    parent: int | None = None


@dataclass(slots=True, kw_only=True)
class TextNode:
    """A static text of the window, indexed or not."""

    role: str
    value: str
    """Normalized text."""
    raw: str
    """Text as reported."""
    depth: float
    parent_index: int | None = None


@dataclass(slots=True, kw_only=True)
class MenuItem:
    """A menu-bar item addressed by its path (`["File", "Save"]`); unindexed rows are disabled."""

    path: list[str]
    enabled: bool
    key: str
    index: int | None = None
    token: str | None = None
    identifier: str | None = None


@dataclass(slots=True, kw_only=True)
class Modal:
    """A sheet, dialog, popover or open menu that owns the window's input."""

    role: str
    label: str
    index: int


@dataclass(slots=True, kw_only=True)
class Snapshot:
    """One observation of a window."""

    id: str
    pid: int
    window_id: int
    app_name: str
    window_title: str
    nodes: list[UINode]
    """Indexed elements outside the menu bar."""
    texts: list[TextNode]
    menu: list[MenuItem]
    signature: str
    taken_at: float
    """Epoch milliseconds."""
    ms: int
    """How long the observation took, in milliseconds."""
    modal: Modal | None = None
    frontmost: bool | None = None
    """Whether the window is the frontmost ordinary window; `None` when unknown."""
    app_windows: list[str] | None = field(default=None)
    """Titles of the app's other on-screen windows."""
