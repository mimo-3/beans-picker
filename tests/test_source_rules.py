from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "cua_jev"
PATTERN_FUNCTIONS = frozenset(
    {"compile", "search", "match", "fullmatch", "sub", "subn", "split", "findall", "finditer"}
)
BARE_CLASSES = frozenset("sSwWdD")


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _pattern_texts(node: ast.expr) -> Iterator[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value


def _bare_classes(pattern: str) -> list[str]:
    found: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\" and i + 1 < len(pattern):
            if pattern[i + 1] in BARE_CLASSES:
                found.append(pattern[i : i + 2])
            i += 2
        else:
            i += 1
    return found


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "round":
            out.append(f"{path.name}:{node.lineno}: builtin round")
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
            and func.attr in PATTERN_FUNCTIONS
            and node.args
        ):
            out.extend(
                f"{path.name}:{node.lineno}: {cls} in a pattern"
                for text in _pattern_texts(node.args[0])
                for cls in _bare_classes(text)
            )
    return out


def test_the_scan_sees_the_package() -> None:
    assert len(_sources()) > 40


@pytest.mark.parametrize(
    ("pattern", "found"),
    [
        (r"a\sb", [r"\s"]),
        (r"\W+\D", [r"\W", r"\D"]),
        (r"\\s", []),
        (r"[0-9]\.\n", []),
        ("\\", []),
    ],
)
def test_bare_classes_skip_escaped_backslashes(pattern: str, found: list[str]) -> None:
    assert _bare_classes(pattern) == found


def test_the_scan_catches_both_rules(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text('import re\nre.compile(r"\\d+")\nre.sub(rf"{x}\\w", "", s)\nround(1.5)\n', encoding="utf-8")
    assert _violations(sample) == [
        r"sample.py:2: \d in a pattern",
        r"sample.py:3: \w in a pattern",
        "sample.py:4: builtin round",
    ]


def test_patterns_spell_out_their_classes_and_nothing_calls_round() -> None:
    assert [v for path in _sources() for v in _violations(path)] == []
