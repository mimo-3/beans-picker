"""Puts the app that was in front before back in front, after the app under test took its place."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from beans_picker._proc import Runner, run

_log = logging.getLogger(__name__)


class ActivateBuild(Protocol):
    """Where the activate helper comes from."""

    async def activate_bin(self) -> Path | None: ...


class Activator:
    """Activates one app by pid with the `activate` helper; never raises. Callers never pass the app under test."""

    def __init__(self, helpers: ActivateBuild, *, runner: Runner = run) -> None:
        self._helpers = helpers
        self._runner = runner

    async def __call__(self, pid: int) -> bool:
        """True when the system accepted the request."""
        try:
            binary = await self._helpers.activate_bin()
            if binary is None:
                return False
            await self._runner((str(binary), str(pid)))
        except Exception as err:
            _log.debug("activating pid %d failed: %s", pid, type(err).__name__)
            return False
        return True
