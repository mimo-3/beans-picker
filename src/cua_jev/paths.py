"""Cache directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Paths:
    cache: Path

    @classmethod
    def default(cls) -> Paths:
        """`~/Library/Caches/cua-jev`."""
        return cls(cache=Path.home() / "Library" / "Caches" / "cua-jev")

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
