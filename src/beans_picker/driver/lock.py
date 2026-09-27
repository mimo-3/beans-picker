"""Screen lock detection: while the login session's screen is locked, nothing is operated."""

from __future__ import annotations

import re
from typing import Final

from beans_picker._proc import Runner, run
from beans_picker._text import WS

_IOREG: Final = ("ioreg", "-n", "Root", "-d1", "-a")
_IOREG_MAX_BYTES: Final = 8 * 1024 * 1024
_LOCKED: Final = re.compile(f"<key>CGSSessionScreenIsLocked</key>{WS}*<true/>")


async def screen_locked(*, runner: Runner = run) -> bool:
    """True when the login session's screen is locked (`CGSSessionScreenIsLocked`); GUI input would go nowhere then."""
    try:
        out = await runner(_IOREG, max_bytes=_IOREG_MAX_BYTES)
    except Exception:
        return False
    return _LOCKED.search(out.stdout) is not None
