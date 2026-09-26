from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from mcp import Client

from cua_jev import config
from cua_jev._json import JsonObject
from cua_jev.server import create_server
from cua_jev.tools.session import Session

# Read at import time: the autouse `isolated_env` fixture clears CUA_* before each test runs.
_ENABLED = sys.platform == "darwin" and os.environ.get("CUA_JEV_E2E") == "1"
_REAL_ENV = dict(os.environ)

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(not _ENABLED, reason="set CUA_JEV_E2E=1 on a Mac with cua-driver to run"),
]


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in _REAL_ENV.items():
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    for directory in config.env_dirs():
        config.load_env(directory)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def _json(result: object) -> JsonObject:
    content = getattr(result, "content", None)
    assert isinstance(content, list)
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    assert getattr(result, "is_error", None) is not True, text
    parsed: JsonObject = json.loads(text)
    return parsed


def _candidate_id(observed: JsonObject, marker: str, kind: str = "click") -> str:
    candidates = observed.get("candidates")
    assert isinstance(candidates, list)
    for c in candidates:
        assert isinstance(c, dict)
        does, cid = c.get("does"), c.get("id")
        if c.get("kind") == kind and isinstance(does, str) and marker in does and isinstance(cid, str):
            return cid
    pytest.fail(f"no {kind} candidate mentions {marker}")


@pytest.mark.usefixtures("real_env")
async def test_observe_act_and_extract_on_calculator() -> None:
    session = Session()
    try:
        async with Client(create_server(session), mode="legacy") as client:
            view = _json(await client.call_tool("observe", {"app": "Calculator"}))

            clear = _candidate_id(view, "Clear")
            cleared = _json(
                await client.call_tool("act", {"app": "Calculator", "instruction": "clear", "candidateId": clear})
            )
            assert cleared["status"] in ("done", "no_effect"), cleared
            seven = _candidate_id(view, "Seven")
            acted = _json(
                await client.call_tool("act", {"app": "Calculator", "instruction": "press 7", "candidateId": seven})
            )
            assert acted["status"] == "done", acted

            read = _json(await client.call_tool("extract", {"app": "Calculator", "instruction": "the display"}))
            assert read["status"] == "done", read
            assert "7" in json.dumps(read.get("element"))
    finally:
        await session.close()


_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_APP = Path.home() / "Library" / "Caches" / "cua-jev" / "fixture" / "CuaJevFixture.app"


@pytest.fixture
def fixture_app(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    subprocess.run(["sh", str(_ROOT / "bench" / "fixture-app" / "build.sh")], check=True, capture_output=True)
    state = tmp_path / "state.json"
    title = f"cua-jev e2e {os.getpid()}"
    before = set(_fixture_pids())
    subprocess.run(
        ["open", "-g", "-n", str(_FIXTURE_APP), "--args", "--state", str(state), "--title", title], check=True
    )
    pid = None
    for _ in range(50):
        started = set(_fixture_pids()) - before
        if started and state.is_file():
            pid = started.pop()
            break
        subprocess.run(["sleep", "0.2"], check=True)
    assert pid is not None, "the fixture app did not start"
    try:
        yield pid, state
    finally:
        subprocess.run(["kill", str(pid)], check=False)


def _fixture_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-x", "CuaJevFixture"], capture_output=True, text=True, check=False).stdout
    return [int(p) for p in out.split()]


async def _state(path: Path, key: str, want: str) -> str:
    got = ""
    for _ in range(20):
        value = json.loads(await anyio.Path(path).read_text(encoding="utf-8")).get(key)
        got = value if isinstance(value, str) else ""
        if got == want:
            break
        await asyncio.sleep(0.1)
    return got


def _ids(observed: JsonObject) -> list[str]:
    candidates = observed.get("candidates")
    assert isinstance(candidates, list)
    ids = [c.get("id") for c in candidates if isinstance(c, dict)]
    return [i for i in ids if isinstance(i, str)]


@pytest.mark.usefixtures("real_env")
async def test_text_entry_on_a_placeholder_field_is_verified_and_ids_stay(fixture_app: tuple[int, Path]) -> None:
    pid, state = fixture_app
    session = Session()
    try:
        async with Client(create_server(session), mode="legacy") as client:
            first = _json(await client.call_tool("observe", {"pid": pid}))
            append = _candidate_id(first, '"Full name"', kind="append")

            typed = _json(
                await client.call_tool(
                    "act", {"pid": pid, "instruction": "type into the Full name field", "text": " Ada  Lovelace"}
                )
            )
            assert typed["status"] == "done", typed
            assert await _state(state, "name", " Ada  Lovelace") == " Ada  Lovelace"

            again = _json(await client.call_tool("observe", {"pid": pid}))
            assert _ids(again) == _ids(first)

            appended = _json(
                await client.call_tool(
                    "act", {"pid": pid, "instruction": "add to the name", "candidateId": append, "text": " "}
                )
            )
            assert appended["status"] == "done", appended
            assert await _state(state, "name", " Ada  Lovelace ") == " Ada  Lovelace "

            read = _json(await client.call_tool("extract", {"pid": pid, "instruction": "the Full name field"}))
            assert read["status"] == "done", read
            assert "Ada" in json.dumps(read.get("element"))
    finally:
        await session.close()
