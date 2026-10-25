"""Runs each task with an agent harness under cua-driver alone and under beans-picker."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO

from beans_picker._aio import Sleep
from beans_picker._json import JsonObject, JsonValue, dumps
from beans_picker._numbers import parse_number, round_half_up, scalar_text
from beans_picker._proc import Runner, run
from beans_picker._text import DIGIT, WS, WS_CLASS_BODY, trim
from beans_picker.driver import lock
from beans_picker.errors import ProcessError
from bench.judge import Task, load_tasks

HERE: Final = Path(__file__).resolve().parent
ROOT: Final = HERE.parent
WORK: Final = HERE / ".work"
OUT: Final = HERE / "results/runs.jsonl"
DRIVER: Final = Path.home() / ".local/bin/cua-driver"
FIXTURE_APP: Final = Path.home() / "Library/Caches/beans-picker/fixture/BeansPickerFixture.app"
FIXTURE_QUERY: Final = "bundleid=dev.beans-picker.fixture"
FIXTURE_PROCESS: Final = "BeansPickerFixture"
CALCULATOR_BUNDLE: Final = "com.apple.calculator"
TEXTEDIT_APP: Final = Path("/Applications/TextEdit.app")
DRIVER_MAX_BYTES: Final = 32 * 1024 * 1024
POLL: Final = 0.15
FRONT_INTERVAL: Final = 0.2

# Tools of cua-driver the baseline may not use: they raise apps, kill processes or read the whole screen.
DRIVER_DENY: Final = (
    "bring_to_front",
    "move_cursor",
    "kill_app",
    "get_desktop_state",
    "page",
    "replay_trajectory",
    "set_config",
    "install_ffmpeg",
    "start_recording",
    "stop_recording",
)

_ASN: Final = re.compile(rf'ASN:[^{WS_CLASS_BODY}:"]+')
_PID: Final = re.compile(rf'"pid"{WS}*={WS}*({DIGIT}+)')
_TOOL_PREFIX: Final = re.compile(r"^mcp__[^_]+(?:-[^_]+)*__")
_JEV_USAGE: Final = re.compile(rf'"jev":\{{"calls":({DIGIT}+),"inputTokens":({DIGIT}+)')
_CLAIM_SUCCESS: Final = re.compile(rf"RESULT:{WS}*success", re.IGNORECASE | re.ASCII)
_CLAIM_FAILURE: Final = re.compile(rf"RESULT:{WS}*failure", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True, slots=True, kw_only=True)
class Options:
    """The command line."""

    reps: float = 1
    harness: str = "claude"
    model: str = "sonnet"
    only: list[str] | None = None
    conditions: list[str] = field(default_factory=lambda: ["a", "b"])


def parse_args(argv: Sequence[str]) -> Options:
    """Options from `argv`; a flag without a value counts as absent."""

    def opt(name: str) -> str | None:
        if name not in argv:
            return None
        i = argv.index(name) + 1
        return argv[i] if i < len(argv) else None

    reps, model, only, conditions = opt("--reps"), opt("--model"), opt("--only"), opt("--conditions")
    harness = opt("--harness") or "claude"
    if harness not in ("claude", "codex", "opencode"):
        raise ValueError(f"unknown harness: {harness}")
    return Options(
        harness=harness,
        reps=1 if reps is None else parse_number(reps),
        model="sonnet" if model is None else model,
        only=None if only is None else only.split(","),
        conditions=["a", "b"] if conditions is None else conditions.split(","),
    )


def selected(tasks: Sequence[Task], only: Sequence[str] | None) -> list[Task]:
    """The tasks named in `only` (all without it), in file order."""
    return [t for t in tasks if only is None or t["id"] in only]


def condition_order(conditions: Sequence[str], rep: int) -> list[str]:
    """Which condition goes first alternates by repetition, so neither always meets a freshly started app."""
    return list(conditions) if rep % 2 == 1 else list(reversed(conditions))


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    pid: int
    window_id: int
    app_label: str
    state_file: Path | None = None


@dataclass(slots=True)
class Metrics:
    """What one agent stream reported."""

    tool_calls: int = 0
    tool_names: dict[str, int] = field(default_factory=dict)
    result: str | None = None
    _message_id: str | None = field(default=None, repr=False, compare=False)
    _started_ms: int | float | None = field(default=None, repr=False, compare=False)
    is_error: bool | None = None
    outcome: JsonObject = field(default_factory=dict)
    tokens: JsonObject = field(default_factory=lambda: {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0})
    jev: dict[str, int] = field(default_factory=lambda: {"calls": 0, "inputTokens": 0})


def run_id(task_id: str, condition: str, now: datetime) -> str:
    """`2026-09-24T12-28-41-383Z-<task>-<condition>`: the UTC start time, with `:` and `.` as `-`."""
    utc = now.astimezone(UTC)
    stamp = f"{utc.strftime('%Y-%m-%dT%H-%M-%S')}-{utc.microsecond // 1000:03d}Z"
    return f"{stamp}-{task_id}-{condition}"


def prompt_for(task: Task, target: Target) -> str:
    """The prompt both conditions get."""
    return "\n".join(
        [
            f"You are operating a macOS app with the tools you have. The target is {target.app_label}, "
            f"running as pid {target.pid}, window id {target.window_id}. Use only that window.",
            "Work in the background: never bring the app to the front and never activate it.",
            "",
            f"Task: {task['prompt']}",
            "",
            "When you have finished, end your final message with exactly one line: `RESULT: success` if "
            "the task is done, or `RESULT: failure` if it is not.",
        ]
    )


def mcp_servers(condition: str) -> JsonObject:
    """The one MCP server a condition gets."""
    if condition == "a":
        return {"cua-driver": {"command": str(DRIVER), "args": ["mcp"]}}
    return {"beans-picker": {"command": sys.executable, "args": ["-m", "beans_picker"]}}


def claude_argv(condition: str, prompt: str, model: str, mcp_config: Path) -> list[str]:
    deny = ["--disallowedTools", *(f"mcp__cua-driver__{t}" for t in DRIVER_DENY)] if condition == "a" else []
    return [
        "-p",
        prompt,
        "--model",
        model,
        "--tools",
        "",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__cua-driver" if condition == "a" else "mcp__beans-picker",
        *deny,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]


def codex_argv(condition: str, prompt: str, model: str) -> list[str]:
    config: JsonObject = {
        "approval_policy": "never",
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "project_root_markers": ["benchh-root"],
        "suppress_unstable_features_warning": True,
    }
    for feature in (
        "shell_tool",
        "apps",
        "plugins",
        "remote_plugin",
        "multi_agent",
        "hooks",
        "memories",
        "skill_mcp_dependency_install",
        "skill_search",
        "tool_suggest",
        "browser_use",
        "computer_use",
        "in_app_browser",
        "image_generation",
        "code_mode",
        "sleep_tool",
        "view_image",
        "goals",
    ):
        config[f"features.{feature}"] = False
    config["features.skip_host_skill_discovery"] = True
    for name, server in mcp_servers(condition).items():
        if isinstance(server, dict):
            for key, value in server.items():
                config[f"mcp_servers.{name}.{key}"] = value
            config[f"mcp_servers.{name}.default_tools_approval_mode"] = "approve"
            config[f"mcp_servers.{name}.required"] = True
            if condition == "a":
                config[f"mcp_servers.{name}.disabled_tools"] = list(DRIVER_DENY)
    return [
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        model,
        *(arg for key, value in config.items() for arg in ("-c", f"{key}={dumps(value)}")),
        prompt,
    ]


def opencode_argv(prompt: str, model: str) -> list[str]:
    return ["run", "--standalone", "--auto", "--format", "json", "-m", model, prompt]


def opencode_config(condition: str) -> JsonObject:
    servers: JsonObject = {}
    for name, server in mcp_servers(condition).items():
        if isinstance(server, dict):
            args = server.get("args")
            servers[name] = {
                "type": "local",
                "command": [server["command"], *(args if isinstance(args, list) else [])],
                "codemode": False,
            }
    prefix = "cua-driver" if condition == "a" else "beans-picker"
    permissions: list[JsonValue] = [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": f"{prefix}_*", "resource": "*", "effect": "allow"},
    ]
    if condition == "a":
        permissions += [{"action": f"{prefix}_{tool}", "resource": "*", "effect": "deny"} for tool in DRIVER_DENY]
    return {"mcp": {"servers": servers}, "permissions": permissions, "snapshots": False}


def _text_of(v: JsonValue) -> str:
    return "" if v is None else scalar_text(v)


def _objects(v: JsonValue) -> list[JsonObject]:
    return [b for b in v if isinstance(b, dict)] if isinstance(v, list) else []


def _count(v: JsonValue) -> JsonValue:
    return 0 if v is None else v


def take(m: Metrics, line: str, harness: str = "claude") -> None:
    """Account for one stream-json event; a line that is not JSON is skipped."""
    try:
        ev = json.loads(line)
    except ValueError:
        return
    if not isinstance(ev, dict):
        return
    if harness == "codex":
        _take_codex(m, ev)
        return
    if harness == "opencode":
        _take_opencode(m, ev)
        return
    message = ev.get("message")
    content = _objects(message.get("content")) if isinstance(message, dict) else []
    kind = ev.get("type")
    if kind == "assistant":
        for b in content:
            if b.get("type") != "tool_use":
                continue
            m.tool_calls += 1
            name = _TOOL_PREFIX.sub("", scalar_text(b["name"]) if "name" in b else "(unnamed)", count=1)
            m.tool_names[name] = m.tool_names.get(name, 0) + 1
    elif kind == "user":
        for b in content:
            if b.get("type") != "tool_result":
                continue
            body = b.get("content")
            if isinstance(body, list):
                text = "".join(_text_of(x.get("text")) if isinstance(x, dict) else "" for x in body)
            else:
                text = _text_of(body)
            usage = _JEV_USAGE.search(text)
            if usage:
                m.jev["calls"] += int(usage.group(1))
                m.jev["inputTokens"] += int(usage.group(2))
    elif kind == "result":
        raw = ev.get("usage")
        u: JsonObject = raw if isinstance(raw, dict) else {}
        m.result = _text_of(ev.get("result"))
        m.is_error = ev.get("is_error") is True
        m.outcome = {key: ev[src] for key, src in _OUTCOME_KEYS if src in ev}
        m.tokens = {
            "input": _count(u.get("input_tokens")),
            "output": _count(u.get("output_tokens")),
            "cacheRead": _count(u.get("cache_read_input_tokens")),
            "cacheWrite": _count(u.get("cache_creation_input_tokens")),
        }


def _tool(m: Metrics, name: str) -> None:
    m.tool_calls += 1
    m.tool_names[name] = m.tool_names.get(name, 0) + 1


def _jev(m: Metrics, text: str) -> None:
    usage = _JEV_USAGE.search(text)
    if usage:
        m.jev["calls"] += int(usage.group(1))
        m.jev["inputTokens"] += int(usage.group(2))


def _body(v: JsonValue) -> str:
    if isinstance(v, list):
        return "".join(_text_of(x.get("text")) for x in v if isinstance(x, dict))
    return _text_of(v)


def _number(v: JsonValue) -> int | float:
    return v if isinstance(v, int | float) and not isinstance(v, bool) else 0


def _add_tokens(m: Metrics, values: JsonObject) -> None:
    for key, value in values.items():
        m.tokens[key] = _number(m.tokens.get(key)) + _number(value)


def _turn(m: Metrics) -> None:
    m.outcome["numTurns"] = _number(m.outcome.get("numTurns")) + 1


def _take_codex(m: Metrics, ev: JsonObject) -> None:
    kind = ev.get("type")
    item = ev.get("item")
    if kind == "item.completed" and isinstance(item, dict):
        match item.get("type"):
            case "mcp_tool_call":
                _tool(m, _TOOL_PREFIX.sub("", _text_of(item.get("tool")), count=1))
                result = item.get("result")
                if isinstance(result, dict):
                    _jev(m, _body(result.get("content")))
                if item.get("error") or item.get("status") == "failed":
                    m.is_error = True
            case "agent_message":
                m.result = _text_of(item.get("text"))
    elif kind == "turn.completed":
        u = ev.get("usage")
        if isinstance(u, dict):
            cached = _number(u.get("cached_input_tokens"))
            _add_tokens(
                m,
                {
                    "input": max(0, _number(u.get("input_tokens")) - cached),
                    "output": u.get("output_tokens"),
                    "cacheRead": cached,
                    "cacheWrite": u.get("cache_write_input_tokens"),
                },
            )
        _turn(m)
        if m.is_error is None:
            m.is_error = False
    elif kind in ("error", "turn.failed"):
        m.is_error = True


def _take_opencode(m: Metrics, ev: JsonObject) -> None:
    kind = ev.get("type")
    stamp = ev.get("timestamp")
    if isinstance(stamp, int | float) and not isinstance(stamp, bool):
        if kind == "step_start" and m._started_ms is None:
            m._started_ms = stamp
        if m._started_ms is not None:
            m.outcome["durationMs"] = max(0, stamp - m._started_ms)
    part = ev.get("part")
    if kind == "error":
        m.is_error = True
    elif isinstance(part, dict):
        if kind == "text":
            message_id = _text_of(part.get("messageID"))
            text = _text_of(part.get("text"))
            m.result = (m.result or "") + text if message_id == m._message_id else text
            m._message_id = message_id
            if m.is_error is None:
                m.is_error = False
        elif kind == "tool_use":
            name = _text_of(part.get("tool"))
            for prefix in ("beans-picker_", "cua-driver_", "beans_picker_", "cua_driver_"):
                if name.startswith(prefix):
                    name = name.removeprefix(prefix)
                    break
            _tool(m, _TOOL_PREFIX.sub("", name, count=1))
            state = part.get("state")
            if isinstance(state, dict):
                _jev(m, _body(state.get("output")))
                if state.get("status") == "error":
                    m.is_error = True
        elif kind == "step_finish":
            u = part.get("tokens")
            if isinstance(u, dict):
                cache = u.get("cache")
                cache = cache if isinstance(cache, dict) else {}
                _add_tokens(
                    m,
                    {
                        "input": u.get("input"),
                        "output": u.get("output"),
                        "cacheRead": cache.get("read"),
                        "cacheWrite": cache.get("write"),
                    },
                )
            if "cost" in part:
                m.outcome["costUsd"] = _number(m.outcome.get("costUsd")) + _number(part["cost"])
            _turn(m)
            if m.is_error is None:
                m.is_error = False


_OUTCOME_KEYS: Final = (("numTurns", "num_turns"), ("durationMs", "duration_ms"), ("costUsd", "total_cost_usd"))


def claimed(result: str | None) -> str:
    """What the agent's final `RESULT:` line claims: success, failure or none."""
    text = result or ""
    if _CLAIM_SUCCESS.search(text):
        return "success"
    return "failure" if _CLAIM_FAILURE.search(text) else "none"


def build_record(
    *,
    rid: str,
    rep: int,
    task: Task,
    condition: str,
    model: str,
    harness: str = "claude",
    judge: JsonObject,
    m: Metrics,
    wall_ms: int,
    steals: int,
) -> JsonObject:
    """The runs.jsonl record, with its keys in their fixed order; values nobody reported are left out."""
    claim = claimed(m.result)
    record: JsonObject = {
        "runId": rid,
        "rep": rep,
        "task": task["id"],
        "condition": condition,
        "harness": harness,
        "model": model,
    }
    if "pass" in judge:
        record["pass"] = judge["pass"]
    record["claimed"] = claim
    record["falseSuccess"] = claim == "success" and not judge.get("pass")
    for key in ("mismatches", "actual"):
        if key in judge:
            record[key] = judge[key]
    record["toolCalls"] = m.tool_calls
    record["toolNames"] = dict(m.tool_names)
    if "numTurns" in m.outcome:
        record["numTurns"] = m.outcome["numTurns"]
    record["wallMs"] = wall_ms
    for key in ("durationMs", "costUsd"):
        if key in m.outcome:
            record[key] = m.outcome[key]
    record["tokens"] = dict(m.tokens)
    record["jev"] = dict(m.jev)
    record["focusSteals"] = steals
    if m.is_error is not None:
        record["isError"] = m.is_error
    return record


def progress_line(task_id: str, condition: str, rep: int, judge: JsonObject, m: Metrics, wall_ms: int) -> str:
    verdict = "PASS" if judge.get("pass") else "FAIL"
    seconds = round_half_up(wall_ms / 1000)
    return f"{task_id} [{condition}] rep {rep}: {verdict} claimed={claimed(m.result)} tools={m.tool_calls} {seconds}s\n"


class LineSplitter:
    """Splits a byte stream into lines at LF; UTF-8 is decoded across chunk boundaries."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""

    def feed(self, data: bytes) -> list[str]:
        """The lines completed by `data`, without their LF."""
        self._buf += self._decoder.decode(data)
        *lines, self._buf = self._buf.split("\n")
        return lines

    def rest(self) -> str:
        """What is left after the last LF once the stream has ended."""
        self._buf += self._decoder.decode(b"", final=True)
        return self._buf


async def front_pid(runner: Runner) -> int | None:
    """The pid of the frontmost app."""
    asn = trim((await runner(["lsappinfo", "front"])).stdout)
    m = _PID.search((await runner(["lsappinfo", "info", "-only", "pid", asn])).stdout)
    return int(m.group(1)) if m else None


class FrontWatch:
    """Samples the frontmost app while a run lasts and counts focus steals."""

    def __init__(self, pid: int, *, runner: Runner = run, interval: float = FRONT_INTERVAL) -> None:
        self.pid = pid
        self.steals = 0
        self._runner = runner
        self._interval = interval
        self._sample: asyncio.Task[None] | None = None
        self._ticker = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._sample is None or self._sample.done():
                self._sample = asyncio.create_task(self._take_sample())

    async def _take_sample(self) -> None:
        with contextlib.suppress(Exception):
            if await front_pid(self._runner) == self.pid:
                self.steals += 1

    async def stop(self) -> int:
        """Stop sampling, wait for a sample in flight, and return the number of steals."""
        self._ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._ticker
        if self._sample is not None:
            await self._sample
        return self.steals


async def _end_process_group(child: asyncio.subprocess.Process) -> None:
    if child.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    await asyncio.shield(child.wait())


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def _exists(path: Path) -> bool:
    return path.exists()


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def _make_dirs(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True, kw_only=True)
class Bench:
    """The benchmark with its outside world: processes, clocks, files and the `claude` binary."""

    options: Options
    tasks: list[Task]
    runner: Runner = run
    sleep: Sleep = asyncio.sleep
    now_ms: Callable[[], int] = _now_ms
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    screen_locked: Callable[[], Awaitable[bool]] = lock.screen_locked
    claude: str = "claude"
    codex: str = "codex"
    opencode: str = "opencode"
    work: Path = WORK
    out: Path = OUT
    stderr: TextIO = sys.stderr
    front_interval: float = FRONT_INTERVAL

    async def driver_call(self, tool: str, args: JsonObject) -> JsonObject:
        """One cua-driver tool call through its CLI."""
        out = await self.runner([str(DRIVER), "call", tool, dumps(args)], max_bytes=DRIVER_MAX_BYTES)
        result: JsonObject = json.loads(out.stdout)
        return result

    async def windows_of(self, pid: int) -> list[JsonObject]:
        windows = (await self.driver_call("list_windows", {"pid": pid})).get("windows")
        return _objects(windows)

    async def pid_of(self, query: str) -> int | None:
        """The pid of the app `lsappinfo find <query>` finds first."""
        asn = _ASN.search((await self.runner(["lsappinfo", "find", query])).stdout)
        if not asn:
            return None
        m = _PID.search((await self.runner(["lsappinfo", "info", "-only", "pid", asn.group(0)])).stdout)
        return int(m.group(1)) if m else None

    async def _quiet(self, argv: Sequence[str]) -> None:
        with contextlib.suppress(ProcessError):
            await self.runner(argv)

    async def setup_fixture(self, task: Task, rid: str) -> Target:
        """A fresh fixture window for the task (the previous one is ours and is quit first)."""
        await self._quiet(["pkill", "-x", FIXTURE_PROCESS])
        for _ in range(20):
            if not await self.pid_of(FIXTURE_QUERY):
                break
            await self.sleep(POLL)
        state_file = self.work / f"{rid}.state.json"
        _remove(state_file)
        title = f"Notes form ({task['id']})"
        extra = [arg for key, value in task["setup"].items() for arg in (f"--{key}", value)]
        argv = ["open", "-g", "-n", str(FIXTURE_APP), "--args", "--state", str(state_file), "--title", title]
        await self.runner([*argv, *extra])
        for _ in range(40):
            pid = await self.pid_of(FIXTURE_QUERY)
            win = next((w for w in await self.windows_of(pid) if w.get("title") == title), None) if pid else None
            if pid and win and _exists(state_file):
                return Target(
                    pid=pid,
                    window_id=_int(win.get("window_id")),
                    app_label='the "BeansPickerFixture" app',
                    state_file=state_file,
                )
            await self.sleep(POLL)
        raise RuntimeError("fixture did not start")

    async def setup_calculator(self) -> Target:
        """Calculator in Basic mode showing 0, set up in the background with cua-driver's CLI."""
        query = f"bundleid={CALCULATOR_BUNDLE}"
        if not await self.pid_of(query):
            await self.runner(["open", "-g", "-b", CALCULATOR_BUNDLE])
        for _ in range(40):
            pid = await self.pid_of(query)
            win = (
                next((w for w in await self.windows_of(pid) if w.get("is_on_screen") is not False), None)
                if pid
                else None
            )
            if pid and win:
                window_id = _int(win.get("window_id"))
                await self.driver_call("hotkey", {"pid": pid, "window_id": window_id, "keys": ["cmd", "1"]})
                for _ in range(3):
                    await self.driver_call("press_key", {"pid": pid, "window_id": window_id, "key": "escape"})
                judge = await self.run_judge("calc-chain", Target(pid=pid, window_id=window_id, app_label=""))
                actual = judge.get("actual")
                if not isinstance(actual, dict) or actual.get("display") != "0":
                    raise RuntimeError(f"Calculator did not clear (display {dumps(actual)})")
                return Target(pid=pid, window_id=window_id, app_label="Calculator")
            await self.sleep(POLL)
        raise RuntimeError("Calculator did not start")

    async def setup(self, task: Task, rid: str) -> Target:
        return await self.setup_fixture(task, rid) if task["app"] == "fixture" else await self.setup_calculator()

    async def run_judge(self, task_id: str, target: Target) -> JsonObject:
        """The judge's verdict, run from the repository root (a failed task exits 1 but still prints)."""
        if target.state_file is not None:
            where = ["--state", str(target.state_file)]
        else:
            where = ["--pid", str(target.pid), "--window", str(target.window_id)]
        out = await self.runner([sys.executable, "-m", "bench.judge", task_id, *where], check=False, cwd=ROOT)
        verdict: JsonObject = json.loads(out.stdout)
        return verdict

    def mcp_config(self, condition: str) -> Path:
        """Write the MCP config for `condition` into the work directory."""
        path = self.work / f"benchh-mcp-{condition}.json"
        _write_text(path, dumps({"mcpServers": mcp_servers(condition)}))
        return path

    def agent_env(self, directory: Path, condition: str) -> dict[str, str]:
        """Isolate agent settings and writable state; retain the environment needed by the MCP."""
        env = dict(os.environ)
        if self.options.harness == "codex":
            source = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            env["CODEX_HOME"] = str(directory)
            if source.is_file():
                auth = directory / "auth.json"
                shutil.copyfile(source, auth)
                auth.chmod(0o600)
            _write_text(directory / "benchh-root", "")
        elif self.options.harness == "opencode":
            for key in list(env):
                if key.startswith("OPENCODE_"):
                    del env[key]
            for key in ("DATA", "CONFIG", "CACHE", "STATE"):
                env[f"XDG_{key}_HOME"] = str(directory / f"benchh-{key.lower()}")
            env["OPENCODE_CONFIG_DIR"] = str(directory / "benchh-config")
            env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
            env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
            path = directory / "benchh-opencode.json"
            _write_text(path, dumps(opencode_config(condition)))
            env["OPENCODE_CONFIG"] = str(path)
            env["TMPDIR"] = str(directory)
        return env

    async def run_agent(self, condition: str, prompt: str, rid: str) -> Metrics:
        """Run the selected harness to the end, saving and accounting for its non-blank events."""
        harness = self.options.harness
        if harness == "claude":
            argv = claude_argv(condition, prompt, self.options.model, self.mcp_config(condition))
        elif harness == "codex":
            argv = codex_argv(condition, prompt, self.options.model)
        else:
            argv = opencode_argv(prompt, self.options.model)
        m = Metrics()
        stream_file = self.work / f"{rid}.stream.jsonl"
        with tempfile.TemporaryDirectory(prefix="benchh-", dir=self.work) as temporary:
            directory = Path(temporary)
            env = self.agent_env(directory, condition)
            child = await asyncio.create_subprocess_exec(
                getattr(self, harness),
                *argv,
                cwd=self.work if harness == "claude" else directory,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,  # end its MCP servers with the agent on cancellation
            )
            try:
                if child.stdout is None:  # pragma: no cover - stdout is always a pipe here
                    raise RuntimeError(f"{harness} has no stdout")
                splitter = LineSplitter()
                while chunk := await child.stdout.read(64 * 1024):
                    for line in splitter.feed(chunk):
                        if trim(line):
                            _append_text(stream_file, line + "\n")
                            take(m, line, harness)
                await child.wait()
            finally:
                await _end_process_group(child)
            rest = splitter.rest()
            if trim(rest):
                take(m, rest, harness)
            if child.returncode != 0:
                m.is_error = True
        return m

    async def one_run(self, task: Task, condition: str, rep: int) -> JsonObject:
        """Set up, run, judge and record one run."""
        rid = run_id(task["id"], condition, self.now())
        target = await self.setup(task, rid)
        watch = FrontWatch(target.pid, runner=self.runner, interval=self.front_interval)
        t0 = self.now_ms()
        try:
            m = await self.run_agent(condition, prompt_for(task, target), rid)
        finally:
            wall_ms = self.now_ms() - t0
            steals = await watch.stop()
        judge = await self.run_judge(task["id"], target)
        record = build_record(
            rid=rid,
            rep=rep,
            task=task,
            condition=condition,
            model=self.options.model,
            harness=self.options.harness,
            judge=judge,
            m=m,
            wall_ms=wall_ms,
            steals=steals,
        )
        _append_text(self.out, dumps(record) + "\n")
        self.stderr.write(progress_line(task["id"], condition, rep, judge, m, wall_ms))
        return record

    async def main(self) -> int:
        """Every repetition of every task under every condition; 2 when the screen locks."""
        _make_dirs(self.work, self.out.parent)
        if _exists(TEXTEDIT_APP) and await self.pid_of("bundleid=com.apple.TextEdit"):
            self.stderr.write("note: TextEdit is running; no task uses it, and none touches it\n")
        rep = 1
        while rep <= self.options.reps:
            for task in self.tasks:
                for condition in condition_order(self.options.conditions, rep):
                    if await self.screen_locked():
                        self.stderr.write("the screen is locked: stopping the bench before the next run\n")
                        return 2
                    await self.one_run(task, condition, rep)
            rep += 1
        await self._quiet(["pkill", "-x", FIXTURE_PROCESS])
        return 0


def _int(v: JsonValue) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    raise RuntimeError(f"not an integer id: {dumps(v)}")


USAGE = (
    "usage: python -m bench.run [--reps N] [--harness claude|codex|opencode] "
    "[--model M] [--only id,...] [--conditions a,b]"
)


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "-h" in args or "--help" in args:
        print(USAGE)
        return 0
    options = parse_args(args)
    bench = Bench(options=options, tasks=selected(load_tasks(), options.only))
    return asyncio.run(bench.main())


if __name__ == "__main__":
    sys.exit(main())
