"""Screen lock detection: while the login session's screen is locked, nothing is operated."""

from __future__ import annotations

import plistlib
from typing import Final

from beans_picker._proc import Runner, run
from beans_picker.errors import ToolError

_IOREG: Final = ("ioreg", "-n", "Root", "-d1", "-a")
_IOREG_MAX_BYTES: Final = 8 * 1024 * 1024


async def screen_locked(*, runner: Runner = run) -> bool:
    """True when the login session's screen is locked (`CGSSessionScreenIsLocked`); GUI input would go nowhere then."""
    try:
        out = await runner(_IOREG, max_bytes=_IOREG_MAX_BYTES)
        data = plistlib.loads(out.stdout.encode("utf-8"))
        if not isinstance(data, (dict, list)) or out.returncode != 0:
            raise ValueError("invalid lock state")
        pending: list[object] = [data]
        locked = False
        has_session = False
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if "IOConsoleUsers" in node:
                    sessions = node["IOConsoleUsers"]
                    if not isinstance(sessions, list) or not sessions or any(not isinstance(s, dict) for s in sessions):
                        raise ValueError("invalid session information")
                    has_session = True
                if "CGSSessionScreenIsLocked" in node:
                    value = node["CGSSessionScreenIsLocked"]
                    if not isinstance(value, bool):
                        raise ValueError("invalid lock state")
                    locked = locked or value
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        if not has_session:
            raise ValueError("missing session information")
        return locked
    except Exception:
        raise ToolError(
            "screen_lock_unavailable", "could not determine whether the screen is locked; nothing was run"
        ) from None
