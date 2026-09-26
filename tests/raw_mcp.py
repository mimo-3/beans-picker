"""Talks JSON-RPC to an in-process server and returns the raw wire dicts, key order included."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from mcp.client._memory import InMemoryTransport
from mcp.server import Server
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.message import SessionMessage


class RawClient:
    """Sends requests and notifications; responses are matched to their request id."""

    def __init__(
        self,
        read: ReadStream[SessionMessage | Exception],
        write: WriteStream[SessionMessage],
    ) -> None:
        self._read = read
        self._write = write
        self._next_id = 0
        self._early: dict[int | str, types.JSONRPCResponse | types.JSONRPCError] = {}

    async def send(self, method: str, params: dict[str, Any] | None) -> int:
        """Sends a request and returns its id without waiting for the answer."""
        self._next_id += 1
        request = types.JSONRPCRequest(jsonrpc="2.0", id=self._next_id, method=method, params=params)
        await self._write.send(SessionMessage(request))
        return self._next_id

    async def result(self, rid: int) -> dict[str, Any]:
        """The result of request `rid` (answers to other requests are kept for later)."""
        while rid not in self._early:
            incoming = await self._read.receive()
            if isinstance(incoming, Exception):
                raise incoming
            msg = incoming.message
            if isinstance(msg, types.JSONRPCResponse | types.JSONRPCError) and msg.id is not None:
                self._early[msg.id] = msg
        msg = self._early.pop(rid)
        if isinstance(msg, types.JSONRPCError):
            raise RuntimeError(f"request {rid} failed: {msg.error.message}")
        return msg.result

    def answered(self, rid: int) -> bool:
        return rid in self._early

    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        return await self.result(await self.send(method, params))

    async def notify(self, method: str, params: dict[str, Any] | None) -> None:
        await self._write.send(SessionMessage(types.JSONRPCNotification(jsonrpc="2.0", method=method, params=params)))

    async def initialize(self, version: str = "2025-06-18") -> dict[str, Any]:
        params = {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
        result = await self.request("initialize", params)
        await self.notify("notifications/initialized", None)
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        return await self.request("tools/call", _call_params(name, arguments))

    async def send_call(self, name: str, arguments: dict[str, Any] | None) -> int:
        return await self.send("tools/call", _call_params(name, arguments))


def _call_params(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return params


@asynccontextmanager
async def raw_client(server: Server[Any]) -> AsyncIterator[RawClient]:
    async with InMemoryTransport(server) as (read, write):
        yield RawClient(read, write)
