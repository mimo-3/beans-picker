"""Identity of controls and menu items across snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from beans_picker._json import dumps


class KeyParts(Protocol):
    @property
    def role(self) -> str: ...
    @property
    def identifier(self) -> str | None: ...
    @property
    def label(self) -> str: ...
    @property
    def within(self) -> Sequence[str]: ...


def key_of(role: str, identifier: str | None, label: str, within: Sequence[str]) -> str:
    """A structured identity; application labels cannot inject field delimiters."""
    return dumps([role, identifier, label, list(within[:3])])


def stable_key(n: KeyParts) -> str:
    """The node's identity: role, identifier, label and nearest named containers."""
    return key_of(n.role, n.identifier, n.label, n.within)


def menu_key(path: Sequence[str]) -> str:
    """A menu path with unambiguous component boundaries."""
    return dumps(["menu", list(path)])


def ambiguous_key(key: str, token: str, index: int) -> str:
    """Duplicate identities are scoped to their observed token, never an ordinal rank."""
    return dumps(["ambiguous", key, token, index])


def is_ambiguous_key(key: str) -> bool:
    """Whether a key cannot safely be rebound after its token becomes stale."""
    return key.startswith('["ambiguous",')
