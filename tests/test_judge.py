from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from bench import judge as judge_mod
from bench.judge import DRIVER, DRIVER_MAX_BYTES, calculator_display, compare, judge, load_tasks, main, read_calculator

from cua_jev._json import JsonObject, dumps
from cua_jev._proc import Completed
from tests.fakes import fake_runner

ROOT = Path(__file__).resolve().parent.parent
LRM = "\u200e"
CLEAN_STATE: JsonObject = {
    "body": "",
    "deletes": 0,
    "email": "",
    "name": "",
    "newsletter": 0,
    "saves": 0,
    "search": "",
    "size": "Small",
    "status": "Not saved",
}


def _window_state_argv(pid: str, window: str) -> tuple[str, ...]:
    args = f'{{"pid":{pid},"window_id":{window},"include_screenshot":false}}'
    return (str(DRIVER), "call", "get_window_state", args)


def test_compares_every_expected_key_by_exact_equality() -> None:
    assert compare({"name": "  Grace Hopper "}, {"name": "  Grace Hopper "}) == []
    assert len(compare({"name": "  Grace Hopper "}, {"name": "Grace Hopper"})) == 1
    assert len(compare({"body": "todo:\nbuy milk"}, {"body": "todo:\nbuy milk\n"})) == 1
    assert len(compare({"newsletter": 1}, {"newsletter": True})) == 1


def test_reads_calculators_current_value_not_the_expression_line_and_only_from_the_first_window() -> None:
    md = "\n".join(
        [
            '- [0] AXWindow "\u8a08\u7b97\u6a5f"',
            f'  - AXStaticText = "{LRM}48+16"',
            f'  - AXStaticText = "{LRM}64"',
            "- [27] AXMenuBar",
            '- [234] AXWindow "\u8a08\u7b97\u6a5f"',
            f'  - AXStaticText = "{LRM}99"',
        ]
    )
    assert calculator_display(md) == "64"


@pytest.mark.parametrize(
    ("expected", "actual", "want"),
    [
        ({"a": 0}, {"a": False}, ["a: expected 0, got false"]),
        ({"a": True}, {"a": 1}, ["a: expected true, got 1"]),
        ({"x": "a"}, {}, ['x: expected "a", got undefined']),
        ({"a": "0"}, {"a": 0}, ['a: expected "0", got 0']),
        ({"a": 1}, {"a": 1.0}, []),
        ({"a": None}, {"a": None}, []),
        ({"a": 1}, {"a": 1, "extra": 2}, []),
        ({"b": 1, "a": 2}, {}, ["b: expected 1, got undefined", "a: expected 2, got undefined"]),
        ({"s": "x\ny"}, {"s": 'q"'}, ['s: expected "x\\ny", got "q\\""']),
    ],
)
def test_compare_is_strict_and_keeps_the_expected_order(
    expected: JsonObject, actual: JsonObject, want: list[str]
) -> None:
    assert compare(expected, actual) == want


@pytest.mark.parametrize(
    ("md", "want"),
    [
        ("", None),
        ("- AXWindow", None),
        ('- AXStaticText = "first row"\n- AXStaticText = "second row"', "first row"),
        ('  - AXStaticText = "a\\"b"', 'a"b'),
        ('  - AXStaticText = "a\\\\b"', "a\\b"),
        ('  - AXStaticText = "a\\nb"', "a\\nb"),
        ('  - AXStaticText = "x" AXStaticText = "y"', "x"),
        ('  - AXStaticText = "\u200b1\u202a2\u20693\u200f"', "123"),
        ('  - AXStaticText = "a\\\rb"', None),
        ('  - AXStaticText = "a\\\u2028b"', None),
    ],
)
def test_calculator_display_edges(md: str, want: str | None) -> None:
    assert calculator_display(md) == want


async def test_read_calculator_asks_cua_driver_for_the_tree() -> None:
    tree = '- AXWindow\n  - AXStaticText = "8"'
    runner = fake_runner({_window_state_argv("12", "34"): Completed(json.dumps({"tree_markdown": tree}), 0)})
    assert await read_calculator(12, 34, runner=runner) == {"display": "8"}
    assert runner.calls == [_window_state_argv("12", "34")]


@pytest.mark.parametrize("reply", [{}, {"tree_markdown": None}, {"tree_markdown": "- AXWindow"}])
async def test_read_calculator_leaves_out_a_missing_display(reply: JsonObject) -> None:
    runner = fake_runner({_window_state_argv("1", "2"): Completed(json.dumps(reply), 0)})
    assert await read_calculator(1, 2, runner=runner) == {}


async def test_read_calculator_allows_a_large_tree() -> None:
    seen: list[int] = []

    async def runner(argv: object, /, *, max_bytes: int = 0, check: bool = True, cwd: Path | None = None) -> Completed:
        seen.append(max_bytes)
        return Completed("{}", 0)

    await read_calculator(1, 2, runner=runner)
    assert seen == [DRIVER_MAX_BYTES]


def _state(tmp_path: Path, **changes: object) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({**CLEAN_STATE, **changes}), encoding="utf-8")
    return path


async def test_judge_reads_the_fixture_state_file(tmp_path: Path) -> None:
    state = _state(tmp_path, name="  Grace Hopper ")
    verdict = await judge(["fx-name-spaces", "--state", str(state)])
    assert list(verdict) == ["task", "pass", "actual", "mismatches"]
    assert verdict["pass"] is True
    assert verdict["mismatches"] == []
    assert verdict["actual"] == {**CLEAN_STATE, "name": "  Grace Hopper "}


async def test_judge_lists_each_mismatch(tmp_path: Path) -> None:
    state = _state(tmp_path, size="Large", saves=2)
    verdict = await judge(["fx-size-save", "--state", str(state)])
    assert verdict["pass"] is False
    assert verdict["mismatches"] == ["saves: expected 1, got 2"]


async def test_judge_without_the_flag_takes_the_first_argument(tmp_path: Path) -> None:
    state = _state(tmp_path)
    verdict = await judge(["fx-clear-search-keep-note", str(state)])
    assert verdict["mismatches"] == ['body: expected "Meeting at 3pm", got ""']


async def test_judge_reads_calculator_by_task_not_by_flags() -> None:
    tree = '- AXWindow\n  - AXStaticText = "12"'
    runner = fake_runner({_window_state_argv("7", "9"): Completed(json.dumps({"tree_markdown": tree}), 0)})
    verdict = await judge(["calc-negate", "--state", "x", "--pid", "7", "--window", "9"], runner=runner)
    assert verdict == {"task": "calc-negate", "pass": True, "actual": {"display": "12"}, "mismatches": []}


async def test_judge_sends_null_for_a_pid_that_is_not_a_number() -> None:
    runner = fake_runner({_window_state_argv("null", "null"): Completed("{}", 0)})
    verdict = await judge(["calc-chain"], runner=runner)
    assert verdict["actual"] == {}
    assert verdict["mismatches"] == ['display: expected "8", got undefined']


@pytest.mark.parametrize(("argv", "message"), [(["nope"], "unknown task nope"), ([], "unknown task undefined")])
async def test_judge_refuses_an_unknown_task(argv: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await judge(argv)


def test_main_prints_one_line_and_exits_by_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = _state(tmp_path, name="  Grace Hopper ")
    assert main(["fx-name-spaces", "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert (
        out
        == dumps(
            {
                "task": "fx-name-spaces",
                "pass": True,
                "actual": {**CLEAN_STATE, "name": "  Grace Hopper "},
                "mismatches": [],
            }
        )
        + "\n"
    )
    assert main(["fx-size-save", "--state", str(state)]) == 1


def test_runs_as_a_module_from_the_repository_root(tmp_path: Path) -> None:
    state = _state(tmp_path, name="\u00e9")
    proc = subprocess.run(
        [sys.executable, "-m", "bench.judge", "fx-name-spaces", "--state", str(state)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 1
    line = json.loads(proc.stdout)
    assert line["mismatches"] == ['name: expected "  Grace Hopper ", got "\u00e9"']
    assert proc.stdout.endswith("}\n")


def test_tasks_are_the_eight_bench_tasks() -> None:
    tasks = load_tasks()
    assert [t["id"] for t in tasks] == [
        "fx-name-spaces",
        "fx-body-not-search",
        "fx-size-save",
        "fx-clear-search-keep-note",
        "fx-email-fix-newsletter",
        "calc-chain",
        "calc-percent",
        "calc-negate",
    ]
    by_id = {t["id"]: t for t in tasks}
    assert by_id["fx-body-not-search"]["setup"] == {"body": "todo:\n"}
    assert '"todo:\\nbuy milk"' in by_id["fx-body-not-search"]["prompt"]
    assert by_id["calc-chain"]["expected"] == {"display": "8"}
    assert tasks == judge_mod.TASKS
    assert all("status" not in t["expected"] for t in tasks)
