"""Regression tests for the second fresh R4 pass, each red before its fix.

- A POST that reached the provider was sent again on a read timeout or a 500, 502 or 504, and the
  whole call left one row with zero tokens, so its spend could pass the ceiling unseen.
- Server-side tools (a web search, a remote MCP server) went on to the provider: a provider then
  reaches out for the agent, past the allowlist.
- The gateway set no upper bound on the output limit.
- A stream sent compressed reached the client as gzip bytes, and its usage chunk was never read.
- A stream's error body that could not be read left no row.
- A run token with a non-ASCII character answered 500.
- A Glimmer URL with odd host text loaded.
- A lower-effort retry stepped one effort down only by luck of the data: now pinned on Spark.
- The gate before each later piece of a long audio file had no test.
"""

import dataclasses
import gzip
import json
import math
import wave
from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models.config import (
    MAX_OUTPUT_TOKENS,
    ModelSettings,
    load_catalog,
    load_model_settings,
)
from grove.models.errors import (
    BudgetReached,
    InvalidOutput,
    ModelRefused,
    NotConfigured,
    ProviderError,
)
from grove.models.gateway import Gateway, create_app, upstream_body
from grove.models.gateway_auth import mint
from grove.models.layer import ModelLayer, clean_tools
from grove.store.query import WorldQuery
from grove.writer.model_calls import set_budget_ceiling

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"s" * 32
CATALOG = load_catalog()
SPARK = CATALOG.model("muse-spark-1.3")
GLIMMER = CATALOG.model("muse-glimmer-30b")
MESSAGES = [
    {"role": "system", "content": "You keep the synthetic orchard ledger."},
    {"role": "user", "content": "Which row holds the pears?"},
]
USAGE = {
    "prompt_tokens": 5000,
    "completion_tokens": 80,
    "prompt_tokens_details": {"cached_tokens": 4096},
    "completion_tokens_details": {"reasoning_tokens": 30},
}
_ROWS = (
    "MATCH (c:ModelCall) RETURN c.outcome AS outcome, c.effort AS effort, "
    "c.input_tokens AS input_tokens, c.output_tokens AS output_tokens, c.cost AS cost "
    "ORDER BY c.at, c.id"
)
INPUT_PRICE = 1.25e-6


def sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"


TEXT = sse({"choices": [{"index": 0, "delta": {"content": "Row nine."}, "finish_reason": None}]})
FINISH = sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": USAGE})
DONE = b"data: [DONE]\n\n"


def settings(**overrides) -> ModelSettings:
    fields = {
        "meta_key": "test-meta-key",
        "glimmer_base_url": "https://glimmer.example.test/v1",
        "glimmer_key": "test-glimmer-key",
        "daily_budget": 5.0,
        "gateway_secret": SECRET,
    }
    return ModelSettings(**(fields | overrides))


@pytest.fixture
def test_clock():
    frozen = TestClock()
    frozen.set(NOW)
    with clock.using(frozen):
        yield frozen


def rows(driver, world) -> list[dict]:
    return WorldQuery(driver, world).read(_ROWS)


class Provider:
    """Sync and async fake: each item is a response, or an exception to raise."""

    def __init__(self, *items) -> None:
        self.items = list(items)
        self.requests: list[httpx.Request] = []

    def _next(self, request: httpx.Request):
        self.requests.append(request)
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return self._next(request)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        return self._next(request)


def layer_for(driver, provider) -> ModelLayer:
    return ModelLayer(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider),
        sleep=lambda _: None,
    )


async def _no_wait(_: float) -> None:
    return None


def post(driver, world, provider: Provider, body: dict, *, job="update_pass", token=None):
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
        sleep=_no_wait,
    )
    token = token or mint(SECRET, CATALOG, world_id=world, run_id="r1", job=job)
    with TestClient(create_app(gateway)) as client:
        return client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"}
        )


# Factories: a response object can be read only once, and each test needs its own.
MAY_HAVE_RUN = {
    "read-timeout": lambda: httpx.ReadTimeout("no answer in time"),
    "read-error": lambda: httpx.ReadError("the connection dropped"),
    "protocol-error": lambda: httpx.RemoteProtocolError("the server hung up"),
    "500": lambda: httpx.Response(500, json={"error": {"message": "boom"}}),
    "502": lambda: httpx.Response(502, json={"error": {"message": "boom"}}),
    "504": lambda: httpx.Response(504, json={"error": {"message": "timed out"}}),
}


@pytest.mark.parametrize("failure", MAY_HAVE_RUN)
def test_layer_sends_a_call_that_may_have_run_once_and_charges_its_input(
    driver, world, test_clock, failure
):
    provider = Provider(MAY_HAVE_RUN[failure]())

    with layer_for(driver, provider) as layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name one synthetic fruit.")

    assert len(provider.requests) == 1
    [row] = rows(driver, world)
    assert row["outcome"] == "provider_error"
    sent = json.loads(provider.requests[0].content)
    assert row["input_tokens"] == math.ceil(len(json.dumps(sent)) / 4)
    assert row["output_tokens"] == 0
    assert row["cost"] == pytest.approx(row["input_tokens"] * INPUT_PRICE, abs=1e-12)
    assert row["cost"] > 0


@pytest.mark.parametrize("failure", MAY_HAVE_RUN)
def test_gateway_sends_a_call_that_may_have_run_once_and_charges_its_input(
    driver, world, test_clock, failure
):
    provider = Provider(MAY_HAVE_RUN[failure]())

    response = post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})

    assert response.status_code >= 500
    assert len(provider.requests) == 1
    [row] = rows(driver, world)
    assert row["outcome"] == "provider_error"
    estimate = math.ceil((len(json.dumps(MESSAGES)) + len(json.dumps([]))) / 4)
    assert row["input_tokens"] == estimate
    assert row["cost"] == pytest.approx(estimate * INPUT_PRICE, abs=1e-12)


def test_a_connection_that_never_opened_is_tried_again_and_costs_nothing(driver, world, test_clock):
    provider = Provider(httpx.ConnectError("no route"))

    with layer_for(driver, provider) as layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name one synthetic fruit.")

    assert len(provider.requests) == 4
    [row] = rows(driver, world)
    assert (row["input_tokens"], row["cost"]) == (0, 0.0)


def test_a_refused_call_costs_nothing(driver, world, test_clock):
    provider = Provider(httpx.Response(400, json={"error": {"message": "bad field"}}))

    with layer_for(driver, provider) as layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name one synthetic fruit.")

    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"], row["cost"]) == ("provider_error", 0, 0.0)


def test_a_failed_background_poll_charges_the_input(driver, world, test_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/responses":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "resp_1", "status": "cancelled"})
        return httpx.Response(404, json={"error": {"message": "gone"}})

    with (
        ModelLayer(
            driver,
            settings=settings(),
            catalog=CATALOG,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        ) as layer,
        pytest.raises(ProviderError),
    ):
        layer.respond(world, job="extraction", input="Name one fruit.", background=True)

    [row] = rows(driver, world)
    assert row["outcome"] == "provider_error"
    assert row["input_tokens"] > 0 and row["cost"] > 0


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search"},
        {"type": "mcp", "server_url": "https://exfil.example.net/mcp", "server_label": "x"},
        {"type": "function"},
        {"type": "function", "function": {"description": "no name"}},
        "brain",
    ],
    ids=["web-search", "mcp", "no-function", "no-name", "text"],
)
def test_only_function_tools_reach_a_provider(driver, world, test_clock, tool):
    provider = Provider(httpx.Response(200, json={"choices": [], "usage": USAGE}))

    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES, "tools": [tool]}
    )

    assert response.status_code == 400
    assert "tool" in response.json()["error"]["message"]
    assert provider.requests == []
    assert rows(driver, world) == []


def test_a_function_tool_keeps_only_its_known_keys():
    tool = {
        "type": "function",
        "server_url": "https://exfil.example.net/mcp",
        "function": {
            "name": "bash",
            "description": "Run a command.",
            "parameters": {"$schema": "https://json-schema.org/draft/2020-12/schema"},
            "server_url": "https://exfil.example.net/mcp",
        },
    }
    assert clean_tools([tool]) == [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a command.",
                "parameters": {"$schema": "https://json-schema.org/draft/2020-12/schema"},
            },
        }
    ]
    body = {"model": "muse-spark-1.3", "messages": MESSAGES, "tools": "bash"}
    with pytest.raises(ModelRefused, match="tools must be a list"):
        upstream_body(body, spec=SPARK, effort="low", job="update_pass")


def test_the_layer_filters_the_tools_too(driver, world, test_clock):
    provider = Provider(httpx.Response(200, json={"choices": [], "usage": USAGE}))
    with layer_for(driver, provider) as layer, pytest.raises(ModelRefused):
        layer.chat(world, job="verification", messages=MESSAGES, tools=[{"type": "web_search"}])
    assert provider.requests == []


def test_a_response_format_of_another_type_is_refused():
    body = {"model": "muse-spark-1.3", "messages": MESSAGES, "response_format": {"type": "url"}}
    with pytest.raises(ModelRefused, match="response_format"):
        upstream_body(body, spec=SPARK, effort="low", job="update_pass")


def test_the_output_limit_has_an_upper_bound():
    body = {"model": "muse-spark-1.3", "messages": MESSAGES, "max_tokens": 10**9}
    out = upstream_body(body, spec=SPARK, effort="low", job="update_pass")
    assert out["max_tokens"] == MAX_OUTPUT_TOKENS == 65536


def test_the_layer_refuses_a_chat_limit_off_the_range(driver, world, test_clock):
    provider = Provider(httpx.Response(200, json={}))
    with layer_for(driver, provider) as layer, pytest.raises(ModelRefused, match="max_tokens"):
        layer.chat(world, job="verification", messages=MESSAGES, max_tokens=65537)
    assert provider.requests == []


class Listed(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for piece in self.chunks:
            yield piece


def test_a_compressed_stream_reaches_the_client_decoded_and_is_metered(driver, world, test_clock):
    packed = gzip.compress(TEXT + FINISH + DONE)
    provider = Provider(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
            stream=Listed(packed),
        )
    )

    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    )

    assert response.status_code == 200
    assert response.content == TEXT + FINISH + DONE
    assert "content-encoding" not in response.headers
    assert provider.requests[0].headers["accept-encoding"] == "identity"
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"], row["output_tokens"]) == ("ok", 5000, 80)


class BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.ReadError("the error body dropped")
        yield b""


def test_a_stream_error_whose_body_cannot_be_read_is_metered(driver, world, test_clock):
    provider = Provider(httpx.Response(500, stream=BrokenBody()))

    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "provider_error"
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


def test_a_run_token_with_a_non_ascii_character_is_401(driver, world, test_clock):
    provider = Provider(httpx.Response(200, json={}))
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
    )
    with TestClient(create_app(gateway)) as client:
        for token in ("g1.\xe9.abc", "g1.abc.\xe9"):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "muse-spark-1.3", "messages": MESSAGES},
                headers={"Authorization": f"Bearer {token}".encode("latin-1")},
            )
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthenticated"
    assert provider.requests == []


@pytest.mark.parametrize(
    "url", ["https://evil.example\\x.modal.run/v1", "https://ev il.modal.run/v1"]
)
def test_a_glimmer_url_with_odd_host_text_does_not_load(url):
    with pytest.raises(NotConfigured, match="GROVE_GLIMMER_BASE_URL"):
        load_model_settings({"GROVE_GLIMMER_BASE_URL": url})


def test_a_lower_effort_retry_steps_down_one_effort(driver, world, test_clock):
    empty = {
        "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10},
    }
    good = {
        "choices": [{"message": {"role": "assistant", "content": "Yes."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    answers = [httpx.Response(200, json=empty), httpx.Response(200, json=good)]

    def provider(request: httpx.Request) -> httpx.Response:
        # The clock moves between the calls: two rows at one instant have no fixed order.
        test_clock.advance(seconds=1)
        return answers.pop(0)

    with layer_for(driver, provider) as layer:
        result = layer.chat(world, job="consolidation", messages=MESSAGES)

    assert result.effort == "medium"
    assert [row["effort"] for row in rows(driver, world)] == ["high", "medium"]


def test_an_empty_answer_with_no_lower_effort_raises(driver, world, test_clock):
    empty = {"choices": [{"message": {"role": "assistant", "content": ""}}], "usage": {}}
    provider = Provider(httpx.Response(200, json=empty))
    with layer_for(driver, provider) as layer, pytest.raises(InvalidOutput):
        layer.chat(world, job="extraction", messages=MESSAGES)
    assert [row["effort"] for row in rows(driver, world)] == ["minimal"]


def _silent_wav(seconds: int) -> bytes:
    out = BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(1000)
        writer.writeframes(b"\x80" * (seconds * 1000))
    return out.getvalue()


def test_each_later_piece_of_a_long_audio_file_passes_the_gate(driver, world, test_clock):
    # With a voice job that waits at the ceiling, the piece that reaches it stops the file.
    jobs = dict(CATALOG.jobs)
    jobs["transcription"] = dataclasses.replace(jobs["transcription"], at_ceiling="defer")
    catalog = dataclasses.replace(CATALOG, jobs=jobs)
    set_budget_ceiling(driver, world, NOW.date(), 1e-9)
    set_budget_ceiling(driver, world, NOW.date(), 0.01)
    provider = Provider(
        httpx.Response(200, json={"transcript": "pears", "audioDurationMs": 590_000})
    )

    with (
        ModelLayer(
            driver,
            settings=settings(),
            catalog=catalog,
            transport=httpx.MockTransport(provider),
            sleep=lambda _: None,
        ) as layer,
        pytest.raises(BudgetReached),
    ):
        layer.transcribe(world, audio_wav=_silent_wav(1250))

    assert len(provider.requests) == 1
    assert len(rows(driver, world)) == 1
