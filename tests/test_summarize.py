"""The bench summary tables."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from bench.summarize import HEAD, RUNS_FILE, fmt, load_runs, main, median, row, summarize, tokens_of

from cua_jev._json import JsonObject, JsonValue, dumps

SUMMARY = RUNS_FILE.parent / "summary.md"
DASH = "\u2013"


def _run(task: str = "t", condition: str = "a", **changes: JsonValue) -> JsonObject:
    run: JsonObject = {
        "task": task,
        "condition": condition,
        "pass": True,
        "claimed": "success",
        "falseSuccess": False,
        "toolCalls": 4,
        "wallMs": 12000,
        "costUsd": 0.05,
        "tokens": {"input": 1, "output": 200, "cacheRead": 1000, "cacheWrite": 299},
        "jev": {"calls": 2, "inputTokens": 900},
        "focusSteals": 0,
    }
    run.update(changes)
    return run


def test_committed_summary_is_the_summary_of_the_committed_runs() -> None:
    runs = load_runs(RUNS_FILE)
    assert len(runs) == 48
    assert summarize(runs) == SUMMARY.read_text(encoding="utf-8")


def test_main_prints_the_summary_of_a_given_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text("\n".join(dumps(r) for r in [_run(), _run(condition="b")]) + "\n\n", encoding="utf-8")
    assert main([str(runs_file)]) == 0
    assert capsys.readouterr().out == summarize(load_runs(runs_file))


def test_main_defaults_to_the_committed_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == SUMMARY.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("xs", "want"),
    [([3, 1, 2], 2), ([4, 1, 3, 2], 2.5), ([5], 5), ([2, 10], 6)],
)
def test_median(xs: list[float], want: float) -> None:
    assert median(xs) == want


@pytest.mark.parametrize("xs", [[], [1, math.nan, 3]])
def test_median_of_nothing_or_nan_is_nan(xs: list[float]) -> None:
    assert math.isnan(median(xs))


@pytest.mark.parametrize(
    ("n", "digits", "want"),
    [(5.5, 1, "5.5"), (0.125, 2, "0.13"), (2.5, 0, "3"), (0.0595, 3, "0.059"), (1876, 0, "1876")],
)
def test_fmt_rounds_on_the_exact_value(n: float, digits: int, want: str) -> None:
    assert fmt(n, digits) == want


@pytest.mark.parametrize("n", [math.nan, math.inf, -math.inf])
def test_fmt_writes_a_dash_when_not_finite(n: float) -> None:
    assert fmt(n, 1) == DASH


def test_tokens_of_adds_all_four_counts() -> None:
    assert tokens_of(_run()) == 1500


def test_row_counts_and_medians() -> None:
    runs = [
        _run(toolCalls=3, wallMs=10000),
        _run(toolCalls=5, wallMs=13000, falseSuccess=True, focusSteals=2) | {"pass": False},
        _run(claimed="failure", toolCalls=10, costUsd=0.07),
    ]
    assert row("x", runs) == "| x | 2/3 | 1/2 | 5.0 | 12.0 | 1.5k | 200 | 0.050 | 6 | 2 |"


def test_a_missing_cost_makes_the_median_a_dash() -> None:
    run = _run()
    del run["costUsd"]
    assert row("x", [run]).split(" | ")[7] == DASH


def test_empty_conditions_still_get_an_overall_row() -> None:
    out = summarize([_run(task="only-b", condition="b")]).split("\n")
    assert out[:3] == ["### Overall", "", HEAD.split("\n")[0]]
    assert out[4] == f"| (a) cua-driver only | 0/0 | 0/0 | {DASH} | {DASH} | {DASH}k | {DASH} | {DASH} | 0 | 0 |"
    assert out[-2].startswith("| only-b b | 1/1 |")
    assert out[-1] == ""


def test_per_task_rows_follow_first_seen_order_then_condition() -> None:
    runs = [_run("t2", "b"), _run("t1", "a"), _run("t2", "a")]
    lines = summarize(runs).split("\n")
    start = lines.index("### Per task")
    assert lines[start + 2].startswith("| task / condition | success |")
    assert [line.split(" | ")[0] for line in lines[start + 4 : -1]] == ["| t2 a", "| t2 b", "| t1 a"]
