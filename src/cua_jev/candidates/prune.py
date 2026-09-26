"""Lexical relevance of candidates to an instruction, and splitting them into Jev-sized shards."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Protocol

from cua_jev.candidates.types import ActionCandidate
from cua_jev.menus.keyequiv import english_title
from cua_jev.menus.menukeys import MenuKeyTable
from cua_jev.observe.normalize import tokenize

# Options per Jev question (the API takes 1-255; this keeps a request well inside its token budget).
MAX_OPTIONS: Final = 60

_QUOTED: Final = re.compile(
    '"([^"]+)"'
    "|\N{LEFT DOUBLE QUOTATION MARK}([^\N{RIGHT DOUBLE QUOTATION MARK}]+)\N{RIGHT DOUBLE QUOTATION MARK}"
    "|\N{LEFT CORNER BRACKET}([^\N{RIGHT CORNER BRACKET}]+)\N{RIGHT CORNER BRACKET}"
    "|'([^']+)'"
)
_SUMMARY_KINDS: Final = frozenset({"key", "scroll", "context_menu"})


class HasLexical(Protocol):
    """Anything ranked by a lexical score."""

    @property
    def lexical(self) -> float: ...


def quoted_spans(instruction: str) -> list[str]:
    """The quoted words of an instruction, lowercased ('click "Save"', 「保存」).

    ASCII single quotes count too, so apostrophes ("don't ... it's") can pair up into a span.
    """
    return [next((g for g in m.groups() if g is not None), "").lower() for m in _QUOTED.finditer(instruction)]


def lexical_score(c: ActionCandidate, instruction: str, learned: MenuKeyTable | None = None) -> int:
    """How well a candidate matches the instruction's words.

    3 per name hit (or a menu item's stock English name), 2 per identifier or help hit, 1 per hit in
    the enclosing groups or the value, and 5 when the name is quoted in the instruction.
    """
    goal = set(tokenize(instruction))
    if not goal:
        return 0

    def hits(s: str | None) -> int:
        return sum(1 for t in tokenize(s) if t in goal) if s else 0

    quoted = quoted_spans(instruction)
    score = 0
    t = c.target
    if t is not None:
        score += 3 * hits(t.label) + 2 * hits(t.identifier) + 2 * hits(t.help) + hits(" ".join(t.within))
        score += hits(t.value)
        name = (t.raw_label if t.raw_label is not None else t.label).lower()
        if name and name in quoted:
            score += 5
    if c.menu is not None:
        path = c.menu.path
        leaf = path[-1] if path else ""
        score += 3 * hits(leaf) + hits(" ".join(path)) + 2 * hits(c.menu.identifier)
        # A localized item's stock English name ("標準テキストにする" is "Make Plain Text") matches
        # an English instruction where the display title cannot.
        english = english_title(leaf, learned)
        if english and english.lower() != leaf.lower():
            score += 3 * hits(english)
        if leaf.lower() in quoted:
            score += 5
    if c.kind in _SUMMARY_KINDS:
        score += hits(c.summary)
    return score


def shards[T: HasLexical](cands: Sequence[T], size: int = MAX_OPTIONS) -> list[list[T]]:
    """Candidates ordered by lexical score (highest first, ties in input order), in chunks of `size`."""
    ordered = sorted(cands, key=lambda c: -c.lexical)
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def in_shard_order[T: HasLexical](cands: Sequence[T]) -> list[T]:
    """The candidates one after another in the order `shards` puts them."""
    return [c for shard in shards(cands) for c in shard]
