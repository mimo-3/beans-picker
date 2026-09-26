"""Fail unless every given wheel ships the native helper sources and the typing marker.

python scripts/check_wheel.py dist/*.whl
"""

from __future__ import annotations

import sys
import zipfile
from collections.abc import Sequence
from typing import Final

REQUIRED: Final = ("cua_jev/native/axtext.m", "cua_jev/native/menukeys.m", "cua_jev/py.typed")


def missing(wheel: str) -> list[str]:
    """The required members `wheel` lacks (empty or unreadable files count as missing)."""
    with zipfile.ZipFile(wheel) as z:
        sizes = {info.filename: info.file_size for info in z.infolist()}
    return [name for name in REQUIRED if name not in sizes or (name.endswith(".m") and sizes[name] == 0)]


def main(argv: Sequence[str]) -> int:
    if not argv:
        sys.stderr.write("usage: check_wheel.py <wheel> [<wheel> ...]\n")
        return 2
    failed = False
    for wheel in argv:
        lacks = missing(wheel)
        if lacks:
            failed = True
            sys.stderr.write(f"{wheel}: missing {', '.join(lacks)}\n")
        else:
            sys.stdout.write(f"{wheel}: ok\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
