"""The bench runner: arguments, accounting, records, setup and the run loop, all with fakes."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from bench.judge import Task, load_tasks
from bench.run import (
    DRIVER,
    DRIVER_DENY,
    FIXTURE_APP,
    ROOT,
    Bench,
    FrontWatch,
    LineSplitter,
    Metrics,
    Options,
    Target,
    build_record,
    claimed,
    claude_argv,
    condition_order,
    mcp_servers,
    parse_args,
    progress_line,
    prompt_for,
    run_id,
    selected,
    take,
)

from cua_jev._json import JsonObject, JsonValue, dumps
from cua_jev._proc import DEFAULT_MAX_BYTES, Completed, Runner
from cua_jev.errors import ProcessError
from tests.fakes import FakeRunner, RecordingSleep, fake_runner

TASKS = {t["id"]: t for t in load_tasks()}
NOW = datetime(2026, 9, 24, 12, 28, 41, 383_999, tzinfo=UTC)
FIXTURE_FIND = ("lsappinfo", "find", "bundleid=dev.cua-jev.fixture")
CALC_FIND = ("lsappinfo", "find", "bundleid=com.apple.calculator")
PKILL = ("pkill", "-x", "CuaJevFixture")

type Step = Completed | Exception | Callable[[], Completed]


class ScriptedRunner:
    """A `Runner` answering each argv from its own queue; the last answer repeats."""

    def __init__(self, script: dict[tuple[str, ...], list[Step]]) -> None:
        self.script = script
        self.calls: list[tuple[str, ...]] = []
        self.max_bytes: list[int] = []

    async def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        check: bool = True,
        cwd: Path | None = None,
    ) -> Completed:
        key = tuple(argv)
        self.calls.append(key)
        self.max_bytes.append(max_bytes)
        queue = self.script[key]
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        out = step if isinstance(step, Completed) else step()
        if check and out.returncode != 0:
            raise ProcessError(f"{argv[0]} exited with code {out.returncode}", returncode=out.returncode)
        return out


def ok(stdout: str = "") -> Completed:
    return Completed(stdout, 0)


def driver(tool: str, args: JsonObject) -> tuple[str, ...]:
    return (str(DRIVER), "call", tool, dumps(args))


def info(asn: str) -> tuple[str, ...]:
    return ("lsappinfo", "info", "-only", "pid", asn)


def result_event(**fields: JsonValue) -> str:
    return dumps({"type": "result", **fields})


def assistant(*names: JsonValue) -> str:
    return dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": n} for n in names]}})


def tool_result(content: JsonValue) -> str:
    return dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": content}]}})


# --- arguments ---------------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    assert parse_args([]) == Options(reps=1, model="sonnet", only=None, conditions=["a", "b"])


def test_parse_args_reads_every_flag() -> None:
    argv = ["--reps", "3", "--model", "opus", "--only", "calc-chain,fx-size-save", "--conditions", "b"]
    assert parse_args(argv) == Options(reps=3, model="opus", only=["calc-chain", "fx-size-save"], conditions=["b"])


def test_parse_args_edges() -> None:
    assert math.isnan(parse_args(["--reps", "many"]).reps)
    assert parse_args(["--reps"]).reps == 1
    assert parse_args(["--model", ""]).model == ""
    assert parse_args(["--only"]).only is None


def test_selected_keeps_file_order() -> None:
    tasks = load_tasks()
    assert [t["id"] for t in selected(tasks, ["calc-chain", "fx-name-spaces"])] == ["fx-name-spaces", "calc-chain"]
    assert selected(tasks, None) == tasks
    assert selected(tasks, [""]) == []


@pytest.mark.parametrize(("rep", "want"), [(1, ["a", "b"]), (2, ["b", "a"]), (3, ["a", "b"])])
def test_condition_order_alternates(rep: int, want: list[str]) -> None:
    assert condition_order(["a", "b"], rep) == want


# --- prompt, ids and configs -------------------------------------------------------------------


def test_run_id_is_the_utc_start_time_with_dashes() -> None:
    assert run_id("fx-name-spaces", "a", NOW) == "2026-09-24T12-28-41-383Z-fx-name-spaces-a"
    tokyo = NOW.astimezone(timezone(timedelta(hours=9)))
    assert run_id("calc-chain", "b", tokyo) == "2026-09-24T12-28-41-383Z-calc-chain-b"


def test_prompt_for() -> None:
    target = Target(pid=321, window_id=9, app_label='the "CuaJevFixture" app')
    assert prompt_for(TASKS["fx-size-save"], target) == (
        'You are operating a macOS app with the tools you have. The target is the "CuaJevFixture" app, '
        "running as pid 321, window id 9. Use only that window.\n"
        "Work in the background: never bring the app to the front and never activate it.\n"
        "\n"
        'Task: Choose "Large" in the Size pop-up, then press the Save button exactly once.\n'
        "\n"
        "When you have finished, end your final message with exactly one line: `RESULT: success` if the task "
        "is done, or `RESULT: failure` if it is not."
    )


def test_mcp_servers() -> None:
    assert mcp_servers("a") == {"cua-driver": {"command": str(DRIVER), "args": ["mcp"]}}
    assert mcp_servers("b") == {"cua-jev": {"command": sys.executable, "args": ["-m", "cua_jev"]}}


def test_claude_argv_denies_driver_tools_only_in_a() -> None:
    cfg = Path("/w/mcp-a.json")
    tail = ["--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    head = ["-p", "P", "--model", "sonnet", "--tools", "", "--mcp-config", str(cfg), "--strict-mcp-config"]
    deny = ["--disallowedTools", *(f"mcp__cua-driver__{t}" for t in DRIVER_DENY)]
    assert claude_argv("a", "P", "sonnet", cfg) == [*head, "--allowedTools", "mcp__cua-driver", *deny, *tail]
    assert claude_argv("b", "P", "sonnet", cfg) == [*head, "--allowedTools", "mcp__cua-jev", *tail]
    assert len(DRIVER_DENY) == 10


# --- accounting --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("mcp__cua-jev__act", "act"),
        ("mcp__cua-driver__get_window_state", "get_window_state"),
        ("mcp__server__tool__x", "tool__x"),
        ("Bash", "Bash"),
        (None, "null"),
    ],
)
def test_take_strips_the_mcp_prefix_from_tool_names(name: JsonValue, want: str) -> None:
    m = Metrics()
    take(m, assistant(name))
    assert m.tool_calls == 1
    assert m.tool_names == {want: 1}


def test_take_counts_tool_uses_only() -> None:
    m = Metrics()
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use"}]}}
    take(m, dumps(event))
    take(m, assistant("mcp__cua-jev__act", "mcp__cua-jev__act"))
    assert m.tool_calls == 3
    assert m.tool_names == {"undefined": 1, "act": 2}


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": '{"status":"done","jev":{"calls":2,"inputTokens":300}}'}],
        [{"type": "text", "text": '{"jev":{"calls":'}, {"type": "image"}, {"text": '2,"inputTokens":300}}'}],
        '{"jev":{"calls":2,"inputTokens":300}} {"jev":{"calls":5,"inputTokens":5}}',
    ],
)
def test_take_adds_jev_usage_from_tool_results(content: JsonValue) -> None:
    m = Metrics()
    take(m, tool_result(content))
    take(m, tool_result(content))
    assert m.jev == {"calls": 4, "inputTokens": 600}


def test_take_ignores_what_it_cannot_read() -> None:
    m = Metrics()
    for line in ["not json", "5", "null", '{"type":"assistant"}', '{"type":"user","message":"x"}', tool_result(None)]:
        take(m, line)
    assert m == Metrics()


def test_take_keeps_the_last_result_event() -> None:
    m = Metrics()
    usage: JsonObject = {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 4,
    }
    take(m, result_event(result="first", is_error=True, num_turns=2, duration_ms=10, total_cost_usd=0.5, usage=usage))
    take(m, result_event(result="RESULT: success", num_turns=3))
    assert m.result == "RESULT: success"
    assert m.is_error is False
    assert m.outcome == {"numTurns": 3}
    assert m.tokens == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def test_take_nullish_numbers() -> None:
    m = Metrics()
    take(m, result_event(result=None, is_error="yes", num_turns=None, usage={"input_tokens": None, "output_tokens": 0}))
    assert m.result == ""
    assert m.is_error is False
    assert m.outcome == {"numTurns": None}
    assert m.tokens == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


@pytest.mark.parametrize(
    ("result", "want"),
    [
        ("all good\nRESULT: success", "success"),
        ("result:success", "success"),
        ("RESULT:\t SUCCESS", "success"),
        ("RESULT: failure", "failure"),
        ("RESULT: failure\nRESULT: success", "success"),
        ("RESULT: done", "none"),
        ("", "none"),
        (None, "none"),
    ],
)
def test_claimed(result: str | None, want: str) -> None:
    assert claimed(result) == want


def test_line_splitter_joins_chunks_and_split_characters() -> None:
    s = LineSplitter()
    data = "ab\ncaf\u00e9\n\nrest".encode()
    cut = data.index(b"\xa9")
    assert s.feed(data[:cut]) == ["ab"]
    assert s.feed(data[cut:]) == ["caf\u00e9", ""]
    assert s.rest() == "rest"


# --- records -----------------------------------------------------------------------------------

FULL_KEYS = [
    "runId", "rep", "task", "condition", "model", "pass", "claimed", "falseSuccess", "mismatches", "actual",
    "toolCalls", "toolNames", "numTurns", "wallMs", "durationMs", "costUsd", "tokens", "jev", "focusSteals", "isError",
]  # fmt: skip
PASSING: JsonObject = {"task": "calc-chain", "pass": True, "actual": {"display": "8"}, "mismatches": []}
FAILING: JsonObject = {"task": "calc-chain", "pass": False, "actual": {}, "mismatches": ["display: x"]}


def _record(judge: JsonObject, *lines: str) -> JsonObject:
    m = Metrics()
    for line in lines:
        take(m, line)
    task = TASKS["calc-chain"]
    return build_record(
        rid="r", rep=1, task=task, condition="b", model="sonnet", judge=judge, m=m, wall_ms=1500, steals=0
    )


FULL_RESULT = result_event(
    result="RESULT: success", is_error=False, num_turns=4, duration_ms=1400, total_cost_usd=0.06, usage={}
)


@pytest.mark.parametrize(
    ("judge", "lines", "keys", "claim", "false_success"),
    [
        (PASSING, [FULL_RESULT], FULL_KEYS, "success", False),
        (FAILING, [FULL_RESULT], FULL_KEYS, "success", True),
        (
            FAILING,
            [assistant("mcp__cua-jev__act")],
            [k for k in FULL_KEYS if k not in ("numTurns", "durationMs", "costUsd", "isError")],
            "none",
            False,
        ),
        (
            PASSING,
            [result_event(result="RESULT: failure")],
            [k for k in FULL_KEYS if k not in ("numTurns", "durationMs", "costUsd")],
            "failure",
            False,
        ),
        (
            {"actual": {}},
            [],
            [k for k in FULL_KEYS if k not in ("pass", "mismatches", "numTurns", "durationMs", "costUsd", "isError")],
            "none",
            False,
        ),
    ],
)
def test_record_keys_and_claims(
    judge: JsonObject, lines: list[str], keys: list[str], claim: str, false_success: bool
) -> None:
    rec = _record(judge, *lines)
    assert list(rec) == keys
    assert rec["claimed"] == claim
    assert rec["falseSuccess"] is false_success


def test_record_values() -> None:
    rec = _record(PASSING, assistant("mcp__cua-jev__act"), FULL_RESULT)
    assert dumps(rec) == (
        '{"runId":"r","rep":1,"task":"calc-chain","condition":"b","model":"sonnet","pass":true,'
        '"claimed":"success","falseSuccess":false,"mismatches":[],"actual":{"display":"8"},"toolCalls":1,'
        '"toolNames":{"act":1},"numTurns":4,"wallMs":1500,"durationMs":1400,"costUsd":0.06,'
        '"tokens":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0},"jev":{"calls":0,"inputTokens":0},'
        '"focusSteals":0,"isError":false}'
    )


@pytest.mark.parametrize(("wall_ms", "seconds"), [(2500, "3s"), (2499, "2s"), (0, "0s"), (135628, "136s")])
def test_progress_line_rounds_half_up(wall_ms: int, seconds: str) -> None:
    m = Metrics(tool_calls=3, result="RESULT: success")
    line = progress_line("calc-chain", "a", 2, PASSING, m, wall_ms)
    assert line == f"calc-chain [a] rep 2: PASS claimed=success tools=3 {seconds}\n"
    assert (
        progress_line("calc-chain", "b", 1, {}, Metrics(), 0) == "calc-chain [b] rep 1: FAIL claimed=none tools=0 0s\n"
    )


# --- the frontmost app -------------------------------------------------------------------------


async def _until(test: Callable[[], bool]) -> None:
    """Poll a counter that the watcher under test bumps from its own task."""
    while not test():  # noqa: ASYNC110
        await asyncio.sleep(0.001)


async def test_front_watch_counts_the_app_in_front() -> None:
    runner = fake_runner({("lsappinfo", "front"): ok("ASN:0x0-0x1:\n"), info("ASN:0x0-0x1:"): ok('"pid"=77\n')})
    watch = FrontWatch(77, runner=runner, interval=0.001)
    await _until(lambda: watch.steals >= 3)
    assert await watch.stop() >= 3
    calls = len(runner.calls)
    await asyncio.sleep(0.01)
    assert len(runner.calls) == calls


async def test_front_watch_ignores_other_apps_and_missed_samples() -> None:
    other = fake_runner({("lsappinfo", "front"): ok("ASN:0x0-0x1:"), info("ASN:0x0-0x1:"): ok('"pid"=5')})
    watch = FrontWatch(77, runner=other, interval=0.001)
    await _until(lambda: len(other.calls) >= 4)
    assert await watch.stop() == 0
    broken = fake_runner({("lsappinfo", "front"): ProcessError("lsappinfo exited with code 1")})
    watch = FrontWatch(77, runner=broken, interval=0.001)
    await _until(lambda: len(broken.calls) >= 2)
    assert await watch.stop() == 0


async def test_front_watch_skips_ticks_while_a_sample_is_in_flight() -> None:
    release = asyncio.Event()
    fronts = 0

    async def runner(
        argv: Sequence[str], /, *, max_bytes: int = 0, check: bool = True, cwd: Path | None = None
    ) -> Completed:
        nonlocal fronts
        if argv[1] == "front":
            fronts += 1
            await release.wait()
            return ok("ASN:1")
        return ok('"pid" = 77')

    watch = FrontWatch(77, runner=runner, interval=0.001)
    await _until(lambda: fronts == 1)
    await asyncio.sleep(0.02)
    assert fronts == 1
    stopping = asyncio.create_task(watch.stop())
    await asyncio.sleep(0.005)
    assert not stopping.done()
    release.set()
    assert await stopping == 1


# --- setup and judging -------------------------------------------------------------------------


def _bench(tmp_path: Path, runner: Runner, *, claude: str = "claude") -> Bench:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Bench(
        options=Options(),
        tasks=list(TASKS.values()),
        runner=runner,
        sleep=RecordingSleep(),
        claude=claude,
        work=work,
        out=tmp_path / "results/runs.jsonl",
        stderr=io.StringIO(),
    )


async def test_pid_of() -> None:
    runner = fake_runner(
        {
            ("lsappinfo", "find", "q"): ok('ASN:0x0-0x5a:"Calculator"\n'),
            info("ASN:0x0-0x5a"): ok('"pid" = 1234\n'),
            ("lsappinfo", "find", "none"): ok("\n"),
            ("lsappinfo", "find", "nopid"): ok("ASN:0x1 x"),
            info("ASN:0x1"): ok("no pid here"),
        }
    )
    bench = Bench(options=Options(), tasks=[], runner=runner)
    assert await bench.pid_of("q") == 1234
    assert await bench.pid_of("none") is None
    assert await bench.pid_of("nopid") is None


async def test_setup_fixture_starts_a_fresh_window(tmp_path: Path) -> None:
    task = TASKS["fx-body-not-search"]
    title = "Notes form (fx-body-not-search)"
    state = tmp_path / "work" / "rid.state.json"
    state.parent.mkdir()
    state.write_text("old", encoding="utf-8")

    def launched() -> Completed:
        assert not state.exists()
        state.write_text("{}", encoding="utf-8")
        return ok()

    open_argv = ("open", "-g", "-n", str(FIXTURE_APP), "--args", "--state", str(state), "--title", title)
    windows = {"windows": [{"window_id": 5, "title": "other"}, {"window_id": 9, "title": title}]}
    runner = ScriptedRunner(
        {
            PKILL: [Completed("", 1)],
            FIXTURE_FIND: [ok("ASN:0x0-0x2:"), ok(""), ok(""), ok("ASN:0x0-0x3:")],
            info("ASN:0x0-0x2"): [ok('"pid"=300')],
            info("ASN:0x0-0x3"): [ok('"pid"=321')],
            (*open_argv, "--body", "todo:\n"): [launched],
            driver("list_windows", {"pid": 321}): [ok(json.dumps(windows))],
        }
    )
    bench = _bench(tmp_path, runner)
    target = await bench.setup_fixture(task, "rid")
    assert target == Target(pid=321, window_id=9, app_label='the "CuaJevFixture" app', state_file=state)
    assert isinstance(bench.sleep, RecordingSleep)
    assert bench.sleep.delays == [0.15, 0.15]
    assert runner.calls[0] == PKILL
    assert runner.max_bytes[-1] == 32 * 1024 * 1024


async def test_setup_fixture_gives_up_after_forty_tries(tmp_path: Path) -> None:
    state = str(tmp_path / "work/r.state.json")
    open_argv = (
        "open",
        "-g",
        "-n",
        str(FIXTURE_APP),
        "--args",
        "--state",
        state,
        "--title",
        "Notes form (fx-size-save)",
    )
    runner = ScriptedRunner({PKILL: [ok()], FIXTURE_FIND: [ok("")], open_argv: [ok()]})
    bench = _bench(tmp_path, runner)
    with pytest.raises(RuntimeError, match="fixture did not start"):
        await bench.setup_fixture(TASKS["fx-size-save"], "r")
    assert isinstance(bench.sleep, RecordingSleep)
    assert len(bench.sleep.delays) == 40


def _calculator_runner(display: str) -> ScriptedRunner:
    windows = {"windows": [{"window_id": 1, "is_on_screen": False}, {"window_id": 2}]}
    verdict = {"task": "calc-chain", "pass": False, "actual": {"display": display}, "mismatches": ["x"]}
    return ScriptedRunner(
        {
            CALC_FIND: [ok(""), ok("ASN:0x0-0x44:")],
            ("open", "-g", "-b", "com.apple.calculator"): [ok()],
            info("ASN:0x0-0x44"): [ok('"pid"=44')],
            driver("list_windows", {"pid": 44}): [ok(json.dumps(windows))],
            driver("hotkey", {"pid": 44, "window_id": 2, "keys": ["cmd", "1"]}): [ok("{}")],
            driver("press_key", {"pid": 44, "window_id": 2, "key": "escape"}): [ok("{}")],
            (sys.executable, "-m", "bench.judge", "calc-chain", "--pid", "44", "--window", "2"): [
                Completed(json.dumps(verdict), 1)
            ],
        }
    )


async def test_setup_calculator_clears_it_in_the_background(tmp_path: Path) -> None:
    runner = _calculator_runner("0")
    target = await _bench(tmp_path, runner).setup_calculator()
    assert target == Target(pid=44, window_id=2, app_label="Calculator")
    escapes = [c for c in runner.calls if c[2:3] == ("press_key",)]
    assert len(escapes) == 3


async def test_setup_calculator_stops_when_the_display_is_not_zero(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r'Calculator did not clear \(display \{"display":"5"\}\)'):
        await _bench(tmp_path, _calculator_runner("5")).setup_calculator()


async def test_run_judge_runs_from_the_root_and_reads_a_failing_verdict(tmp_path: Path) -> None:
    state = tmp_path / "s.json"
    verdict = {"task": "fx-size-save", "pass": False, "actual": {}, "mismatches": ["saves: expected 1, got 0"]}
    argv = (sys.executable, "-m", "bench.judge", "fx-size-save", "--state", str(state))
    runner = fake_runner({argv: Completed(json.dumps(verdict), 1)})
    got = await _bench(tmp_path, runner).run_judge(
        "fx-size-save", Target(pid=1, window_id=2, app_label="x", state_file=state)
    )
    assert got == verdict
    assert runner.cwds == [ROOT]


async def test_run_claude_streams_and_saves_the_events(tmp_path: Path) -> None:
    usage: JsonObject = {
        "input_tokens": 5,
        "output_tokens": 6,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 8,
    }
    lines = [
        assistant("mcp__cua-jev__act"),
        "",
        "   ",
        tool_result([{"type": "text", "text": '{"jev":{"calls":2,"inputTokens":300}}'}]),
        "not json",
    ]
    last = result_event(result="ok\nRESULT: success", is_error=False, num_turns=3, total_cost_usd=0.01, usage=usage)
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "json.dump(sys.argv[1:], open('args.json', 'w'))\n"
        f"lines = json.loads({json.dumps(lines)!r})\n"
        "sys.stdout.write(''.join(line + '\\n' for line in lines))\n"
        f"sys.stdout.write({last!r})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    bench = _bench(tmp_path, fake_runner(), claude=str(script))
    m = await bench.run_claude("b", "the prompt", "rid")
    assert m.tool_calls == 1
    assert m.tool_names == {"act": 1}
    assert m.jev == {"calls": 2, "inputTokens": 300}
    assert m.result == "ok\nRESULT: success"
    assert m.outcome == {"numTurns": 3, "costUsd": 0.01}
    assert m.tokens == {"input": 5, "output": 6, "cacheRead": 7, "cacheWrite": 8}
    saved = (bench.work / "rid.stream.jsonl").read_text(encoding="utf-8")
    assert saved == f"{lines[0]}\n{lines[3]}\n{lines[4]}\n"
    config = bench.work / "mcp-b.json"
    assert json.loads(config.read_text(encoding="utf-8")) == {"mcpServers": mcp_servers("b")}
    args = json.loads((bench.work / "args.json").read_text(encoding="utf-8"))
    assert args == claude_argv("b", "the prompt", "sonnet", config)


async def test_a_cancelled_run_ends_the_agent_and_what_it_started(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"helper = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(600)'])\n"
        "open('helper.pid', 'w').write(str(helper.pid))\n"
        "sys.stdout.write('{}\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    bench = _bench(tmp_path, fake_runner(), claude=str(script))
    running = asyncio.ensure_future(bench.run_claude("b", "the prompt", "rid"))
    pid_file = bench.work / "helper.pid"
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.05)
    helper = int(pid_file.read_text(encoding="utf-8"))
    running.cancel()
    await asyncio.wait_for(asyncio.wait({running}), 10)
    for _ in range(100):  # the helper's parent is gone, so it is reaped by the system, not by us
        try:
            os.kill(helper, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the agent's helper is still running")


# --- the run loop ------------------------------------------------------------------------------


class LoopBench(Bench):
    """A bench whose setup, agent and judge are canned; the loop and the records are real."""

    order: list[tuple[str, str, int]]
    verdict: JsonObject
    metrics: Callable[[], Metrics]
    fake: FakeRunner

    async def one_run(self, task: Task, condition: str, rep: int) -> JsonObject:
        self.order.append((task["id"], condition, rep))
        return await Bench.one_run(self, task, condition, rep)

    async def setup(self, task: Task, rid: str) -> Target:
        return Target(pid=7, window_id=8, app_label="Calculator")

    async def run_claude(self, condition: str, prompt: str, rid: str) -> Metrics:
        return self.metrics()

    async def run_judge(self, task_id: str, target: Target) -> JsonObject:
        return self.verdict


def _loop_bench(
    tmp_path: Path, options: Options, *, locked_after: int = 99, metrics: Callable[[], Metrics] = Metrics
) -> LoopBench:
    checks = 0

    async def screen_locked() -> bool:
        nonlocal checks
        checks += 1
        return checks > locked_after

    ticks = iter(range(0, 10**6, 2500))
    runner = fake_runner(
        {
            PKILL: Completed("", 1),
            ("lsappinfo", "find", "bundleid=com.apple.TextEdit"): ok(""),
            ("lsappinfo", "front"): ProcessError("no front app"),
        }
    )
    bench = LoopBench(
        options=options,
        tasks=[TASKS["calc-chain"], TASKS["calc-negate"]],
        runner=runner,
        now_ms=lambda: next(ticks),
        now=lambda: NOW,
        screen_locked=screen_locked,
        work=tmp_path / "work",
        out=tmp_path / "results/runs.jsonl",
        stderr=io.StringIO(),
    )
    bench.order = []
    bench.verdict = {"task": "calc-chain", "pass": False, "actual": {"display": "0"}, "mismatches": ["m"]}
    bench.metrics = metrics
    bench.fake = runner
    return bench


def _records(bench: Bench) -> list[JsonObject]:
    return [json.loads(line) for line in bench.out.read_text(encoding="utf-8").splitlines()]


async def test_main_runs_every_rep_task_and_condition_in_order(tmp_path: Path) -> None:
    bench = _loop_bench(tmp_path, Options(reps=2))
    assert await bench.main() == 0
    assert bench.order == [
        ("calc-chain", "a", 1),
        ("calc-chain", "b", 1),
        ("calc-negate", "a", 1),
        ("calc-negate", "b", 1),
        ("calc-chain", "b", 2),
        ("calc-chain", "a", 2),
        ("calc-negate", "b", 2),
        ("calc-negate", "a", 2),
    ]
    records = _records(bench)
    assert [(r["task"], r["condition"], r["rep"]) for r in records] == bench.order
    assert records[0]["runId"] == "2026-09-24T12-28-41-383Z-calc-chain-a"
    assert isinstance(bench.stderr, io.StringIO)
    assert bench.stderr.getvalue().splitlines()[0] == "calc-chain [a] rep 1: FAIL claimed=none tools=0 3s"
    assert bench.fake.calls[-1] == PKILL


async def test_an_interrupted_run_is_recorded_as_a_failure(tmp_path: Path) -> None:
    bench = _loop_bench(tmp_path, Options(conditions=["b"]), metrics=lambda: Metrics(tool_calls=2))
    await bench.main()
    first = _records(bench)[0]
    assert first["pass"] is False
    assert first["claimed"] == "none"
    assert first["focusSteals"] == 0
    assert first["wallMs"] == 2500
    assert not {"numTurns", "durationMs", "costUsd", "isError"} & set(first)


async def test_a_success_claim_the_judge_rejects_is_a_false_success(tmp_path: Path) -> None:
    claims = Metrics(result="RESULT: success")
    bench = _loop_bench(tmp_path, Options(conditions=["a"]), metrics=lambda: claims)
    await bench.main()
    assert [r["falseSuccess"] for r in _records(bench)] == [True, True]


async def test_main_stops_when_the_screen_locks(tmp_path: Path) -> None:
    bench = _loop_bench(tmp_path, Options(), locked_after=2)
    assert await bench.main() == 2
    assert len(_records(bench)) == 2
    assert isinstance(bench.stderr, io.StringIO)
    assert bench.stderr.getvalue().endswith("the screen is locked: stopping the bench before the next run\n")
    assert PKILL not in bench.fake.calls


async def test_reps_that_are_not_a_number_run_nothing(tmp_path: Path) -> None:
    bench = _loop_bench(tmp_path, Options(reps=math.nan))
    assert await bench.main() == 0
    assert bench.order == []
    assert not bench.out.exists()
    assert bench.out.parent.is_dir()
