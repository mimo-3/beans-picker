"""Summarizes bench/results/runs.jsonl into the Markdown tables the README shows."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from cua_jev._json import JsonObject
from cua_jev._numbers import fixed, number_text

HERE: Final = Path(__file__).resolve().parent
RUNS_FILE: Final = HERE / "results/runs.jsonl"
CONDITIONS: Final = ("a", "b")
LABEL: Final = {"a": "(a) cua-driver only", "b": "(b) cua-jev"}
HEAD: Final = (
    "| | success | false success / success claims | tool calls (median) | time s (median) "
    "| Claude tokens (median, incl. cache) | output tokens (median) | USD (median) | Jev calls (total) "
    "| focus steals |\n|---|---|---|---|---|---|---|---|---|---|"
)


def load_runs(path: Path) -> list[JsonObject]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]


def median(xs: Sequence[float]) -> float:
    """The median; NaN for no values or when any value is NaN."""
    if not xs or any(math.isnan(x) for x in xs):
        return math.nan
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def fmt(n: float, digits: int = 0) -> str:
    """`n` with `digits` decimals (rounded half away from zero), or an en dash when not finite."""
    return fixed(n, digits) if math.isfinite(n) else "\u2013"


def _num(v: object) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else math.nan


def _sub(r: JsonObject, key: str, field: str) -> float:
    inner = r.get(key)
    return _num(inner.get(field)) if isinstance(inner, dict) else math.nan


def tokens_of(r: JsonObject) -> float:
    """Claude's tokens for one run: input, output, cache reads and cache writes."""
    return sum(_sub(r, "tokens", f) for f in ("input", "output", "cacheRead", "cacheWrite"))


def _count(rs: Sequence[JsonObject], test: Callable[[JsonObject], bool]) -> int:
    return sum(1 for r in rs if test(r))


def _total(rs: Sequence[JsonObject], value: Callable[[JsonObject], float]) -> str:
    return number_text(sum(value(r) for r in rs))


def row(label: str, rs: Sequence[JsonObject]) -> str:
    """One table row for the runs `rs`."""
    passed = _count(rs, lambda r: bool(r.get("pass")))
    claims = _count(rs, lambda r: r.get("claimed") == "success")
    false_success = _count(rs, lambda r: bool(r.get("falseSuccess")))
    cells = [
        label,
        f"{passed}/{len(rs)}",
        f"{false_success}/{claims}",
        fmt(median([_num(r.get("toolCalls")) for r in rs]), 1),
        fmt(median([_num(r.get("wallMs")) / 1000 for r in rs]), 1),
        fmt(median([tokens_of(r) for r in rs]) / 1000, 1) + "k",
        fmt(median([_sub(r, "tokens", "output") for r in rs])),
        fmt(median([_num(r.get("costUsd")) for r in rs]), 3),
        _total(rs, lambda r: _sub(r, "jev", "calls")),
        _total(rs, lambda r: _num(r.get("focusSteals"))),
    ]
    return "| " + " | ".join(cells) + " |"


def summarize(runs: Sequence[JsonObject]) -> str:
    """The Overall and Per task tables, ending with a newline."""
    out = ["### Overall", "", HEAD]
    out += [row(LABEL[c], [r for r in runs if r.get("condition") == c]) for c in CONDITIONS]
    out += ["", "### Per task", "", HEAD.replace("| |", "| task / condition |", 1)]
    tasks = dict.fromkeys(str(r.get("task")) for r in runs)
    for task in tasks:
        for c in CONDITIONS:
            rs = [r for r in runs if r.get("task") == task and r.get("condition") == c]
            if rs:
                out.append(row(f"{task} {c}", rs))
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the tables for the given runs file (default: the committed results)."""
    args = sys.argv[1:] if argv is None else argv
    sys.stdout.write(summarize(load_runs(Path(args[0]) if args else RUNS_FILE)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
