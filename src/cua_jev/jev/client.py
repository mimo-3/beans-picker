"""JevClient: the TypeSafe SDK's `system_one` with a pinned model, answer validation and usage accounting."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

import httpx2
from pydantic import BaseModel, ConfigDict
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    RetryPolicy,
    Score,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeError,
)

from cua_jev import _json, config, log
from cua_jev._json import JsonValue
from cua_jev._numbers import round_half_up, scalar_text
from cua_jev._text import utf16_slice
from cua_jev.errors import JevBadResponse, JevUnavailable

if TYPE_CHECKING:
    from cua_jev.jev.state import JevState

_log = logging.getLogger(__name__)

type Question = Choice | Noul | Score

MISSING_KEY: Final = (
    "JEV_API_KEY is not set (put it in .env.local in the cua-jev checkout, or in ~/.config/cua-jev/.env.local)"
)
_REQUEST_ID_HEADER: Final = "x-typesafe-request-id"


class JevUsageOut(TypedDict):
    """How usage is written in tool output."""

    calls: int
    inputTokens: int
    ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class JevUsage:
    """Jev calls, input tokens and milliseconds spent since usage was last taken."""

    calls: int = 0
    input_tokens: int = 0
    ms: int = 0

    def as_output(self) -> JevUsageOut:
        return {"calls": self.calls, "inputTokens": self.input_tokens, "ms": self.ms}


@dataclass(frozen=True, slots=True, kw_only=True)
class AskResult:
    """Validated answers keyed by question id, with this call's time and input tokens."""

    answers: Mapping[str, JsonValue]
    ms: int
    input_tokens: int
    request_id: str | None = None


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answers: dict[str, JsonValue] = {}
    usage: dict[str, JsonValue] | None = None


class _Box:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: str | None = None


_REQUEST_ID: ContextVar[_Box | None] = ContextVar("cua_jev_request_id", default=None)


class _RequestIdTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        box = _REQUEST_ID.get()
        if box is not None:
            box.value = response.headers.get(_REQUEST_ID_HEADER)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class JevClient:
    """One Jev connection per process."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        key = api_key if api_key is not None else config.jev_api_key()
        if not key:
            raise JevUnavailable(MISSING_KEY)
        self.model: str = model if model is not None else config.model()
        # The SDK logs request bodies, which carry screen text: its logger stays off.
        log.silence("typesafe_sdk")
        try:
            self._client = AsyncTypeSafeClient(
                api_key=key,
                model=self.model,
                timeout=httpx2.Timeout(None),
                retry=RetryPolicy(max_retries=config.JEV_RETRIES, timeout=None),
                transport=_RequestIdTransport(transport if transport is not None else httpx2.AsyncHTTPTransport()),
            )
        except TypeSafeError as err:
            raise JevUnavailable(str(err)) from err
        self._usage = JevUsage()

    async def ask(self, state: JevState, questions: Mapping[str, Question]) -> AskResult:
        """Asks every question about `state` in one request; raises JevUnavailable or JevBadResponse."""
        t0 = time.perf_counter()
        box = _Box()
        token = _REQUEST_ID.set(box)
        try:
            reply = await self._client.system_one(state, questions, model=self.model, response_model=_Reply)
        except TypeSafeAPIResponseValidationError as err:
            _log.debug("jev reply not readable (request %s): %s", err.request_id, err)
            raise JevBadResponse(str(err)) from err
        except TypeSafeAPIError as err:
            _log.debug("jev error %s (request %s)", err.status, err.request_id)
            body = _json.dumps(err.body if err.body is not None else "")
            raise JevUnavailable(f"Jev API {err.status}: {utf16_slice(body, 200)}") from err
        except Exception as err:
            _log.debug("jev call failed: %s: %s", type(err).__name__, err)
            raise JevUnavailable(f"{type(err).__name__}: {err}") from err
        finally:
            _REQUEST_ID.reset(token)
        validate(questions, reply.answers)
        ms = round_half_up((time.perf_counter() - t0) * 1000)
        input_tokens = _input_tokens(reply.usage)
        u = self._usage
        self._usage = JevUsage(calls=u.calls + 1, input_tokens=u.input_tokens + input_tokens, ms=u.ms + ms)
        _log.debug("jev call: %d ms, %d input tokens", ms, input_tokens)
        return AskResult(answers=reply.answers, ms=ms, input_tokens=input_tokens, request_id=box.value or None)

    def take_usage(self) -> JevUsage:
        """Usage since the last call to this method."""
        u = self._usage
        self._usage = JevUsage()
        return u

    async def aclose(self) -> None:
        await self._client.aclose()


def _input_tokens(usage: Mapping[str, JsonValue] | None) -> int:
    n = usage.get("input_tokens") if usage is not None else None
    if isinstance(n, bool):
        return 0
    if isinstance(n, int):
        return n
    if isinstance(n, float) and n.is_integer():
        return int(n)
    return 0


def _is_number(v: JsonValue | None) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def validate(questions: Mapping[str, Question], answers: Mapping[str, JsonValue]) -> None:
    """Every question must be answered with its own type; a choice must be one of its criteria keys."""
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q.type:
            raise JevBadResponse(f'missing or mistyped answer for "{qid}"')
        if isinstance(q, Choice):
            choice = a.get("choice")
            if not isinstance(choice, str) or not choice or choice not in q.criteria:
                shown = "undefined" if "choice" not in a else scalar_text(choice)
                raise JevBadResponse(f'"{qid}" chose unknown option {shown}')
        elif isinstance(q, Noul):
            if not _is_number(a.get("noul")):
                raise JevBadResponse(f'"{qid}" has no noul')
        elif not _is_number(a.get("score")):
            raise JevBadResponse(f'"{qid}" has no score')
