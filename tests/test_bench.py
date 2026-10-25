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
    BUILTIN_TOOLS,
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
    codex_argv,
    condition_order,
    main,
    mcp_servers,
    opencode_argv,
    opencode_config,
    parse_args,
    progress_line,
    prompt_for,
    run_id,
    selected,
    take,
)

from beans_picker._json import JsonObject, JsonValue, dumps
from beans_picker._proc import DEFAULT_MAX_BYTES, Completed, Runner
from beans_picker.errors import ProcessError
from tests.fakes import FakeRunner, RecordingSleep, fake_runner

TASKS = {t["id"]: t for t in load_tasks()}
NOW = datetime(2026, 9, 24, 12, 28, 41, 383_999, tzinfo=UTC)
FIXTURE_FIND = ("lsappinfo", "find", "bundleid=dev.beans-picker.fixture")
CALC_FIND = ("lsappinfo", "find", "bundleid=com.apple.calculator")
PKILL = ("pkill", "-x", "BeansPickerFixture")

type Step = Completed | Exception | Callable[[], Completed]


class ScriptedRunner:
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


def test_parse_args_defaults() -> None:
    assert parse_args([]) == Options(reps=1, model="sonnet", only=None, conditions=["a", "b"])


def test_parse_args_reads_every_flag() -> None:
    argv = ["--reps", "3", "--model", "opus", "--only", "calc-chain,fx-size-save", "--conditions", "b"]
    assert parse_args(argv) == Options(reps=3, model="opus", only=["calc-chain", "fx-size-save"], conditions=["b"])


def test_parse_args_reads_the_run_timeout() -> None:
    assert parse_args(["--run-timeout", "30"]).run_timeout == 30
    assert parse_args([]).run_timeout == 600
    assert math.isnan(parse_args(["--run-timeout", "soon"]).run_timeout)


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_parse_args_selects_the_harness_and_passes_through_the_model(harness: str) -> None:
    options = parse_args(["--harness", harness, "--model", "provider/model"])
    assert options.harness == harness
    assert options.model == "provider/model"


def test_parse_args_rejects_an_unknown_harness() -> None:
    with pytest.raises(ValueError, match="harness"):
        parse_args(["--harness", "other"])


def test_parse_args_rejects_a_harness_without_a_value() -> None:
    with pytest.raises(ValueError, match="--harness"):
        parse_args(["--harness"])


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


def test_run_id_is_the_utc_start_time_with_dashes() -> None:
    assert run_id("fx-name-spaces", "a", NOW) == "2026-09-24T12-28-41-383Z-fx-name-spaces-a"
    tokyo = NOW.astimezone(timezone(timedelta(hours=9)))
    assert run_id("calc-chain", "b", tokyo) == "2026-09-24T12-28-41-383Z-calc-chain-b"


def test_prompt_for() -> None:
    target = Target(pid=321, window_id=9, app_label='the "BeansPickerFixture" app')
    assert prompt_for(TASKS["fx-size-save"], target) == (
        'You are operating a macOS app with the tools you have. The target is the "BeansPickerFixture" app, '
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
    assert mcp_servers("b") == {"beans-picker": {"command": sys.executable, "args": ["-m", "beans_picker"]}}


def test_claude_argv_denies_driver_tools_only_in_a() -> None:
    cfg = Path("/w/mcp-a.json")
    tail = ["--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    head = ["-p", "P", "--model", "sonnet", "--tools", "", "--mcp-config", str(cfg), "--strict-mcp-config"]
    deny = ["--disallowedTools", *(f"mcp__cua-driver__{t}" for t in DRIVER_DENY)]
    assert claude_argv("a", "P", "sonnet", cfg) == [*head, "--allowedTools", "mcp__cua-driver", *deny, *tail]
    assert claude_argv("b", "P", "sonnet", cfg) == [*head, "--allowedTools", "mcp__beans-picker", *tail]
    assert len(DRIVER_DENY) == 10


@pytest.mark.parametrize("condition", ["a", "b"])
def test_codex_argv_isolates_the_tools_and_keeps_the_requested_model(condition: str) -> None:
    argv = codex_argv(condition, "the prompt", "gpt-6.1-sol")
    assert argv[:11] == [
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-6.1-sol",
        "-c",
    ]
    assert argv[-1] == "the prompt"
    config = dict(arg.split("=", 1) for arg in argv[11:-1:2])
    assert config["approval_policy"] == '"never"'
    assert config["features.shell_tool"] == "false"
    assert config["features.view_image"] == "false"
    assert config["features.goals"] == "false"
    assert config["web_search"] == '"disabled"'
    assert config["features.skip_host_skill_discovery"] == "true"
    assert config["project_doc_max_bytes"] == "0"
    servers = mcp_servers(condition)
    name, server = next(iter(servers.items()))
    assert isinstance(server, dict)
    assert json.loads(config[f"mcp_servers.{name}.command"]) == server["command"]
    assert json.loads(config[f"mcp_servers.{name}.args"]) == server["args"]
    assert config[f"mcp_servers.{name}.default_tools_approval_mode"] == '"approve"'
    names = {key.split(".")[1] for key in config if key.startswith("mcp_servers.")}
    assert names == {name}
    if condition == "a":
        assert json.loads(config[f"mcp_servers.{name}.disabled_tools"]) == list(DRIVER_DENY)
    else:
        assert not any(key.endswith(".disabled_tools") for key in config)


def test_codex_passes_the_mcp_servers_an_environment_that_survives_the_isolation() -> None:
    env = {"HOME": "/home/someone", "PATH": "/usr/bin"}
    config = dict(arg.split("=", 1) for arg in codex_argv("b", "p", "m", env)[11:-1:2])
    assert json.loads(config["mcp_servers.beans-picker.env"]) == env
    assert "mcp_servers.cua-driver.env" not in config


def test_opencode_argv_passes_the_provider_model_and_enables_auto_approval() -> None:
    assert opencode_argv("the prompt", "opencode/deepseek-v4.1-flash-free") == [
        "run",
        "--standalone",
        "--auto",
        "--format",
        "json",
        "-m",
        "opencode/deepseek-v4.1-flash-free",
        "the prompt",
    ]


@pytest.mark.parametrize("condition", ["a", "b"])
def test_opencode_config_denies_builtin_tools_and_allows_only_the_selected_mcp(condition: str) -> None:
    config = opencode_config(condition)
    mcp = config["mcp"]
    assert isinstance(mcp, dict)
    servers = mcp["servers"]
    assert isinstance(servers, dict)
    name, expected = next(iter(mcp_servers(condition).items()))
    assert isinstance(expected, dict)
    args = expected["args"]
    assert isinstance(args, list)
    assert servers == {name: {"type": "local", "command": [expected["command"], *args], "codemode": False}}
    permissions = config["permissions"]
    prefix = "cua_driver" if condition == "a" else "beans_picker"
    assert isinstance(permissions, list)
    assert permissions[: len(BUILTIN_TOOLS)] == [
        {"action": action, "resource": "*", "effect": "deny"} for action in BUILTIN_TOOLS
    ]
    assert permissions[len(BUILTIN_TOOLS)] == {"action": f"{prefix}_*", "resource": "*", "effect": "allow"}
    assert permissions[len(BUILTIN_TOOLS) + 1 :] == (
        [{"action": f"{prefix}_{tool}", "resource": "*", "effect": "deny"} for tool in DRIVER_DENY]
        if condition == "a"
        else []
    )
    assert "*" not in [rule["action"] for rule in permissions if isinstance(rule, dict)]


def test_opencode_config_gives_the_mcp_server_the_callers_environment() -> None:
    env = {"HOME": "/home/someone", "PATH": "/usr/bin"}
    config = opencode_config("b", env)
    mcp = config["mcp"]
    assert isinstance(mcp, dict)
    servers = mcp["servers"]
    assert isinstance(servers, dict)
    beans = servers["beans-picker"]
    assert isinstance(beans, dict)
    assert beans["environment"] == env


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("mcp__beans-picker__act", "act"),
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
    take(m, assistant("mcp__beans-picker__act", "mcp__beans-picker__act"))
    assert m.tool_calls == 3
    assert m.tool_names == {"(unnamed)": 1, "act": 2}


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


def test_take_counts_claude_tool_errors_without_failing_the_run() -> None:
    m = Metrics()
    take(
        m,
        dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "boom", "is_error": True}]},
            }
        ),
    )
    take(m, result_event(result="RESULT: success", is_error=False))
    assert m.tool_errors == 1
    assert m.is_error is False


def test_take_ignores_what_it_cannot_read() -> None:
    m = Metrics()
    for line in ["not json", "5", "null", '{"type":"assistant"}', '{"type":"user","message":"x"}', tool_result(None)]:
        take(m, line)
    assert m == Metrics()


def test_take_codex_counts_completed_calls_and_uses_the_final_agent_message() -> None:
    m = Metrics()
    call: JsonObject = {
        "id": "item_1",
        "type": "mcp_tool_call",
        "server": "beans-picker",
        "tool": "observe",
        "arguments": {},
        "result": None,
        "error": None,
        "status": "in_progress",
    }
    take(m, dumps({"type": "item.started", "item": call}), "codex")
    take(m, dumps({"type": "item.completed", "item": {**call, "status": "completed"}}), "codex")
    take(
        m,
        dumps({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "working"}}),
        "codex",
    )
    take(
        m,
        dumps({"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "RESULT: success"}}),
        "codex",
    )
    take(
        m,
        dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 15196,
                    "cached_input_tokens": 8960,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 7,
                    "reasoning_output_tokens": 0,
                },
            }
        ),
        "codex",
    )
    assert m.tool_calls == 1
    assert m.tool_names == {"observe": 1}
    assert m.result == "RESULT: success"
    assert m.is_error is False
    assert m.tokens == {"input": 6236, "output": 7, "cacheRead": 8960, "cacheWrite": 0}
    assert m.outcome["numTurns"] == 1


def test_take_codex_adds_usage_across_turns_and_reads_jev_results() -> None:
    m = Metrics()
    for _ in range(2):
        take(
            m,
            dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 12,
                        "cached_input_tokens": 2,
                        "cache_write_input_tokens": 3,
                        "output_tokens": 4,
                    },
                }
            ),
            "codex",
        )
    take(
        m,
        dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "tool": "mcp__beans-picker__act",
                    "status": "completed",
                    "result": {
                        "content": [{"type": "text", "text": '{"jev":{"calls":2,"inputTokens":300}}'}],
                        "structured_content": None,
                    },
                    "error": None,
                },
            }
        ),
        "codex",
    )
    assert m.tokens == {"input": 20, "output": 8, "cacheRead": 4, "cacheWrite": 6}
    assert m.outcome["numTurns"] == 2
    assert m.jev == {"calls": 2, "inputTokens": 300}
    assert m.tool_names == {"act": 1}


def test_take_codex_records_a_failed_mcp_call_as_a_tool_error() -> None:
    m = Metrics()
    take(
        m,
        dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "server": "probe",
                    "tool": "echo",
                    "arguments": {},
                    "result": None,
                    "error": {"message": "MCP tool call requires approval, but approval policy is never"},
                    "status": "failed",
                },
            }
        ),
        "codex",
    )
    assert m.tool_calls == 1
    assert m.tool_names == {"echo": 1}
    assert m.tool_errors == 1
    assert m.is_error is None


def test_take_codex_counts_other_tool_items_so_leaks_are_visible() -> None:
    m = Metrics()
    for item_type in ("command_execution", "file_change", "web_search"):
        take(m, dumps({"type": "item.completed", "item": {"type": item_type}}), "codex")
    for item_type in ("agent_message", "reasoning", "error"):
        take(m, dumps({"type": "item.completed", "item": {"type": item_type, "text": "x"}}), "codex")
    assert m.tool_calls == 3
    assert m.tool_names == {"!command_execution": 1, "!file_change": 1, "!web_search": 1}
    assert m.is_error is None


def test_take_codex_treats_only_a_failed_turn_as_an_error() -> None:
    transient = Metrics()
    take(transient, dumps({"type": "error", "message": "Reconnecting... 1/5"}), "codex")
    assert transient.is_error is None
    failed = Metrics()
    take(failed, dumps({"type": "turn.failed", "error": {"message": "failed"}}), "codex")
    assert failed.is_error is True


def test_take_opencode_adds_step_usage_and_reads_tool_results() -> None:
    m = Metrics()
    take(m, dumps({"type": "step_start", "timestamp": 1000, "part": {"type": "step-start"}}), "opencode")
    take(
        m,
        dumps(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "id": "call-1",
                    "tool": "beans-picker_observe",
                    "messageID": "tool-message",
                    "state": {"status": "completed", "input": {}, "output": '{"jev":{"calls":2,"inputTokens":300}}'},
                },
            }
        ),
        "opencode",
    )
    for _ in range(2):
        take(
            m,
            dumps(
                {
                    "type": "step_finish",
                    "timestamp": 1400,
                    "part": {
                        "type": "step-finish",
                        "reason": "tool-calls",
                        "cost": 0.02,
                        "tokens": {"input": 26, "output": 8, "reasoning": 0, "cache": {"read": 4, "write": 0}},
                    },
                }
            ),
            "opencode",
        )
    take(
        m,
        dumps(
            {
                "type": "text",
                "timestamp": 1500,
                "part": {
                    "type": "text",
                    "messageID": "final-message",
                    "text": "RESULT: success",
                    "time": {"start": 1450, "end": 1500},
                },
            }
        ),
        "opencode",
    )
    assert m.tool_calls == 1
    assert m.tool_names == {"observe": 1}
    assert m.result == "RESULT: success"
    assert m.is_error is False
    assert m.tokens == {"input": 52, "output": 16, "cacheRead": 8, "cacheWrite": 0}
    assert m.outcome == {"numTurns": 2, "costUsd": 0.04}
    assert m.jev == {"calls": 2, "inputTokens": 300}


def test_take_opencode_adds_reasoning_tokens_to_the_output() -> None:
    m = Metrics()
    take(
        m,
        dumps(
            {
                "type": "step_finish",
                "part": {"type": "step-finish", "tokens": {"input": 1, "output": 2, "reasoning": 3}},
            }
        ),
        "opencode",
    )
    assert m.tokens["output"] == 5


def test_take_opencode_keeps_only_the_last_steps_text() -> None:
    m = Metrics()
    for kind, text in [
        ("step_start", ""),
        ("text", "RESULT: success"),
        ("step_finish", ""),
        ("step_start", ""),
        ("text", "still working"),
        ("step_finish", ""),
    ]:
        part = {"type": kind, "text": text} if text else {"type": kind}
        take(m, dumps({"type": kind, "part": part}), "opencode")
    assert m.result == "still working"
    assert claimed(m.result) == "none"


def test_take_opencode_records_the_observed_provider_limit_error() -> None:
    m = Metrics()
    take(
        m,
        dumps(
            {
                "type": "error",
                "timestamp": 1792932189328,
                "sessionID": "session",
                "error": {"type": "provider.free-limit", "message": "Free usage limit reached.", "status": 429},
            }
        ),
        "opencode",
    )
    assert m.is_error is True


def test_take_opencode_records_the_observed_failed_tool_call_as_a_tool_error() -> None:
    m = Metrics()
    take(
        m,
        dumps(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "id": "call",
                    "tool": "cua-driver_get_window_state",
                    "state": {"status": "error", "input": {}, "error": "No tool named tool is currently available."},
                },
            }
        ),
        "opencode",
    )
    take(
        m,
        dumps({"type": "text", "part": {"type": "text", "messageID": "final", "text": "RESULT: success"}}),
        "opencode",
    )
    assert m.tool_calls == 1
    assert m.tool_names == {"get_window_state": 1}
    assert m.tool_errors == 1
    assert m.is_error is None


@pytest.mark.parametrize("harness", ["codex", "opencode"])
def test_take_ignores_malformed_or_unrelated_agent_events(harness: str) -> None:
    m = Metrics()
    for line in [
        "not json",
        "[]",
        "null",
        '{"type":"unrelated"}',
        '{"type":"item.completed","item":null}',
        '{"type":"tool_use","part":null}',
    ]:
        take(m, line, harness)
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


FULL_KEYS = [
    "runId", "rep", "task", "condition", "harness", "model", "pass", "claimed", "falseSuccess", "mismatches", "actual",
    "toolCalls", "toolNames", "toolErrors", "numTurns", "wallMs", "durationMs", "costUsd", "tokens", "jev",
    "focusSteals", "isError",
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
            [assistant("mcp__beans-picker__act")],
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
    rec = _record(PASSING, assistant("mcp__beans-picker__act"), FULL_RESULT)
    assert dumps(rec) == (
        '{"runId":"r","rep":1,"task":"calc-chain","condition":"b","harness":"claude","model":"sonnet","pass":true,'
        '"claimed":"success","falseSuccess":false,"mismatches":[],"actual":{"display":"8"},"toolCalls":1,'
        '"toolNames":{"act":1},"toolErrors":0,"numTurns":4,"wallMs":1500,"durationMs":1400,"costUsd":0.06,'
        '"tokens":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0},"jev":{"calls":0,"inputTokens":0},'
        '"focusSteals":0,"isError":false}'
    )


def test_record_leaves_duration_out_for_other_harnesses_and_flags_a_timeout() -> None:
    m = Metrics(timed_out=True, is_error=True)
    m.outcome["durationMs"] = 1200
    rec = build_record(
        rid="r",
        rep=1,
        task=TASKS["calc-chain"],
        condition="b",
        harness="opencode",
        model="provider/model",
        judge=PASSING,
        m=m,
        wall_ms=1300,
        steals=0,
    )
    assert "durationMs" not in rec
    assert rec["wallMs"] == 1300
    assert rec["timeout"] is True
    assert rec["isError"] is True


def test_record_preserves_the_selected_harness_before_the_model() -> None:
    rec = build_record(
        rid="r",
        rep=1,
        task=TASKS["calc-chain"],
        condition="b",
        harness="codex",
        model="gpt-6.1-sol",
        judge=PASSING,
        m=Metrics(),
        wall_ms=0,
        steals=0,
    )
    assert rec["harness"] == "codex"
    keys = list(rec)
    assert keys[keys.index("model") - 1] == "harness"


@pytest.mark.parametrize(("wall_ms", "seconds"), [(2500, "3s"), (2499, "2s"), (0, "0s"), (135628, "136s")])
def test_progress_line_rounds_half_up(wall_ms: int, seconds: str) -> None:
    m = Metrics(tool_calls=3, result="RESULT: success")
    line = progress_line("calc-chain", "a", 2, PASSING, m, wall_ms)
    assert line == f"calc-chain [a] rep 2: PASS claimed=success tools=3 {seconds}\n"
    assert (
        progress_line("calc-chain", "b", 1, {}, Metrics(), 0) == "calc-chain [b] rep 1: FAIL claimed=none tools=0 0s\n"
    )


async def _until(test: Callable[[], bool]) -> None:
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


async def test_pid_of_reads_quoted_and_unquoted_names() -> None:
    runner = fake_runner(
        {
            ("lsappinfo", "find", "q"): ok('ASN:0x0-0x5a:"Calculator"\n'),
            info("ASN:0x0-0x5a"): ok('"pid" = 1234\n'),
            ("lsappinfo", "find", "bare"): ok("ASN:0x0-0x5b:"),
            info("ASN:0x0-0x5b"): ok("pid = 31521 !cgsConnection  wallTime = 1.0 seconds\n"),
            ("lsappinfo", "find", "none"): ok("\n"),
            ("lsappinfo", "find", "nopid"): ok("ASN:0x1 x"),
            info("ASN:0x1"): ok("no pid here"),
        }
    )
    bench = Bench(options=Options(), tasks=[], runner=runner)
    assert await bench.pid_of("q") == 1234
    assert await bench.pid_of("bare") == 31521
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
    assert target == Target(pid=321, window_id=9, app_label='the "BeansPickerFixture" app', state_file=state)
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


def test_codex_home_copies_authentication_without_user_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "user-codex"
    source.mkdir()
    (source / "auth.json").write_text('{"token":"fixture"}', encoding="utf-8")
    (source / "config.toml").write_text("unwanted = true", encoding="utf-8")
    (source / "skills").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    bench = Bench(options=Options(harness="codex"), tasks=[])
    env = bench.agent_env(isolated, "b")
    assert env["CODEX_HOME"] == str(isolated)
    assert (isolated / "auth.json").read_text(encoding="utf-8") == '{"token":"fixture"}'
    assert (isolated / "auth.json").stat().st_mode & 0o777 == 0o600
    assert not (isolated / "config.toml").exists()
    assert not (isolated / "skills").exists()


def test_codex_home_allows_absent_authentication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    env = Bench(options=Options(harness="codex"), tasks=[]).agent_env(isolated, "a")
    assert env["CODEX_HOME"] == str(isolated)
    assert not (isolated / "auth.json").exists()


def test_opencode_environment_replaces_user_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG", "/unwanted/config.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"mcp":{"unwanted":{}}}')
    monkeypatch.setenv("HOME", "/unwanted/home")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    bench = Bench(options=Options(harness="opencode"), tasks=[])
    env = bench.agent_env(isolated, "b")
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"
    assert env["HOME"] == str(isolated / "home")
    for key in ("DATA", "CONFIG", "CACHE", "STATE"):
        assert Path(env[f"XDG_{key}_HOME"]).is_relative_to(isolated)
    assert Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(isolated)
    config = Path(env["OPENCODE_CONFIG"])
    assert config.is_relative_to(isolated)
    assert json.loads(config.read_text(encoding="utf-8")) == opencode_config("b", bench._mcp_env())


def test_opencode_keeps_the_real_home_for_its_mcp_server(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    env = Bench(options=Options(harness="opencode"), tasks=[]).agent_env(isolated, "b")
    config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    beans = config["mcp"]["servers"]["beans-picker"]
    assert beans["environment"]["HOME"] == os.environ["HOME"]
    assert beans["environment"]["PATH"] == os.environ["PATH"]
    assert env["HOME"] != os.environ["HOME"]


def test_clone_credentials_exports_and_imports_only_the_stored_accounts(tmp_path: Path) -> None:
    log = tmp_path / "auth.log"
    script = tmp_path / "opencode"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"log = {str(log)!r}\n"
        "if sys.argv[1:3] == ['auth', 'export']:\n"
        "    print(json.dumps([{'id': 'cred', 'value': 'secret'}]))\n"
        "elif sys.argv[1:3] == ['auth', 'import']:\n"
        "    with open(log, 'a') as out:\n"
        "        out.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    bench = Bench(options=Options(harness="opencode"), tasks=[], opencode=str(script))
    bench.clone_credentials(tmp_path, {"XDG_DATA_HOME": str(tmp_path / "data")})
    assert json.loads(log.read_text(encoding="utf-8")) == [{"id": "cred", "value": "secret"}]


def test_clone_credentials_says_nothing_when_export_fails(tmp_path: Path) -> None:
    script = tmp_path / "opencode"
    script.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n", encoding="utf-8")
    script.chmod(0o755)
    bench = Bench(options=Options(harness="opencode"), tasks=[], opencode=str(script))
    bench.clone_credentials(tmp_path, {"XDG_DATA_HOME": str(tmp_path / "data")})
    assert not (tmp_path / "data").exists()


async def test_run_agent_streams_and_saves_the_events(tmp_path: Path) -> None:
    usage: JsonObject = {
        "input_tokens": 5,
        "output_tokens": 6,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 8,
    }
    lines = [
        assistant("mcp__beans-picker__act"),
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
    m = await bench.run_agent("b", "the prompt", "rid")
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
    assert (bench.work / "rid.stderr.log").exists()


@pytest.mark.parametrize("harness", ["codex", "opencode"])
async def test_run_agent_uses_the_selected_binary_with_closed_stdin_and_isolated_cwd(
    tmp_path: Path, harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloned: list[dict[str, str]] = []
    monkeypatch.setattr(Bench, "clone_credentials", lambda self, directory, env: cloned.append(env))
    capture = tmp_path / "capture.json"
    script = tmp_path / harness
    event: JsonObject = (
        {"type": "item.completed", "item": {"type": "agent_message", "text": "RESULT: success"}}
        if harness == "codex"
        else {"type": "text", "part": {"text": "RESULT: success"}}
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(capture)!r}, 'w') as output:\n"
        "    json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}, output)\n"
        f"print({dumps(event)!r})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    bench = Bench(
        options=Options(harness=harness, model="provider/model"),
        tasks=[],
        work=work,
        codex=str(script),
        opencode=str(script),
    )
    metrics = await bench.run_agent("b", "the prompt", "r1")
    observed = json.loads(capture.read_text(encoding="utf-8"))
    expected = (
        codex_argv("b", "the prompt", "provider/model", bench._mcp_env())
        if harness == "codex"
        else opencode_argv("the prompt", "provider/model")
    )
    assert observed["argv"] == expected
    assert observed["stdin"] == ""
    assert bool(cloned) is (harness == "opencode")
    cwd = Path(observed["cwd"])
    assert cwd.parent == work
    assert cwd.name.startswith("bench-")
    assert not await asyncio.to_thread(cwd.exists)
    assert metrics.result == "RESULT: success"
    assert (work / "r1.stream.jsonl").read_text(encoding="utf-8") == dumps(event) + "\n"


@pytest.mark.parametrize("harness", ["codex", "opencode"])
async def test_run_agent_records_an_unsuccessful_exit(
    tmp_path: Path, harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Bench, "clone_credentials", lambda self, directory, env: None)
    script = tmp_path / "exit"
    script.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n", encoding="utf-8")
    script.chmod(0o755)
    bench = Bench(options=Options(harness=harness), tasks=[], work=tmp_path, codex=str(script), opencode=str(script))
    metrics = await bench.run_agent("a", "the prompt", "r1")
    assert metrics.is_error is True


async def test_a_run_past_its_deadline_is_killed_and_marked(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\nimport time\nprint('{{}}', flush=True)\ntime.sleep(600)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    bench = _bench(tmp_path, fake_runner(), claude=str(script))
    bench.options = Options(run_timeout=0.3)
    m = await asyncio.wait_for(bench.run_agent("b", "the prompt", "rid"), 30)
    assert m.is_error is True
    assert m.timed_out is True
    assert (bench.work / "rid.stderr.log").exists()


async def test_a_clean_exit_still_ends_what_the_agent_started(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"helper = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(600)'])\n"
        "open('helper.pid', 'w').write(str(helper.pid))\n"
        "print('{}', flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    bench = _bench(tmp_path, fake_runner(), claude=str(script))
    await bench.run_agent("b", "the prompt", "rid")
    helper = int((bench.work / "helper.pid").read_text(encoding="utf-8"))
    for _ in range(100):
        try:
            os.kill(helper, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the agent's helper is still running")


async def test_the_agents_stderr_goes_to_its_own_log(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stderr.write('a warning\\n')\nprint('{{}}')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    bench = _bench(tmp_path, fake_runner(), claude=str(script))
    await bench.run_agent("b", "the prompt", "rid")
    assert (bench.work / "rid.stderr.log").read_text(encoding="utf-8") == "a warning\n"


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
    running = asyncio.ensure_future(bench.run_agent("b", "the prompt", "rid"))
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


class LoopBench(Bench):
    order: list[tuple[str, str, int]]
    verdict: JsonObject
    metrics: Callable[[], Metrics]
    fake: FakeRunner

    async def one_run(self, task: Task, condition: str, rep: int) -> JsonObject:
        self.order.append((task["id"], condition, rep))
        return await Bench.one_run(self, task, condition, rep)

    async def setup(self, task: Task, rid: str) -> Target:
        return Target(pid=7, window_id=8, app_label="Calculator")

    async def run_agent(self, condition: str, prompt: str, rid: str) -> Metrics:
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


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_without_running(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([flag]) == 0
    output = capsys.readouterr().out
    assert output.startswith("usage: python -m bench.run")
    assert "--harness" in output
