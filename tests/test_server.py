from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import pytest
from mcp import Client, MCPError
from mcp.types import INVALID_PARAMS

from beans_picker import server as server_module
from beans_picker._json import JsonObject
from beans_picker.errors import DriverError, DriverUnavailable, JevBadResponse, JevUnavailable, ToolError
from beans_picker.server import INSTRUCTIONS, create_server, error, run
from beans_picker.tools.session import Session
from tests.fakes import FakeDriver


async def _unlocked() -> bool:
    return False


async def _locked() -> bool:
    return True


async def _driver() -> FakeDriver:
    return FakeDriver()


def _text(result: object) -> JsonObject:
    content = getattr(result, "content", None)
    assert isinstance(content, list)
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    parsed: JsonObject = json.loads(text)
    return parsed


async def test_offers_observe_act_and_extract_and_reports_a_bad_target_as_a_tool_error() -> None:
    server = create_server(Session(_driver), screen_locked=_unlocked)
    async with Client(server, mode="legacy") as client:
        tools = (await client.list_tools()).tools
        assert sorted(t.name for t in tools) == ["act", "extract", "observe"]
        act = next(t for t in tools if t.name == "act")
        props = act.input_schema["properties"]
        for key in ("app", "pid", "windowId", "instruction", "text", "candidateId", "allowDestructive", "then"):
            assert key in props
        res = await client.call_tool("observe", {})
        assert res.is_error is True
        assert _text(res) == {"status": "failed", "code": "bad_target", "message": "give app, pid or windowId"}
        assert client.instructions == INSTRUCTIONS


def test_instructions_keep_their_lines() -> None:
    lines = INSTRUCTIONS.split("\n")
    assert len(lines) == 10
    assert lines[3].startswith("  status: done | unverified")
    assert lines[-1] == "Plan the steps yourself and read each step's status."


def test_error_is_a_failed_status_in_json() -> None:
    r = error("bad_target", 'say "hi"')
    assert r.is_error is True
    assert _text(r) == {"status": "failed", "code": "bad_target", "message": 'say "hi"'}


async def test_run_reports_success_as_json_text() -> None:
    async def fn() -> object:
        return {"status": "done", "p": 1.0, "text": "\u00e9"}

    r = await run(Session(_driver), fn, acts=True, screen_locked=_unlocked)
    assert r.is_error is False
    assert r.content[0].model_dump()["text"] == '{"status":"done","p":1,"text":"\u00e9"}'


@pytest.mark.parametrize(("acts", "verb"), [(True, "done"), (False, "read")])
async def test_run_refuses_while_the_screen_is_locked(acts: bool, verb: str) -> None:
    ran = False

    async def fn() -> object:
        nonlocal ran
        ran = True
        return {}

    r = await run(Session(_driver), fn, acts=acts, screen_locked=_locked)
    assert _text(r) == {
        "status": "failed",
        "code": "screen_locked",
        "message": f"the screen is locked; nothing was {verb}. Unlock it and call again",
    }
    assert not ran


@pytest.mark.parametrize(
    ("err", "code", "message"),
    [
        (ToolError("window_not_found", "pid 3 has no usable window"), "window_not_found", "pid 3 has no usable window"),
        (JevUnavailable("no key"), "jev_unavailable", "no key"),
        (JevBadResponse('"pick" has no noul'), "jev_bad_response", '"pick" has no noul'),
        (DriverError("click", "stale", "gone"), "driver_error", "click refused (stale): gone"),
        (DriverUnavailable("cannot start cua-driver"), "driver_unavailable", "cannot start cua-driver"),
        (RuntimeError("boom"), "internal", "boom"),
    ],
)
async def test_run_maps_failures_to_codes(err: Exception, code: str, message: str) -> None:
    async def fn() -> object:
        raise err

    r = await run(Session(_driver), fn, acts=False, screen_locked=_unlocked)
    assert r.is_error is True
    assert _text(r) == {"status": "failed", "code": code, "message": message}


async def test_a_failing_lock_check_is_internal() -> None:
    async def broken() -> bool:
        raise OSError("ioreg failed")

    async def fn() -> object:
        return {}

    r = await run(Session(_driver), fn, acts=False, screen_locked=broken)
    assert _text(r)["code"] == "internal"


async def test_calls_after_shutdown_are_refused() -> None:
    session = Session(_driver)
    await session.close()

    async def fn() -> object:
        return {}

    r = await run(session, fn, acts=False, screen_locked=_unlocked)
    assert _text(r) == {"status": "failed", "code": "internal", "message": "server is shutting down"}


async def test_tool_calls_never_interleave() -> None:
    session = Session(_driver)
    events: list[str] = []

    def body(name: str) -> Callable[[], Awaitable[object]]:
        async def fn() -> object:
            events.append(f"start {name}")
            await asyncio.sleep(0.01)
            events.append(f"end {name}")
            return {}

        return fn

    await asyncio.gather(*(run(session, body(n), acts=True, screen_locked=_unlocked) for n in "abc"))
    assert events == ["start a", "end a", "start b", "end b", "start c", "end c"]


async def test_unexpected_failures_are_logged_with_their_traceback(caplog: pytest.LogCaptureFixture) -> None:
    async def fn() -> object:
        raise RuntimeError("boom")

    async def refused() -> object:
        raise ToolError("bad_target", "give app, pid or windowId")

    with caplog.at_level(logging.WARNING, logger="beans_picker.server"):
        await run(Session(_driver), fn, acts=False, screen_locked=_unlocked)
        await run(Session(_driver), refused, acts=False, screen_locked=_unlocked)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "tool call failed (internal)"
    assert record.exc_info is not None


async def test_an_unknown_tool_is_a_protocol_error() -> None:
    server = create_server(Session(_driver), screen_locked=_unlocked)
    async with Client(server, mode="legacy") as client:
        with pytest.raises(MCPError) as info:
            await client.call_tool("nope", {})
    assert info.value.code == INVALID_PARAMS
    assert info.value.message == "Tool nope not found"


async def test_a_session_the_server_made_is_closed_when_it_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[Session] = []

    class Recording(Session):
        async def close(self) -> None:
            closed.append(self)
            await super().close()

    monkeypatch.setattr(server_module, "Session", lambda: Recording(_driver))
    async with Client(create_server(screen_locked=_unlocked), mode="legacy") as client:
        await client.list_tools()
    assert len(closed) == 1


async def test_a_given_session_is_left_open() -> None:
    session = Session(_driver)
    async with Client(create_server(session, screen_locked=_unlocked), mode="legacy") as client:
        await client.list_tools()

    async def fn() -> object:
        return {}

    r = await run(session, fn, acts=False, screen_locked=_unlocked)
    assert r.is_error is False
    await session.close()
