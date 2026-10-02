"""Cache directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Paths:
    cache: Path

    @classmethod
    def default(cls) -> Paths:
        """`~/Library/Caches/beans-picker`."""
        return cls(cache=Path.home() / "Library" / "Caches" / "beans-picker")

    @property
    def axtext(self) -> Path:
        """The exact-text helper app and its temporary output files."""
        return self.cache / "axtext"

    @property
    def shots(self) -> Path:
        """Temporary window screenshots (always deleted after use)."""
        return self.cache / "shots"

    @property
    def menukeys(self) -> Path:
        """The menu key helper and the learned key tables."""
        return self.cache / "menukeys"

    @property
    def activate(self) -> Path:
        """The helper that puts the app that was in front back in front."""
        return self.cache / "activate"

    @property
    def winrec(self) -> Path:
        """The window recorder."""
        return self.cache / "winrec"
