"""A short hash of what matters for "did anything change" in a window."""

from __future__ import annotations

import hashlib

from cua_jev._json import quote
from cua_jev._text import hash_bytes
from cua_jev.observe.types import Snapshot

_SEP = "\x01"


def state_signature(s: Snapshot) -> str:
    """16 hex digits of the SHA-1 over the window title, the node count (in fives), the modal,
    every node value, the exact text of fields read exactly, selections, visible text and the app's
    other windows.

    Exact text is included quoted, so a line break or space at either end changes the signature
    even though the normalized value hides it.
    """
    parts: list[str] = [
        s.window_title,
        f"n{len(s.nodes) // 5}",
        f"{s.modal.role}:{s.modal.label}" if s.modal is not None else "-",
    ]
    parts.extend(f"{n.key}={n.value}" for n in s.nodes if n.value is not None)
    parts.extend(
        f"x:{n.key}={quote(n.raw_value) if n.raw_value is not None else 'undefined'}" for n in s.nodes if n.exact
    )
    parts.extend(f"sel:{n.key}" for n in s.nodes if n.selected)
    parts.extend(t.value for t in s.texts)
    parts.extend(f"win:{w}" for w in s.app_windows or [])
    digest = hashlib.sha1(hash_bytes(_SEP.join(parts)))  # noqa: S324 - a change detector, not security
    return digest.hexdigest()[:16]
