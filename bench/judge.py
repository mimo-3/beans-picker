"""The judge: compares a task's final state with its `expected` JSON by exact equality.

It never sees what the agent said. Fixture tasks are read from the fixture's state file; Calculator
tasks from its display, read over accessibility with cua-driver's CLI.

    python -m bench.judge <task-id> --state <file>
    python -m bench.judge <task-id> --pid <pid> --window <id>

Prints {"task","pass","actual","mismatches"} as one line and exits 0 on pass, 1 on fail.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, TypedDict

from cua_jev._json import JsonObject, JsonValue, dumps
from cua_jev._numbers import parse_number
from cua_jev._proc import Runner, run
from cua_jev._text import NOT_LINE_END

HERE: Final = Path(__file__).resolve().parent
TASKS_FILE: Final = HERE / "tasks.json"
DRIVER: Final = Path.home() / ".local/bin/cua-driver"
DRIVER_MAX_BYTES: Final = 32 * 1024 * 1024


class Task(TypedDict):
    """One benchmark task from tasks.json."""

    id: str
    app: Literal["fixture", "calculator"]
    setup: dict[str, str]
    prompt: str
    expected: JsonObject


def load_tasks(path: Path = TASKS_FILE) -> list[Task]:
    """The tasks of `path`, in file order."""
    tasks: list[Task] = json.loads(path.read_text(encoding="utf-8"))["tasks"]
    return tasks


TASKS: Final = load_tasks()


class _Missing:
    """A key that is not in the actual state (written as `undefined` in a mismatch)."""


_MISSING: Final = _Missing()


def _same(want: JsonValue, got: JsonValue | _Missing) -> bool:
    """Exact equality: same kind of value and same value. A boolean is never equal to a number
    (so 1 is not true), integers and floats compare by value, and lists and objects are never
    equal to a separately read one."""
    if isinstance(got, _Missing):
        return False
    if isinstance(want, bool) or isinstance(got, bool):
        return type(want) is type(got) and want == got
    if isinstance(want, int | float) and isinstance(got, int | float):
        return want == got
    if isinstance(want, str) and isinstance(got, str):
        return want == got
    return want is got


def _shown(v: JsonValue | _Missing) -> str:
    return "undefined" if isinstance(v, _Missing) else dumps(v)


def compare(expected: Mapping[str, JsonValue], actual: Mapping[str, JsonValue]) -> list[str]:
    """One line per expected key whose actual value is not exactly equal, in `expected`'s order.

    Keys only in `actual` are ignored.
    """
    out: list[str] = []
    for key, want in expected.items():
        got = actual.get(key, _MISSING)
        if not _same(want, got):
            out.append(f"{key}: expected {dumps(want)}, got {_shown(got)}")
    return out


# Directional and zero-width marks Calculator puts around numbers. They format, they are not text.
BIDI: Final = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_STATIC_TEXT: Final = re.compile(rf'AXStaticText = "((?:[^"\\]|\\{NOT_LINE_END})*)"')
_ESCAPED: Final = re.compile(r'\\(["\\])')


def calculator_display(tree_markdown: str) -> str | None:
    """Calculator's display: the last static text of its window (the current value, below the
    expression line), with bidi marks removed; None when the window has no static text."""
    texts: list[str] = []
    for line in tree_markdown.split("\n"):
        if line.startswith("- ") and texts:
            break  # a second top-level row: the menu bar or a repeated window
        m = _STATIC_TEXT.search(line)
        if m:
            texts.append(_ESCAPED.sub(r"\1", m.group(1)))
    return BIDI.sub("", texts[-1]) if texts else None


async def read_calculator(pid: float, window_id: float, *, runner: Runner = run) -> JsonObject:
    """{"display": ...} read from Calculator's window, or {} when it shows no text."""
    args = dumps({"pid": pid, "window_id": window_id, "include_screenshot": False})
    out = await runner([str(DRIVER), "call", "get_window_state", args], max_bytes=DRIVER_MAX_BYTES)
    md = json.loads(out.stdout).get("tree_markdown")
    display = calculator_display(md if isinstance(md, str) else "")
    return {} if display is None else {"display": display}


def _arg(rest: Sequence[str], name: str) -> str | None:
    """The value after `name`; without `name`, the first argument (no validation)."""
    i = rest.index(name) + 1 if name in rest else 0
    return rest[i] if i < len(rest) else None


def _number(text: str | None) -> float:
    return math.nan if text is None else parse_number(text)


async def judge(argv: Sequence[str], *, runner: Runner = run, tasks: Sequence[Task] = TASKS) -> JsonObject:
    """The verdict for `argv` (`<task-id> --state <file>` or `<task-id> --pid <p> --window <w>`).

    The task's app decides which reader is used, not the flags given. Raises ValueError for an
    unknown task.
    """
    task_id = argv[0] if argv else None
    rest = argv[1:]
    task = next((t for t in tasks if t["id"] == task_id), None)
    if task is None:
        raise ValueError(f"unknown task {'undefined' if task_id is None else task_id}")
    actual: JsonObject
    if task["app"] == "fixture":
        state = _arg(rest, "--state")
        if state is None:
            raise ValueError("no state file given")
        actual = _read_state(Path(state))
    else:
        actual = await read_calculator(_number(_arg(rest, "--pid")), _number(_arg(rest, "--window")), runner=runner)
    mismatches: list[JsonValue] = list(compare(task["expected"], actual))
    return {"task": task["id"], "pass": not mismatches, "actual": actual, "mismatches": mismatches}


def _read_state(path: Path) -> JsonObject:
    state: JsonObject = json.loads(path.read_text(encoding="utf-8"))
    return state


def main(argv: Sequence[str] | None = None) -> int:
    """Print the verdict line; 0 on pass, 1 on fail."""
    verdict = asyncio.run(judge(sys.argv[1:] if argv is None else argv))
    sys.stdout.write(dumps(verdict) + "\n")
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
