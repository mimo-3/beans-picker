"""Identity of controls and menu items across snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class KeyParts(Protocol):
    """What a stable key is made of."""

    @property
    def role(self) -> str: ...
    @property
    def identifier(self) -> str | None: ...
    @property
    def label(self) -> str: ...
    @property
    def within(self) -> Sequence[str]: ...


def key_of(role: str, identifier: str | None, label: str, within: Sequence[str]) -> str:
    """`role|identifier|label|ancestors`, ancestors (at most three) joined by `>`."""
    return "|".join((role, identifier if identifier is not None else "", label, ">".join(within[:3])))


def stable_key(n: KeyParts) -> str:
    """The node's identity: role, identifier, label and nearest named containers.

    `AXButton||OK|AXSheet: Save>AXWindow: Doc` for an OK button without identifier in a sheet.
    """
    return key_of(n.role, n.identifier, n.label, n.within)


def menu_key(path: Sequence[str]) -> str:
    """`AXMenuItem|menu|File > Save` for `["File", "Save"]`."""
    return "AXMenuItem|menu|" + " > ".join(path)
