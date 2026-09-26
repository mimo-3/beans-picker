from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable

import httpx2
import pytest
from typesafe_sdk import Choice, Noul, Score

from cua_jev._json import JsonValue
from cua_jev.errors import JevBadResponse, JevUnavailable
from cua_jev.jev.client import MISSING_KEY, JevClient, JevUsage, Question, validate

QUESTIONS: dict[str, Question] = {
    "next": Choice(instructions="Which action?", criteria={"a0": "press 7", "a1": "press 8"}),
    "done": Noul(instructions="Is the goal done?"),
}

OK: dict[str, JsonValue] = {
    "answers": {
        "next": {"type": "choice", "choice": "a1", "confidence": 0.8, "probabilities": {"a1": 0.8, "a0": 0.2}},
        "done": {"type": "noul", "noul": 0.1},
    },
    "usage": {"input_tokens": 321, "output_tokens": 20},
}


class Recorder:
    """A MockTransport handler answering with one status and body, recording each request."""

    def __init__(self, status: int, body: object, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = {"content-type": "application/json", "x-request-id": "req_test", **(headers or {})}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        content = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return httpx2.Response(self.status, content=content, headers=self.headers)


def client(handler: Callable[[httpx2.Request], httpx2.Response], **kw: str) -> JevClient:
    return JevClient(api_key="sk-test-123456789", transport=httpx2.MockTransport(handler), **kw)


async def test_sends_the_pinned_model_and_accounts_usage_until_taken() -> None:
    rec = Recorder(200, OK)
    jev = client(rec, model="jev-latest")
    r = await jev.ask({"instruction": "x"}, QUESTIONS)
    nxt = r.answers["next"]
    assert isinstance(nxt, dict)
    assert nxt["choice"] == "a1"
    assert r.input_tokens == 321
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "jev-latest"
    assert body["state"] == {"instruction": "x"}
    first = jev.take_usage()
    assert (first.calls, first.input_tokens) == (1, 321)
    second = jev.take_usage()
    assert (second.calls, second.input_tokens) == (0, 0)
    assert r.request_id is None  # x-request-id is not the header Jev's request id comes in
    await jev.aclose()


async def test_rejects_answers_that_pick_an_option_we_did_not_offer() -> None:
    bad = copy.deepcopy(OK)
    answers = bad["answers"]
    assert isinstance(answers, dict)
    nxt = answers["next"]
    assert isinstance(nxt, dict)
    nxt["choice"] = "a9"
    with pytest.raises(JevBadResponse, match='"next" chose unknown option a9'):
        await client(Recorder(200, bad)).ask({"instruction": "x"}, QUESTIONS)


async def test_reports_api_errors_as_jev_unavailable() -> None:
    rec = Recorder(401, {"detail": "unauthorized"})
    with pytest.raises(JevUnavailable) as err:
        await client(rec).ask({"instruction": "x"}, QUESTIONS)
    assert str(err.value) == 'Jev API 401: {"detail":"unauthorized"}'
    assert len(rec.requests) == 1  # 401 is not retried


def test_refuses_to_start_without_a_key() -> None:
    with pytest.raises(JevUnavailable, match="JEV_API_KEY is not set"):
        JevClient()
    assert str(JevUnavailable(MISSING_KEY)).endswith("~/.config/cua-jev/.env.local)")


def test_an_empty_key_is_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_API_KEY", "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test-123456789")
    with pytest.raises(JevUnavailable, match="JEV_API_KEY is not set"):
        JevClient()


async def test_the_typesafe_key_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-fallback-1")
    rec = Recorder(200, OK)
    jev = JevClient(transport=httpx2.MockTransport(rec))
    await jev.ask({"instruction": "x"}, QUESTIONS)
    assert rec.requests[0].headers["authorization"] == "Bearer sk-fallback-1"


def test_a_malformed_key_is_reported_as_unavailable() -> None:
    with pytest.raises(JevUnavailable, match="printable ASCII"):
        JevClient(api_key="sk bad key")


def test_the_model_defaults_to_the_configured_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert JevClient(api_key="sk-1").model == "jev-latest"
    monkeypatch.setenv("CUA_JEV_MODEL", "jev-other")
    assert JevClient(api_key="sk-1").model == "jev-other"


async def test_keeps_the_request_id_of_the_response() -> None:
    rec = Recorder(200, OK, {"x-typesafe-request-id": "req_42"})
    r = await client(rec).ask({"instruction": "x"}, QUESTIONS)
    assert r.request_id == "req_42"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", 'Jev API 400: ""'),
        (b"plain words", 'Jev API 400: "plain words"'),
        ({"detail": "x" * 300}, 'Jev API 400: {"detail":"' + "x" * 189),
    ],
)
async def test_quotes_the_error_body_and_cuts_it_at_200_units(body: object, expected: str) -> None:
    with pytest.raises(JevUnavailable) as err:
        await client(Recorder(400, body)).ask({"instruction": "x"}, QUESTIONS)
    assert str(err.value) == expected


async def test_a_reply_that_is_not_an_object_is_a_bad_response() -> None:
    with pytest.raises(JevBadResponse):
        await client(Recorder(200, [1, 2])).ask({"instruction": "x"}, QUESTIONS)


async def test_other_failures_are_named_by_their_type() -> None:
    def boom(_: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("no route")

    with pytest.raises(JevUnavailable, match=r"^RuntimeError: no route$"):
        await client(boom).ask({"instruction": "x"}, QUESTIONS)


async def test_usage_adds_up_over_calls_and_ignores_missing_counts() -> None:
    no_usage = {"answers": OK["answers"]}
    jev = client(Recorder(200, no_usage))
    await jev.ask({"instruction": "x"}, QUESTIONS)
    await jev.ask({"instruction": "y"}, QUESTIONS)
    u = jev.take_usage()
    assert (u.calls, u.input_tokens) == (2, 0)
    assert u.as_output() == {"calls": 2, "inputTokens": 0, "ms": u.ms}
    assert JevUsage().as_output() == {"calls": 0, "inputTokens": 0, "ms": 0}


async def test_sdk_logging_never_carries_screen_text(caplog: pytest.LogCaptureFixture) -> None:
    sdk = logging.getLogger("typesafe_sdk")
    saved = (sdk.level, sdk.propagate, sdk.disabled)
    try:
        sdk.setLevel(logging.DEBUG)
        jev = client(Recorder(200, OK))
        with caplog.at_level(logging.DEBUG):
            await jev.ask({"screen_text": ["SENTINEL-4711"]}, QUESTIONS)
        assert "SENTINEL-4711" not in caplog.text
        assert not [r for r in caplog.records if r.name.startswith("typesafe_sdk")]
    finally:
        sdk.setLevel(saved[0])
        sdk.propagate, sdk.disabled = saved[1], saved[2]


def test_validate_names_the_first_bad_answer() -> None:
    choice = Choice(criteria={"o0": "a", "none": "b"})
    with pytest.raises(JevBadResponse, match=r'^missing or mistyped answer for "pick"$'):
        validate({"pick": choice}, {})
    with pytest.raises(JevBadResponse, match=r'^missing or mistyped answer for "pick"$'):
        validate({"pick": choice}, {"pick": {"type": "noul", "noul": 1}})
    with pytest.raises(JevBadResponse, match=r"^missing or mistyped answer for"):
        validate({"pick": choice}, {"pick": "o0"})
    with pytest.raises(JevBadResponse, match=r'^"pick" chose unknown option undefined$'):
        validate({"pick": choice}, {"pick": {"type": "choice"}})
    with pytest.raises(JevBadResponse, match=r'^"pick" chose unknown option null$'):
        validate({"pick": choice}, {"pick": {"type": "choice", "choice": None}})
    with pytest.raises(JevBadResponse, match=r'^"pick" chose unknown option $'):
        validate({"pick": choice}, {"pick": {"type": "choice", "choice": ""}})
    with pytest.raises(JevBadResponse, match=r'^"done" has no noul$'):
        validate({"done": Noul()}, {"done": {"type": "noul", "noul": True}})
    with pytest.raises(JevBadResponse, match=r'^"s" has no score$'):
        validate({"s": Score(criteria=["low", "high"])}, {"s": {"type": "score", "score": "1"}})
    validate(
        {"pick": choice, "done": Noul(), "s": Score(criteria=["low", "high"])},
        {
            "pick": {"type": "choice", "choice": "none"},
            "done": {"type": "noul", "noul": 0},
            "s": {"type": "score", "score": 1.5},
        },
    )
