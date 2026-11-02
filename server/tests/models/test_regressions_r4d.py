"""Regression tests for the fourth fresh R4 pass, each red before its fix.

- A run token for a job that continues in code passed the ceiling at the gateway.
- A schema with $dynamicRef or $recursiveRef could make the validator open its own socket.
- An answer nested too deep, or a usage count past any real count, escaped the meter.
- A structured answer nested too deep raised untyped, with no row.
- An infinite or absurd audio duration lost the row or closed the day's budget.
- One passing failure of a background poll cancelled a paid run.
- One stream chunk of an odd shape cut the client's stream and lost the usage.
- One prompt cache key served all worlds, so `cached_tokens` could tell one world about another.
- The gateway had no limit on a body's size.
- A stream's store write froze the event loop for every other stream.
- A read that broke with an error outside httpx left the pump with no outcome.
"""

import asyncio
import json
import threading
import wave
from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models import gateway as gateway_module
from grove.models.config import ModelSettings, load_catalog
from grove.models.errors import BodyTooLarge, InvalidOutput, ModelRefused, ProviderError
from grove.models.gateway import Gateway, create_app
from grove.models.gateway_auth import mint, verify
from grove.models.layer import ModelLayer, check_schema
from grove.models.shape import chat_usage, clean_messages, clean_tools, responses_usage
from grove.store.query import WorldQuery
from grove.writer.model_calls import set_budget_ceiling

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"s" * 32
CATALOG = load_catalog()
SPARK = CATALOG.model("muse-spark-1.3")
MESSAGES = [
    {"role": "system", "content": "You keep the synthetic orchard ledger."},
    {"role": "user", "content": "Which row holds the pears?"},
]
SCHEMA = {"type": "object", "properties": {"row": {"type": "integer"}}, "required": ["row"]}
USAGE = {"prompt_tokens": 5000, "completion_tokens": 80}
_ROWS = (
    "MATCH (c:ModelCall) RETURN c.outcome AS outcome, c.input_tokens AS input_tokens, "
    "c.output_tokens AS output_tokens, c.cost AS cost, c.audio_seconds AS audio_seconds "
    "ORDER BY c.at, c.id"
)
DEEP = b"[" * 200_000 + b"]" * 200_000


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
    def __init__(self, make) -> None:
        self.make = make
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.make()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.make()


def layer_for(driver, handler) -> ModelLayer:
    return ModelLayer(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


async def _no_wait(_: float) -> None:
    return None


def post(driver, world, provider: Provider, body: dict, *, job="cleanup_pass"):
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
        sleep=_no_wait,
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="r1", job=job)
    with TestClient(create_app(gateway)) as client:
        return client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"}
        )


class Listed(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for piece in self.chunks:
            yield piece


def sse(event: object) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"


TEXT = sse({"choices": [{"delta": {"content": "Row nine."}}]})
FINISH = sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": USAGE})
DONE = b"data: [DONE]\n\n"


@pytest.mark.parametrize("key", ["$dynamicRef", "$recursiveRef", "$ref"])
def test_a_reference_of_any_kind_is_refused_before_the_validator_sees_it(key):
    schema = {
        "type": "object",
        "properties": {"row": {key: "https://exfil.example.net/schema.json"}},
    }
    with pytest.raises(ModelRefused, match="references"):
        check_schema(schema)
    tool = {"type": "function", "function": {"name": "bash", "parameters": schema}}
    with pytest.raises(ModelRefused, match="inside itself"):
        clean_tools([tool])


@pytest.mark.parametrize("key", ["$dynamicRef", "$recursiveRef", "$ref"])
def test_a_tool_schema_may_refer_inside_itself(key):
    parameters = {"properties": {"row": {key: "#node"}}}
    tool = {"type": "function", "function": {"name": "bash", "parameters": parameters}}
    assert clean_tools([tool])[0]["function"]["parameters"] == parameters


def test_a_body_nested_too_deep_is_metered_by_the_layer(driver, world, test_clock):
    handler = Provider(lambda: httpx.Response(200, content=DEEP))
    with layer_for(driver, handler) as layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name a fruit.")
    [row] = rows(driver, world)
    assert row["outcome"] == "provider_error" and row["input_tokens"] > 0


def test_a_body_nested_too_deep_is_metered_by_the_gateway(driver, world, test_clock):
    provider = Provider(lambda: httpx.Response(200, content=DEEP))
    response = post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})
    assert response.status_code == 503
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


def test_a_structured_answer_nested_too_deep_is_invalid_output_with_a_row(
    driver, world, test_clock
):
    text = "[" * 200_000 + "]" * 200_000
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    with (
        layer_for(driver, Provider(lambda: httpx.Response(200, json=answer))) as layer,
        pytest.raises(InvalidOutput),
    ):
        layer.respond(world, job="extraction", input="Name a fruit.", schema=SCHEMA)
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"]) == ("invalid_output", 10)


def test_a_usage_count_past_any_real_count_reads_as_absent(driver, world, test_clock):
    assert chat_usage({"prompt_tokens": 2**63, "completion_tokens": 10**13}).input_tokens == 0
    assert responses_usage({"usage": {"input_tokens": 2**70}}).input_tokens == 0
    answer = {
        "choices": [{"message": {"role": "assistant", "content": "Row nine."}}],
        "usage": {"prompt_tokens": 2**70, "completion_tokens": 2**70},
    }
    provider = Provider(lambda: httpx.Response(200, json=answer))
    response = post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})
    assert response.status_code == 200
    [row] = rows(driver, world)
    assert row["outcome"] == "ok" and 0 < row["input_tokens"] < 1000


def _wav(seconds: int) -> bytes:
    out = BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(1000)
        writer.writeframes(b"\x80" * (seconds * 1000))
    return out.getvalue()


@pytest.mark.parametrize(
    "duration",
    [b"Infinity", b"-Infinity", b"NaN", b"1e300", b"0", b"-5", b'"long"', b"true"],
    ids=["inf", "minus-inf", "nan", "huge", "zero", "negative", "text", "bool"],
)
def test_an_absurd_audio_duration_counts_the_audio_that_was_sent(
    driver, world, test_clock, duration
):
    body = b'{"transcript": "pears", "audioDurationMs": ' + duration + b"}"
    with layer_for(driver, Provider(lambda: httpx.Response(200, content=body))) as layer:
        transcript = layer.transcribe(world, audio_wav=_wav(100))
    assert transcript.audio_seconds == 100.0
    [row] = rows(driver, world)
    assert row["audio_seconds"] == 100.0
    assert row["cost"] == pytest.approx(100 / 3600 * 0.18, abs=1e-12)


def test_a_plausible_audio_duration_is_the_providers(driver, world, test_clock):
    body = b'{"transcript": "pears", "audioDurationMs": 101500}'
    with layer_for(driver, Provider(lambda: httpx.Response(200, content=body))) as layer:
        assert layer.transcribe(world, audio_wav=_wav(100)).audio_seconds == 101.5


DONE_DOC = {
    "id": "resp_1",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "Pluot"}]}],
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


def _polling(driver, polls):
    seen = []
    polls = list(polls)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/responses":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "resp_1", "status": "cancelled"})
        answer = polls.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return layer_for(driver, handler), seen


def test_a_passing_poll_failure_does_not_end_a_paid_run(driver, world, test_clock):
    polls = [
        httpx.ReadError("the connection dropped"),
        httpx.Response(500, json={"error": {"message": "boom"}}),
        httpx.Response(200, content=b"<html>busy</html>"),
        httpx.Response(200, json=DONE_DOC),
    ]
    layer, seen = _polling(driver, polls)
    with layer:
        result = layer.respond(world, job="extraction", input="Name a fruit.", background=True)
    assert result.text == "Pluot"
    assert not any(path.endswith("/cancel") for _, path in seen)
    assert [row["outcome"] for row in rows(driver, world)] == ["ok"]


def test_a_poll_that_keeps_failing_ends_the_run_and_cancels_it(driver, world, test_clock):
    polls = [httpx.Response(500, json={"error": {"message": "boom"}}) for _ in range(4)]
    layer, seen = _polling(driver, polls)
    with layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name a fruit.", background=True)
    assert ("POST", "/v1/responses/resp_1/cancel") in seen
    assert [method for method, _ in seen].count("GET") == 4


@pytest.mark.parametrize(
    "event",
    [
        {"choices": 5},
        {"choices": "none"},
        {"choices": [5]},
        {"choices": [{"delta": 5}]},
        {"choices": [{"delta": {"tool_calls": 5}}]},
        {"choices": [{"delta": {"tool_calls": [5]}}]},
        {"usage": 5},
        [1, 2],
    ],
    ids=["int", "text", "choice-int", "delta-int", "calls-int", "call-int", "usage-int", "list"],
)
def test_one_odd_stream_chunk_neither_cuts_the_stream_nor_loses_the_usage(
    driver, world, test_clock, event
):
    odd = sse(event)
    provider = Provider(lambda: httpx.Response(200, stream=Listed(odd, TEXT, FINISH, DONE)))

    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    )

    assert response.status_code == 200
    assert response.content == odd + TEXT + FINISH + DONE
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"], row["output_tokens"]) == ("ok", 5000, 80)


def test_two_worlds_never_share_a_prompt_cache_key(driver, make_world, test_clock):
    first, second = make_world(), make_world()
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Pluot"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    handler = Provider(lambda: httpx.Response(200, json=answer))
    with layer_for(driver, handler) as layer:
        layer.respond(first, job="extraction", input="Name a fruit.", instructions="Same prefix.")
        layer.respond(second, job="extraction", input="Name a fruit.", instructions="Same prefix.")
        layer.respond(first, job="extraction", input="Name a tree.", instructions="Same prefix.")
    keys = [json.loads(request.content)["prompt_cache_key"] for request in handler.requests]
    assert keys[0] == keys[2] != keys[1]

    chat = {"choices": [{"message": {"role": "assistant", "content": "Yes"}}], "usage": USAGE}
    provider = Provider(lambda: httpx.Response(200, json=chat))
    for world in (first, second):
        post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})
    sent = [json.loads(request.content)["prompt_cache_key"] for request in provider.requests]
    assert sent[0] != sent[1]


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_a_body_over_the_limit_is_refused_and_nothing_leaves(
    driver, world, test_clock, monkeypatch, declared
):
    monkeypatch.setattr(gateway_module, "MAX_BODY_BYTES", 1024)
    provider = Provider(lambda: httpx.Response(200, json={}))
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="r1", job="cleanup_pass")
    big = json.dumps({"model": "muse-spark-1.3", "messages": MESSAGES, "x": "y" * 4096}).encode()

    def pieces():
        yield big[:2048]
        yield big[2048:]

    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=big if declared else pieces(),
            headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert provider.requests == []


def test_a_data_url_needs_its_delimiter():
    good = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    assert clean_messages([{"role": "user", "content": [good]}])[0]["content"] == [good]
    bad = {"type": "image_url", "image_url": {"url": "data:image/pngx;base64,iVBORw0KGgo="}}
    with pytest.raises(ModelRefused, match="data URLs"):
        clean_messages([{"role": "user", "content": [bad]}])


def test_a_stream_record_runs_off_the_event_loop(driver, world, test_clock):
    gateway = Gateway(driver, settings=settings(), catalog=CATALOG)
    threads = []

    def spy(*_: object) -> None:
        threads.append(threading.get_ident())

    gateway._record = spy
    claims = verify(
        SECRET, CATALOG, mint(SECRET, CATALOG, world_id=world, run_id="r2", job="cleanup_pass")
    )
    response = httpx.Response(200, stream=Listed(TEXT, FINISH, DONE))

    async def drive() -> int:
        relay = gateway._relay(response, claims, SPARK, "low", clock.monotonic())
        async for _ in relay:
            pass
        await gateway.aclose()
        return threading.get_ident()

    loop_thread = asyncio.run(drive())
    assert threads and threads[0] != loop_thread


class Breaks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield TEXT
        raise ValueError("a read broke with an error outside httpx")


def test_a_read_that_breaks_outside_httpx_is_metered_and_ends(driver, world, test_clock):
    gateway = Gateway(driver, settings=settings(), catalog=CATALOG)
    claims = verify(
        SECRET, CATALOG, mint(SECRET, CATALOG, world_id=world, run_id="r3", job="cleanup_pass")
    )
    response = httpx.Response(200, stream=Breaks())

    async def drive() -> list:
        relay = gateway._relay(response, claims, SPARK, "low", clock.monotonic())
        got = [piece async for piece in relay]
        pending = list(gateway._pending)
        await gateway.aclose()
        return got + [task.exception() for task in pending if task.done()]

    got = asyncio.run(drive())
    assert got[0] == TEXT and all(item is None or isinstance(item, bytes) for item in got)
    [row] = rows(driver, world)
    assert row["outcome"] == "provider_error"


def test_every_gateway_job_shares_the_one_ceiling_in_the_store(driver, world, test_clock):
    # The gate reads the same row the layer writes: a spend through the layer stops the gateway.
    set_budget_ceiling(driver, world, NOW.date(), 1e-9)
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Pluot"}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    with layer_for(driver, Provider(lambda: httpx.Response(200, json=answer))) as layer:
        layer.respond(world, job="extraction", input="Name a fruit.")
    provider = Provider(lambda: httpx.Response(200, json={}))
    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES}, job="extraction"
    )
    assert response.status_code == 429
    assert provider.requests == []


def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body():
    class Unread:
        headers = {"content-length": str(gateway_module.MAX_BODY_BYTES + 1)}

        async def stream(self):
            raise AssertionError("the body must not be read")
            yield b""

    with pytest.raises(BodyTooLarge):
        asyncio.run(gateway_module._read_json(Unread()))
