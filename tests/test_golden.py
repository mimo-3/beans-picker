"""The server's wire answers against captures of what MCP clients of this server already see."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cua_jev import __version__
from cua_jev.server import create_server
from cua_jev.tools.session import Session
from tests.fakes import FakeDriver
from tests.raw_mcp import raw_client

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _validation_lines() -> list[dict[str, Any]]:
    lines = (FIXTURES / "validation_errors.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


async def _never_locked() -> bool:
    return False


async def _driver() -> FakeDriver:
    return FakeDriver()


def _server() -> Any:
    return create_server(Session(_driver), screen_locked=_never_locked)


def _nested_key_orders(value: Any) -> list[list[str]]:
    """Key order of every object below the top level of each property (what this package writes)."""
    out: list[list[str]] = []
    if isinstance(value, dict):
        out.append(list(value))
        for v in value.values():
            out.extend(_nested_key_orders(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_nested_key_orders(v))
    return out


async def test_tools_list_matches_the_capture() -> None:
    expected = _load("tools_list.json")
    async with raw_client(_server()) as client:
        await client.initialize()
        got = await client.request("tools/list", {})
    assert got == expected
    assert [t["name"] for t in got["tools"]] == ["observe", "act", "extract"]
    for mine, theirs in zip(got["tools"], expected["tools"], strict=True):
        # Property order and everything inside each property is as captured; the order of the keys
        # of a tool entry and of the schema's top level is the MCP SDK's wire model's.
        props, their_props = mine["inputSchema"]["properties"], theirs["inputSchema"]["properties"]
        assert list(props) == list(their_props)
        assert _nested_key_orders(props) == _nested_key_orders(their_props)


async def test_initialize_matches_the_capture_but_for_the_version() -> None:
    expected = _load("initialize.json")
    async with raw_client(_server()) as client:
        got = await client.initialize("2025-06-18")
    assert got["serverInfo"] == {"name": "cua-jev", "version": __version__}
    got["serverInfo"]["version"] = expected["serverInfo"]["version"]
    assert got == expected


@pytest.mark.parametrize("line", _validation_lines(), ids=lambda d: f"{d['tool']}:{json.dumps(d['arguments'])[:60]}")
async def test_invalid_calls_get_the_captured_error(line: dict[str, Any]) -> None:
    async with raw_client(_server()) as client:
        await client.initialize()
        got = await client.call_tool(line["tool"], line["arguments"])
    assert got["isError"] is True
    assert got["content"] == [{"type": "text", "text": line["text"]}]
