"""The internal gateway against the live dev store, with a fake provider behind httpx.MockTransport.

Every request goes through starlette's TestClient; the provider is an async MockTransport handler
that keeps each request it got. Rows are read back with WorldQuery. No socket leaves the box.
"""

import asyncio
import hashlib
import json
import math
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models.config import ModelSettings, load_catalog, load_model_settings
from grove.models.errors import EgressRefused, ModelRefused, NotConfigured
from grove.models.gateway import Gateway, create_app, serve, upstream_body
from grove.models.gateway_auth import mint, verify
from grove.models.http import MAX_WAIT, MODAL_ATTEMPTS
from grove.store.query import WorldQuery
from grove.writer.model_calls import set_budget_ceiling
from tests.runner.helpers import seeded_world

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
DAY = date(2026, 11, 2)
SECRET = b"s" * 32
CATALOG = load_catalog()
SPARK = "muse-spark-1.3"
GLIMMER = "muse-glimmer-30b"
VOICE = "muse-voice-transcribe-1.0"
SPARK_SPEC = CATALOG.model(SPARK)
GLIMMER_SPEC = CATALOG.model(GLIMMER)
META_URL = "https://api.meta.ai/v1/chat/completions"
GLIMMER_URL = "https://glimmer.example.test/v1/chat/completions"
RUN = "run-orchard-1"

SYSTEM = "You keep the orchard ledger for Lio Varn. Answer in one short line."
MESSAGES = [
    {"role": "system", "content": SYSTEM},
    {"role": "user", "content": "Which row holds the pear crates?"},
]
# The tool shape OpenCode 1.18.32 sends: its JSON schema carries a "$schema" key.
READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read a file from the orchard notes.",
        "parameters": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"filePath": {"type": "string", "description": "The file to read"}},
            "required": ["filePath"],
            "additionalProperties": False,
        },
    },
}

SPARK_DOC = {
    "id": "chatcmpl-orchard-1",
    "object": "chat.completion",
    "created": 1793610000,
    "model": SPARK,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Row nine holds the pear crates."},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_tokens": 1500,
        "prompt_tokens_details": {"cached_tokens": 1000},
        "completion_tokens_details": {"reasoning_tokens": 120},
    },
    "system_fingerprint": "fp-orchard",
}
# 200 uncached at 1.25, 1000 cached at 0.15 and 300 output at 4.25, per million.
SPARK_COST = 0.001675
SPARK_USAGE = {"input_tokens": 1200, "cached_tokens": 1000, "output_tokens": 300}

GLIMMER_DOC = {
    "id": "chatcmpl-glimmer-1",
    "object": "chat.completion",
    "model": GLIMMER,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "relevant"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
}

_ROWS = (
    "MATCH (c:ModelCall) RETURN c.job AS job, c.model AS model, c.route AS route, "
    "c.outcome AS outcome, c.effort AS effort, c.run_id AS run_id, "
    "c.input_tokens AS input_tokens, c.cached_tokens AS cached_tokens, "
    "c.output_tokens AS output_tokens, c.reasoning_tokens AS reasoning_tokens, c.cost AS cost, "
    "c.latency_ms AS latency_ms, c.at AS at, c.day AS day ORDER BY c.at, c.id"
)
_BUDGETS = (
    "MATCH (b:Budget) RETURN b.day AS day, b.spent AS spent, b.ceiling AS ceiling, "
    "b.state AS state ORDER BY b.day"
)


# The fake provider.


def answer(status: int = 200, doc: object = None, headers: dict | None = None):
    """A factory, so that each attempt gets a fresh response."""
    body = {} if doc is None else doc
    return lambda: httpx.Response(status, json=body, headers=headers or {})


def streamed(*chunks: bytes):
    async def body():
        for chunk in chunks:
            yield chunk

    return lambda: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body()
    )


class Upstream:
    """Answers in order; the last answer repeats. Keeps every request it got."""

    def __init__(self, *answers, on_call=None) -> None:
        self.answers = list(answers) or [answer(200, SPARK_DOC)]
        self.requests: list[httpx.Request] = []
        self.on_call = on_call

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self.on_call is not None:
            self.on_call(request)
        return self.answers[min(len(self.requests), len(self.answers)) - 1]()

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


class Waits(list):
    """The injected back-off: it keeps each wait and never sleeps."""

    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


class Chunks(httpx.AsyncByteStream):
    """A streamed provider body for driving Gateway._relay by hand."""

    def __init__(self, *chunks: bytes, cut_after: bool = False) -> None:
        self.chunks = chunks
        self.cut_after = cut_after
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.cut_after:
            raise httpx.ReadError("the provider dropped the connection")

    async def aclose(self) -> None:
        self.closed = True


def settings(daily_budget: float | None = 5.0) -> ModelSettings:
    return ModelSettings(
        meta_key="test-meta-key",
        glimmer_base_url="https://glimmer.example.test/v1",
        glimmer_key="test-glimmer-key",
        daily_budget=daily_budget,
        gateway_secret=SECRET,
    )


def token(world: str, job: str = "update_pass", run_id: str = RUN, **kwargs) -> str:
    return mint(SECRET, CATALOG, world_id=world, run_id=run_id, job=job, **kwargs)


def cache_key(job: str, prefix: str, world: str = "") -> str:
    digest = hashlib.sha256(f"{world}\0{prefix}".encode()).hexdigest()[:24]
    return f"grove:{job}:{digest}"


def sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"


def chunk(choices: list, usage: object = "absent") -> bytes:
    event = {"id": "chatcmpl-orchard-2", "object": "chat.completion.chunk", "model": SPARK}
    event["choices"] = choices
    if usage != "absent":
        event["usage"] = usage
    return sse(event)


DONE = b"data: [DONE]\n\n"
ROLE_CHOICES = [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
ROLE = chunk(ROLE_CHOICES)
CALL_OPEN = chunk(
    [
        {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_orchard_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": ""},
                    }
                ]
            },
            "finish_reason": None,
        }
    ]
)
CALL_ARGS = chunk(
    [
        {
            "index": 0,
            "delta": {
                "tool_calls": [{"index": 0, "function": {"arguments": '{"filePath":"orchard.md"}'}}]
            },
            "finish_reason": None,
        }
    ]
)
STREAM_USAGE = {
    "prompt_tokens": 5000,
    "completion_tokens": 80,
    "total_tokens": 5080,
    "prompt_tokens_details": {"cached_tokens": 4096},
    "completion_tokens_details": {"reasoning_tokens": 30},
}
FINISH = chunk([{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], STREAM_USAGE)
# 904 uncached at 1.25, 4096 cached at 0.15 and 80 output at 4.25, per million.
STREAM_COST = 0.0020844


# The store.


def read_rows(driver, world: str) -> list[dict]:
    return WorldQuery(driver, world).read(_ROWS)


def read_budgets(driver, world: str) -> list[dict]:
    return WorldQuery(driver, world).read(_BUDGETS)


def expected_row(**fields) -> dict:
    base = {
        "job": "update_pass",
        "model": SPARK,
        "route": "meta",
        "outcome": "ok",
        "effort": "low",
        "run_id": RUN,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "latency_ms": 0,
        "at": NOW,
        "day": DAY,
    }
    return base | fields


def assert_rows(rows: list[dict], expected: list[tuple[dict, float]]) -> None:
    assert len(rows) == len(expected), rows
    for got, (want, cost) in zip(rows, expected, strict=True):
        got = dict(got)
        got_cost = got.pop("cost")
        assert got == want
        assert got_cost == pytest.approx(cost, abs=1e-12)


def error_of(response) -> dict:
    return response.json()["error"]


# Fixtures.


@pytest.fixture
def frozen_clock():
    ticks = TestClock()
    ticks.set(NOW)
    with clock.using(ticks):
        yield ticks


@dataclass
class Rig:
    client: TestClient
    upstream: Upstream
    waits: Waits
    gateway: Gateway

    def post(self, run_token: str, body: dict):
        return self.client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {run_token}"}
        )


@pytest.fixture
def world(driver, make_world):
    """The meter records through the writer, and the writer locks a real World row."""
    return seeded_world(driver, make_world())


@pytest.fixture
def make_rig(driver, frozen_clock):
    with ExitStack() as stack:

        def make(*answers, daily_budget=5.0, on_call=None, raise_server_exceptions=True) -> Rig:
            upstream = Upstream(*answers, on_call=on_call)
            waits = Waits()
            gateway = Gateway(
                driver,
                settings=settings(daily_budget),
                catalog=CATALOG,
                transport=httpx.MockTransport(upstream),
                sleep=waits,
            )
            client = stack.enter_context(
                TestClient(create_app(gateway), raise_server_exceptions=raise_server_exceptions)
            )
            return Rig(client, upstream, waits, gateway)

        yield make


# Where the gateway listens, and what it needs to start.


def test_gateway_host_defaults_to_loopback():
    assert ModelSettings().gateway_host == "127.0.0.1"
    loaded = load_model_settings({})
    assert (loaded.gateway_host, loaded.gateway_port) == ("127.0.0.1", 18300)


def test_serve_binds_the_loopback_host(monkeypatch):
    import uvicorn

    import grove.store.connection as connection

    seen: dict = {}

    class StubDriver:
        closed = False

        def close(self) -> None:
            StubDriver.closed = True

    def fake_run(app, **kwargs) -> None:
        seen.update(kwargs, app=app)

    monkeypatch.setattr(connection, "open_driver", StubDriver)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    for name in (
        "GROVE_GATEWAY_HOST",
        "GROVE_GATEWAY_PORT",
        "GROVE_META_BASE_URL",
        "GROVE_GLIMMER_BASE_URL",
        "GROVE_DAILY_BUDGET_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROVE_GATEWAY_SECRET", "s" * 32)

    serve()

    assert (seen["host"], seen["port"]) == ("127.0.0.1", 18300)
    assert StubDriver.closed


@pytest.mark.parametrize("secret", [None, b"s" * 31])
def test_gateway_refuses_to_start_without_a_long_secret(driver, secret):
    with pytest.raises(NotConfigured):
        Gateway(driver, settings=ModelSettings(meta_key="test-meta-key", gateway_secret=secret))


def test_gateway_client_goes_through_the_egress_allowlist(driver):
    upstream = Upstream()
    gateway = Gateway(
        driver, settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(upstream)
    )

    async def drive() -> list[str]:
        refused = []
        for url in (
            "https://orchard.example.test/v1/chat/completions",
            "http://api.meta.ai/v1/chat/completions",
            "https://api.meta.ai:8443/v1/chat/completions",
        ):
            try:
                await gateway.client.post(url, json={})
            except EgressRefused:
                refused.append(url)
        await gateway.aclose()
        return refused

    assert len(asyncio.run(drive())) == 3
    assert upstream.requests == []


# The run token.


def _no_header(world, ticks):
    return {}


def _basic_scheme(world, ticks):
    return {"Authorization": f"Basic {token(world)}"}


def _empty_bearer(world, ticks):
    return {"Authorization": "Bearer   "}


def _other_secret(world, ticks):
    other = mint(b"o" * 32, CATALOG, world_id=world, run_id=RUN, job="update_pass")
    return {"Authorization": f"Bearer {other}"}


def _swapped_payload(world, ticks):
    prefix, _, signature = token(world, job="update_pass").split(".")
    payload = token(world, job="consolidation").split(".")[1]
    return {"Authorization": f"Bearer {prefix}.{payload}.{signature}"}


def _expired(world, ticks):
    run_token = token(world, ttl_seconds=60)
    ticks.advance(seconds=60)
    return {"Authorization": f"Bearer {run_token}"}


@pytest.mark.parametrize(
    "headers",
    [_no_header, _basic_scheme, _empty_bearer, _other_secret, _swapped_payload, _expired],
    ids=["no-header", "basic-scheme", "empty-bearer", "other-secret", "swapped", "expired"],
)
def test_bad_run_token_gets_401_and_no_call(driver, world, make_rig, frozen_clock, headers):
    rig = make_rig()

    response = rig.client.post(
        "/v1/chat/completions",
        json={"model": SPARK, "messages": MESSAGES},
        headers=headers(world, frozen_clock),
    )

    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"
    assert error_of(response)["retryable"] is False
    assert rig.upstream.requests == []
    assert read_rows(driver, world) == []


def test_token_is_valid_until_its_last_second(world, make_rig, frozen_clock):
    rig = make_rig()
    run_token = token(world, ttl_seconds=60)

    frozen_clock.advance(seconds=59)
    assert rig.post(run_token, {"model": SPARK, "messages": MESSAGES}).status_code == 200
    frozen_clock.advance(seconds=1)
    late = rig.post(run_token, {"model": SPARK, "messages": MESSAGES})

    assert late.status_code == 401
    assert error_of(late)["message"] == "the run token has expired"
    assert len(rig.upstream.requests) == 1


def test_models_lists_the_chat_models_only(world, make_rig):
    rig = make_rig()

    listed = rig.client.get("/v1/models", headers={"Authorization": f"Bearer {token(world)}"})
    anonymous = rig.client.get("/v1/models")

    assert listed.status_code == 200
    assert listed.json() == {
        "object": "list",
        "data": [
            {"id": SPARK, "object": "model", "owned_by": "grove"},
            {"id": GLIMMER, "object": "model", "owned_by": "grove"},
        ],
    }
    assert anonymous.status_code == 401
    assert error_of(anonymous)["code"] == "unauthenticated"
    assert rig.upstream.requests == []


# Spark through Meta's Chat Completions.


def test_spark_call_is_shaped_sent_with_the_server_key_and_metered(driver, world, make_rig):
    rig = make_rig()
    run_token = token(world)
    client_body = {
        "model": SPARK,
        "messages": MESSAGES,
        "max_tokens": 100,
        "temperature": 0.2,
        "stop": ["\n\n"],
        "logprobs": True,
        "top_logprobs": 3,
        "tools": [READ_TOOL],
        "tool_choice": "required",
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"reasoning_strength": "high"},
    }

    response = rig.post(run_token, client_body)

    assert response.status_code == 200
    assert response.json() == SPARK_DOC
    [sent] = rig.upstream.requests
    assert (sent.method, str(sent.url)) == ("POST", META_URL)
    assert sent.headers["authorization"] == "Bearer test-meta-key"
    assert run_token not in json.dumps(dict(sent.headers))
    assert run_token.encode() not in sent.content
    assert json.loads(sent.content) == {
        "model": SPARK,
        "messages": MESSAGES,
        "max_tokens": 1024,
        "temperature": 0.2,
        "tools": [READ_TOOL],
        "tool_choice": "auto",
        "reasoning_effort": "low",
        "prompt_cache_key": cache_key("update_pass", SYSTEM, world),
    }
    assert_rows(
        read_rows(driver, world),
        [(expected_row(**SPARK_USAGE, reasoning_tokens=120), SPARK_COST)],
    )
    [budget] = read_budgets(driver, world)
    assert budget["day"] == DAY
    assert budget["spent"] == pytest.approx(SPARK_COST, abs=1e-12)
    assert (budget["ceiling"], budget["state"]) == (5.0, "open")


@pytest.mark.parametrize(
    ("asked", "sent"),
    [
        ({"max_tokens": 100}, 1024),
        ({"max_tokens": 1023}, 1024),
        ({"max_tokens": 1024}, 1024),
        ({"max_tokens": 8192}, 8192),
        ({}, 8192),
        ({"max_completion_tokens": 2000}, 2000),
        ({"max_completion_tokens": 100}, 1024),
        ({"max_tokens": 100, "max_completion_tokens": 3000}, 3000),
    ],
    ids=["100", "1023", "1024", "8192", "none", "completion-2000", "completion-100", "both"],
)
def test_output_limit_is_raised_to_the_floor(world, make_rig, asked, sent):
    rig = make_rig()

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES} | asked)

    assert response.status_code == 200
    [body] = rig.upstream.bodies()
    assert body["max_tokens"] == sent
    assert "max_completion_tokens" not in body


def test_n_other_than_one_is_refused(driver, world, make_rig):
    rig = make_rig()

    refused = rig.post(token(world), {"model": SPARK, "messages": MESSAGES, "n": 2})
    single = rig.post(token(world), {"model": SPARK, "messages": MESSAGES, "n": 1})

    assert refused.status_code == 400
    assert error_of(refused)["code"] == "invalid_arguments"
    assert error_of(refused)["message"] == "n must be 1"
    assert single.status_code == 200
    [body] = rig.upstream.bodies()
    assert "n" not in body
    assert len(read_rows(driver, world)) == 1


def test_caller_effort_is_dropped_for_the_job_effort(driver, world, make_rig):
    # D15: the job table sets the effort. An agent that asks for more cannot raise its spend.
    rig = make_rig()

    raised = rig.post(
        token(world), {"model": SPARK, "messages": MESSAGES, "reasoning_effort": "max"}
    )
    unknown = rig.post(
        token(world), {"model": SPARK, "messages": MESSAGES, "reasoning_effort": "ultra"}
    )

    assert (raised.status_code, unknown.status_code) == (200, 200)
    assert [body["reasoning_effort"] for body in rig.upstream.bodies()] == ["low", "low"]
    assert [row["effort"] for row in read_rows(driver, world)] == ["low", "low"]


def test_a_glimmer_job_on_spark_gets_spark_lowest_effort(driver, world, make_rig):
    # second_opinion's own effort is medium, but it belongs to Glimmer, not to Spark.
    rig = make_rig()

    response = rig.post(token(world, job="second_opinion"), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 200
    [body] = rig.upstream.bodies()
    assert body["reasoning_effort"] == "minimal"
    assert body["prompt_cache_key"] == cache_key("second_opinion", SYSTEM, world)
    assert [row["effort"] for row in read_rows(driver, world)] == ["minimal"]


@pytest.mark.parametrize(
    ("asked", "tools", "tool_choice"),
    [
        ({"tools": [READ_TOOL], "tool_choice": "required"}, [READ_TOOL], "auto"),
        ({"tools": [READ_TOOL], "tool_choice": "auto"}, [READ_TOOL], "auto"),
        (
            {
                "tools": [READ_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "read"}},
            },
            [READ_TOOL],
            "auto",
        ),
        ({"tools": [READ_TOOL], "tool_choice": "none"}, None, None),
        ({"tool_choice": "required"}, None, None),
        ({"tools": [READ_TOOL]}, [READ_TOOL], None),
    ],
    ids=["required", "auto", "named", "none", "no-tools", "unset"],
)
def test_meta_takes_only_auto_tool_choice(asked, tools, tool_choice):
    out = upstream_body(
        {"model": SPARK, "messages": MESSAGES} | asked,
        spec=SPARK_SPEC,
        effort="low",
        job="update_pass",
    )

    assert out.get("tools") == tools
    assert out.get("tool_choice") == tool_choice


@pytest.mark.parametrize(
    ("messages", "asked", "key"),
    [
        (MESSAGES, {}, cache_key("cleanup_pass", SYSTEM)),
        (MESSAGES, {"prompt_cache_key": "orchard-shared"}, "orchard-shared"),
        (MESSAGES[1:], {}, None),
        ([{"role": "system", "content": [{"type": "text", "text": SYSTEM}]}], {}, None),
        ([{"role": "system", "content": ""}], {}, None),
    ],
    ids=["derived", "caller-key", "no-system", "list-content", "empty-system"],
)
def test_meta_cache_key_per_shared_prefix(messages, asked, key):
    out = upstream_body(
        {"model": SPARK, "messages": messages} | asked,
        spec=SPARK_SPEC,
        effort="low",
        job="cleanup_pass",
    )

    assert out.get("prompt_cache_key") == key


@pytest.mark.parametrize("messages", [[], None, "hello"], ids=["empty", "absent", "text"])
def test_a_body_with_no_message_list_is_refused(messages):
    body = {"model": SPARK} | ({} if messages is None else {"messages": messages})
    with pytest.raises(ModelRefused, match="messages must be a list"):
        upstream_body(body, spec=SPARK_SPEC, effort="low", job="cleanup_pass")


def test_glimmer_body_keeps_its_own_fields_and_drops_the_cache_key():
    # D15: Glimmer runs on its own sampling, so an agent's temperature, top_p and stop are dropped.
    out = upstream_body(
        {
            "model": GLIMMER,
            "messages": MESSAGES,
            "stop": ["<|eom|>"],
            "temperature": 0,
            "top_p": 0.1,
            "logprobs": True,
            "tools": [READ_TOOL],
            "tool_choice": "required",
            "prompt_cache_key": "orchard-shared",
            "reasoning_effort": "high",
        },
        spec=GLIMMER_SPEC,
        effort="medium",
        job="second_opinion",
    )

    assert out == {
        "model": GLIMMER,
        "messages": MESSAGES,
        "logprobs": True,
        "tools": [READ_TOOL],
        "tool_choice": "required",
        "max_tokens": 8192,
        "chat_template_kwargs": {"reasoning_strength": "medium"},
    }


# Streaming, in the shape OpenCode sends.


def test_opencode_stream_reaches_the_client_byte_for_byte(driver, world, make_rig):
    # The finish chunk comes in two pieces, so its usage is read across a chunk boundary.
    raw = (ROLE, CALL_OPEN, CALL_ARGS, FINISH[:41], FINISH[41:], DONE)
    rig = make_rig(streamed(*raw))
    opencode_body = {
        "model": SPARK,
        "max_tokens": 8192,
        "messages": MESSAGES,
        "tools": [READ_TOOL],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    response = rig.post(token(world), opencode_body)

    assert response.status_code == 200
    assert response.content == b"".join(raw)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    [sent] = rig.upstream.requests
    assert str(sent.url) == META_URL
    assert json.loads(sent.content) == {
        "model": SPARK,
        "max_tokens": 8192,
        "messages": MESSAGES,
        "tools": [READ_TOOL],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": "low",
        "prompt_cache_key": cache_key("update_pass", SYSTEM, world),
    }
    assert_rows(
        read_rows(driver, world),
        [
            (
                expected_row(
                    input_tokens=5000, cached_tokens=4096, output_tokens=80, reasoning_tokens=30
                ),
                STREAM_COST,
            )
        ],
    )


@pytest.mark.parametrize(
    ("asked", "sent"),
    [
        (None, {"include_usage": True}),
        ({"include_usage": False}, {"include_usage": True}),
        (
            {"include_usage": False, "continuous_usage_stats": True},
            {"include_usage": True, "continuous_usage_stats": True},
        ),
    ],
    ids=["absent", "false", "other-key-kept"],
)
def test_stream_always_asks_for_the_usage_chunk(world, make_rig, asked, sent):
    rig = make_rig(streamed(ROLE, FINISH, DONE))
    body = {"model": SPARK, "messages": MESSAGES, "stream": True}
    if asked is not None:
        body["stream_options"] = asked

    assert rig.post(token(world), body).status_code == 200

    [upstream] = rig.upstream.bodies()
    assert upstream["stream_options"] == sent


def test_usage_in_a_trailing_chunk_is_metered(driver, world, make_rig):
    # OpenAI-style servers send "usage": null on each chunk and the usage after the finish.
    text = chunk([{"index": 0, "delta": {"content": "Row nine."}, "finish_reason": None}], None)
    finish = chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}], None)
    trailer = chunk([], STREAM_USAGE)
    rig = make_rig(streamed(chunk(ROLE_CHOICES, None), text, finish, trailer, DONE))

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES, "stream": True})

    assert response.status_code == 200
    assert_rows(
        read_rows(driver, world),
        [
            (
                expected_row(
                    input_tokens=5000, cached_tokens=4096, output_tokens=80, reasoning_tokens=30
                ),
                STREAM_COST,
            )
        ],
    )


def test_stream_that_ends_without_a_finish_is_a_provider_error(driver, world, make_rig):
    rig = make_rig(streamed(ROLE, CALL_OPEN))

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES, "stream": True})

    assert response.status_code == 200
    assert response.content == ROLE + CALL_OPEN
    # No usage chunk came, so the row carries the estimate: four characters a token of the
    # messages and tools sent, and of the text streamed (none here).
    estimate = math.ceil((len(json.dumps(MESSAGES)) + len(json.dumps([]))) / 4)
    assert_rows(
        read_rows(driver, world),
        [(expected_row(outcome="provider_error", input_tokens=estimate), estimate * 1.25e-6)],
    )


class Gated(httpx.AsyncByteStream):
    """A provider body that holds its finish chunk until the test opens the gate."""

    def __init__(self, first: bytes, rest: tuple[bytes, ...]) -> None:
        self.first = first
        self.rest = rest
        self.gate = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.first
        await self.gate.wait()
        for piece in self.rest:
            yield piece

    async def aclose(self) -> None:
        self.closed = True


def test_client_that_stops_reading_is_metered_as_client_closed(driver, world, frozen_clock):
    # The client leaves before the finish chunk, which carries Meta's usage. The pump reads on
    # and records the real tokens, so the spend counts against the ceiling and the run cap.
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(Upstream()),
        sleep=Waits(),
    )
    body = Gated(ROLE, (FINISH, DONE))
    response = httpx.Response(200, stream=body)
    claims = verify(SECRET, CATALOG, token(world))

    async def drive() -> bytes:
        relay = gateway._relay(response, claims, SPARK_SPEC, "low", clock.monotonic())
        first = await relay.__anext__()
        await relay.aclose()
        body.gate.set()
        await gateway.aclose()
        return first

    assert asyncio.run(drive()) == ROLE
    assert body.closed and response.is_closed
    assert_rows(
        read_rows(driver, world),
        [
            (
                expected_row(
                    outcome="client_closed",
                    input_tokens=5000,
                    cached_tokens=4096,
                    output_tokens=80,
                    reasoning_tokens=30,
                ),
                STREAM_COST,
            )
        ],
    )


def test_stream_cut_by_the_provider_keeps_its_usage(driver, world, frozen_clock):
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(Upstream()),
        sleep=Waits(),
    )
    body = Chunks(ROLE, FINISH, cut_after=True)
    response = httpx.Response(200, stream=body)
    claims = verify(SECRET, CATALOG, token(world))

    async def drive() -> list[bytes]:
        relay = gateway._relay(response, claims, SPARK_SPEC, "low", clock.monotonic())
        got = [piece async for piece in relay]
        await gateway.aclose()
        return got

    assert asyncio.run(drive()) == [ROLE, FINISH]
    assert body.closed
    usage = {"input_tokens": 5000, "cached_tokens": 4096, "output_tokens": 80}
    assert_rows(
        read_rows(driver, world),
        [(expected_row(outcome="provider_error", **usage, reasoning_tokens=30), STREAM_COST)],
    )


# Models the gateway refuses.


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (f"{SPARK}-contributor", "a contributor model id is refused: never send data to it"),
        ("MUSE-SPARK-1.3-CONTRIBUTOR", "a contributor model id is refused: never send data to it"),
        ("muse-orchard-9", "unknown model"),
        (VOICE, "this model does not take chat"),
    ],
    ids=["contributor", "contributor-upper", "unknown", "voice"],
)
def test_refused_model_gets_400_and_no_call(driver, world, make_rig, model, message):
    rig = make_rig()

    response = rig.post(token(world), {"model": model, "messages": MESSAGES})

    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_arguments"
    assert error_of(response)["message"] == message
    assert rig.upstream.requests == []
    assert read_rows(driver, world) == []


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[1, 2]", b'{"messages": []}', b'{"model": 7}'],
    ids=["not-json", "array", "no-model", "model-not-text"],
)
def test_malformed_body_gets_400_and_no_call(driver, world, make_rig, content):
    rig = make_rig()

    response = rig.client.post(
        "/v1/chat/completions",
        content=content,
        headers={"Authorization": f"Bearer {token(world)}", "content-type": "application/json"},
    )

    assert response.status_code == 400
    assert error_of(response)["code"] == "invalid_arguments"
    assert rig.upstream.requests == []
    assert read_rows(driver, world) == []


# The day's ceiling and the run's token cap.


@pytest.mark.parametrize(
    ("job", "model"),
    [
        ("verification", SPARK),
        ("consolidation", SPARK),
        ("second_opinion", GLIMMER),
        ("update_pass", SPARK),
        ("cleanup_pass", SPARK),
        ("consolidation_pass", SPARK),
    ],
)
def test_deferred_job_waits_once_the_ceiling_is_reached(driver, world, make_rig, job, model):
    set_budget_ceiling(driver, world, DAY, 0.0)
    rig = make_rig(answer(200, GLIMMER_DOC if model == GLIMMER else SPARK_DOC))

    waits = rig.post(token(world, job=job), {"model": model, "messages": MESSAGES})

    assert waits.status_code == 429
    error = error_of(waits)
    assert (error["code"], error["retryable"]) == ("budget_reached", True)
    assert error["details"] == {
        "job": job,
        "day": "2026-11-02",
        "spent": 0.0,
        "ceiling": 0.0,
        "resets_at": "2026-11-03T00:00:00+00:00",
        "retry_after_seconds": 54000,
    }
    # An OpenAI client reads the wait from the header.
    assert waits.headers["retry-after"] == "54000"
    assert rig.upstream.requests == []
    assert read_rows(driver, world) == []


@pytest.mark.parametrize(
    ("job", "model"),
    [
        ("triage", GLIMMER),
        ("triage", SPARK),
        ("triage_fallback", SPARK),
        ("extraction", SPARK),
        ("transcription", SPARK),
    ],
)
def test_every_gateway_call_stops_at_the_ceiling_whatever_job_its_token_names(
    driver, world, make_rig, job, model
):
    # Build plan section 4: an agent call stops at the ceiling. The jobs that continue past it are
    # the ones code runs through the layer, and an agent token must not reach that exemption.
    set_budget_ceiling(driver, world, DAY, 0.0)
    rig = make_rig(answer(200, GLIMMER_DOC if model == GLIMMER else SPARK_DOC))

    response = rig.post(token(world, job=job), {"model": model, "messages": MESSAGES})

    assert response.status_code == 429
    assert error_of(response)["code"] == "budget_reached"
    assert error_of(response)["details"]["job"] == job
    assert rig.upstream.requests == []
    assert read_rows(driver, world) == []


def test_spend_that_reaches_the_ceiling_defers_the_next_cleanup_pass(driver, world, make_rig):
    rig = make_rig(daily_budget=0.001)

    first = rig.post(token(world, job="cleanup_pass"), {"model": SPARK, "messages": MESSAGES})
    second = rig.post(token(world, job="cleanup_pass"), {"model": SPARK, "messages": MESSAGES})

    assert first.status_code == 200
    assert second.status_code == 429
    assert error_of(second)["code"] == "budget_reached"
    assert error_of(second)["details"]["spent"] == pytest.approx(SPARK_COST, abs=1e-12)
    assert error_of(second)["details"]["ceiling"] == 0.001
    assert len(rig.upstream.requests) == 1
    [budget] = read_budgets(driver, world)
    assert budget["spent"] == pytest.approx(SPARK_COST, abs=1e-12)
    assert (budget["ceiling"], budget["state"]) == (0.001, "reached")


def test_deferred_job_runs_again_after_the_day_resets(driver, world, make_rig, frozen_clock):
    set_budget_ceiling(driver, world, DAY, 0.0)
    rig = make_rig()

    frozen_clock.set(datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC))
    late = rig.post(token(world, job="cleanup_pass"), {"model": SPARK, "messages": MESSAGES})
    frozen_clock.set(datetime(2026, 11, 3, 0, 0, tzinfo=UTC))
    next_day = rig.post(token(world, job="cleanup_pass"), {"model": SPARK, "messages": MESSAGES})

    assert late.status_code == 429
    assert error_of(late)["details"]["resets_at"] == "2026-11-03T00:00:00+00:00"
    assert next_day.status_code == 200
    [row] = read_rows(driver, world)
    assert (row["job"], row["day"]) == ("cleanup_pass", date(2026, 11, 3))


def test_run_token_cap_stops_the_run_once_its_tokens_reach_it(driver, world, make_rig):
    rig = make_rig()
    capped = token(world, run_id="run-orchard-cap", token_cap=2000)

    first = rig.post(capped, {"model": SPARK, "messages": MESSAGES})
    second = rig.post(capped, {"model": SPARK, "messages": MESSAGES})
    third = rig.post(capped, {"model": SPARK, "messages": MESSAGES})
    other_run = rig.post(
        token(world, run_id="run-orchard-other", token_cap=2000),
        {"model": SPARK, "messages": MESSAGES},
    )

    assert (first.status_code, second.status_code) == (200, 200)
    assert third.status_code == 429
    error = error_of(third)
    assert (error["code"], error["retryable"]) == ("run_cap_reached", False)
    assert error["details"] == {"used": 3000, "token_cap": 2000}
    assert other_run.status_code == 200
    assert len(rig.upstream.requests) == 3
    runs = sorted(row["run_id"] for row in read_rows(driver, world))
    assert runs == ["run-orchard-cap", "run-orchard-cap", "run-orchard-other"]


# Back-off on 429 and on a server that is not up yet.


def test_upstream_429_is_retried_until_the_stated_reset(driver, world, make_rig):
    rig = make_rig(
        answer(429, headers={"retry-after": "3"}),
        answer(429, headers={"x-ratelimit-reset-requests": "45s"}),
        answer(429),
        answer(200, SPARK_DOC),
    )

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 200
    assert response.json() == SPARK_DOC
    assert rig.waits == [3.0, 45.0, 8.0]
    bodies = rig.upstream.bodies()
    assert len(bodies) == 4
    assert all(body == bodies[0] for body in bodies)
    assert_rows(
        read_rows(driver, world),
        [(expected_row(**SPARK_USAGE, reasoning_tokens=120), SPARK_COST)],
    )


def test_upstream_429_with_a_far_reset_returns_at_once_with_the_wait(driver, world, make_rig):
    # D15: a 429 backs off until the stated reset. Past MAX_WAIT the wait is the caller's, so
    # nothing is sent again before the reset.
    rig = make_rig(answer(429, headers={"x-ratelimit-reset-requests": "1m30s"}))

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "90"
    assert rig.waits == []
    assert len(rig.upstream.bodies()) == 1
    assert [row["outcome"] for row in read_rows(driver, world)] == ["rate_limited"]


def test_upstream_429_on_every_attempt_returns_rate_limited(driver, world, make_rig):
    rig = make_rig(
        answer(429, {"error": {"message": "slow down, orchard"}}, headers={"retry-after": "12"})
    )

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "rate_limited",
            "type": "rate_limited",
            "message": "slow down, orchard",
            "details": {"status": 429},
            "retryable": True,
        }
    }
    assert response.headers["retry-after"] == "12"
    assert rig.waits == [12.0, 12.0, 12.0]
    assert len(rig.upstream.requests) == 4
    assert_rows(read_rows(driver, world), [(expected_row(outcome="rate_limited"), 0.0)])


def test_upstream_429_without_a_reset_backs_off_and_sends_no_retry_after(driver, world, make_rig):
    rig = make_rig(answer(429))

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 429
    assert error_of(response)["code"] == "rate_limited"
    assert error_of(response)["message"] == "the model provider refused the call"
    assert "retry-after" not in response.headers
    assert rig.waits == [2.0, 4.0, 8.0]
    assert_rows(read_rows(driver, world), [(expected_row(outcome="rate_limited"), 0.0)])


def test_upstream_400_is_not_retried_and_is_metered(driver, world, make_rig):
    rig = make_rig(answer(400, {"error": {"message": "unknown field", "param": "secret-ish"}}))

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "provider_error",
        "type": "provider_error",
        "message": "unknown field",
        "details": {"status": 400},
        "retryable": False,
    }
    assert len(rig.upstream.requests) == 1
    assert rig.waits == []
    assert_rows(read_rows(driver, world), [(expected_row(outcome="provider_error"), 0.0)])


def test_spark_503_is_tried_four_times(driver, world, make_rig):
    rig = make_rig(answer(503))

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code == 503
    assert (error_of(response)["code"], error_of(response)["retryable"]) == (
        "provider_error",
        True,
    )
    assert len(rig.upstream.requests) == 4
    assert rig.waits == [2.0, 4.0, 8.0]
    assert_rows(read_rows(driver, world), [(expected_row(outcome="provider_error"), 0.0)])


def test_glimmer_503_is_retried_while_its_container_starts(driver, world, make_rig):
    cold = [answer(503)] * (MODAL_ATTEMPTS - 1)
    rig = make_rig(*cold, answer(200, GLIMMER_DOC))

    response = rig.post(token(world, job="triage"), {"model": GLIMMER, "messages": MESSAGES})

    assert response.status_code == 200
    assert response.json() == GLIMMER_DOC
    assert len(rig.upstream.requests) == MODAL_ATTEMPTS == 15
    assert rig.waits == [min(2.0**attempt, MAX_WAIT) for attempt in range(1, MODAL_ATTEMPTS)]
    [row] = read_rows(driver, world)
    assert (row["job"], row["route"], row["outcome"]) == ("triage", "modal", "ok")


def test_glimmer_503_on_every_attempt_stops_after_modal_attempts(driver, world, make_rig):
    rig = make_rig(answer(503))

    response = rig.post(token(world, job="triage"), {"model": GLIMMER, "messages": MESSAGES})

    assert response.status_code == 503
    assert len(rig.upstream.requests) == MODAL_ATTEMPTS
    [row] = read_rows(driver, world)
    assert (row["route"], row["outcome"]) == ("modal", "provider_error")


def test_upstream_200_that_is_not_json_is_still_metered(driver, world, make_rig):
    # D15: every call that reached a provider is metered, a failed one included.
    rig = make_rig(
        lambda: httpx.Response(200, content=b"<html>orchard proxy</html>"),
        raise_server_exceptions=False,
    )

    response = rig.post(token(world), {"model": SPARK, "messages": MESSAGES})

    assert response.status_code >= 500
    assert len(rig.upstream.requests) == 1
    [row] = read_rows(driver, world)
    assert row["outcome"] in ("provider_error", "invalid_output")


# Glimmer on Modal.


def test_glimmer_call_goes_to_modal_with_its_key_and_template_effort(
    driver, world, make_rig, frozen_clock
):
    # The provider takes 2.5 seconds: Glimmer is priced by its GPU seconds.
    rig = make_rig(
        answer(200, GLIMMER_DOC), on_call=lambda request: frozen_clock.advance(seconds=2.5)
    )
    run_token = token(world, job="second_opinion")

    response = rig.post(
        run_token,
        {
            "model": GLIMMER,
            "messages": MESSAGES,
            "max_tokens": 100,
            "prompt_cache_key": "orchard-shared",
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )

    assert response.status_code == 200
    assert response.json() == GLIMMER_DOC
    [sent] = rig.upstream.requests
    assert (sent.method, str(sent.url)) == ("POST", GLIMMER_URL)
    assert sent.headers["authorization"] == "Bearer test-glimmer-key"
    assert run_token.encode() not in sent.content
    assert json.loads(sent.content) == {
        "model": GLIMMER,
        "messages": MESSAGES,
        "max_tokens": 1024,
        "chat_template_kwargs": {"reasoning_strength": "medium"},
    }
    assert_rows(
        read_rows(driver, world),
        [
            (
                expected_row(
                    job="second_opinion",
                    model=GLIMMER,
                    route="modal",
                    effort="medium",
                    input_tokens=300,
                    output_tokens=40,
                    latency_ms=2500,
                    at=NOW + timedelta(seconds=2.5),
                ),
                2.5 * 0.001097,
            )
        ],
    )


@pytest.mark.parametrize(
    ("job", "asked", "strength"),
    [
        ("triage", None, "low"),
        # The job table sets the effort: an effort the agent sends is dropped.
        ("second_opinion", "high", "medium"),
        # consolidation's own effort is high, but it belongs to Spark: Glimmer's lowest applies.
        ("consolidation", None, "low"),
    ],
    ids=["job-effort", "caller-effort", "other-family-job"],
)
def test_glimmer_effort_goes_in_the_chat_template(driver, world, make_rig, job, asked, strength):
    rig = make_rig(answer(200, GLIMMER_DOC))
    body = {"model": GLIMMER, "messages": MESSAGES}
    if asked is not None:
        body["reasoning_effort"] = asked

    assert rig.post(token(world, job=job), body).status_code == 200

    [sent] = rig.upstream.bodies()
    assert sent["chat_template_kwargs"] == {"reasoning_strength": strength}
    assert "reasoning_effort" not in sent
    assert "prompt_cache_key" not in sent
    [row] = read_rows(driver, world)
    assert (row["job"], row["effort"]) == (job, strength)


def test_glimmer_drops_an_effort_it_does_not_take(driver, world, make_rig):
    rig = make_rig(answer(200, GLIMMER_DOC))

    response = rig.post(
        token(world, job="triage"),
        {"model": GLIMMER, "messages": MESSAGES, "reasoning_effort": "max"},
    )

    assert response.status_code == 200
    [sent] = rig.upstream.bodies()
    assert sent["chat_template_kwargs"] == {"reasoning_strength": "low"}
    assert "reasoning_effort" not in sent
    [row] = read_rows(driver, world)
    assert row["effort"] == "low"
