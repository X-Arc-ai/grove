"""Regression tests for the third fresh R4 pass, each red before its fix.

- A provider answer of an odd shape, or a stream usage chunk with an odd field, lost its row. On a
  stream, the client's response never ended either.
- An answer with no token counts was free against the ceiling on a non-stream call.
- A stream value that is not a boolean could leave a stream unmetered at the provider's whim.
- A bad run id was found only after the call had run and billed.
- A 5xx outside 500, 502 and 504 counted as free, and a voice call that may have run cost nothing.
- A signed caller could get an untyped 500 from a body that cannot be sent.
- A part of a two-part key, or a key with a newline, could reach a message or a traceback.
- Remote image URLs passed the layer. Remote schema references, other tool call types and SVG data
  URLs passed the gateway. Odd base URLs raised or loaded.
- The Modal image was taken by a tag that can move.
"""

import asyncio
import dataclasses
import json
import math
import wave
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models.config import ModelSettings, load_catalog, load_model_settings
from grove.models.errors import InvalidOutput, ModelRefused, NotConfigured, ProviderError
from grove.models.gateway import DrainIncomplete, Gateway, create_app, upstream_body
from grove.models.gateway_auth import mint, verify
from grove.models.layer import ModelLayer
from grove.models.shape import (
    Usage,
    chat_answer,
    chat_usage,
    clean_messages,
    clean_tools,
    redact,
    responses_text,
    responses_usage,
)
from grove.store.query import WorldQuery

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"s" * 32
CATALOG = load_catalog()
SPARK = CATALOG.model("muse-spark-1.3")
MESSAGES = [
    {"role": "system", "content": "You keep the synthetic orchard ledger."},
    {"role": "user", "content": "Which row holds the pears?"},
]
PRICE_IN, PRICE_OUT = 1.25e-6, 4.25e-6
_ROWS = (
    "MATCH (c:ModelCall) RETURN c.outcome AS outcome, c.input_tokens AS input_tokens, "
    "c.output_tokens AS output_tokens, c.cost AS cost, c.audio_seconds AS audio_seconds "
    "ORDER BY c.at, c.id"
)


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


def layer_for(driver, provider: Provider) -> ModelLayer:
    return ModelLayer(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider),
        sleep=lambda _: None,
    )


async def _no_wait(_: float) -> None:
    return None


def post(driver, world, provider: Provider, body, *, job="cleanup_pass", raw=None):
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
        sleep=_no_wait,
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="r1", job=job)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(gateway)) as client:
        if raw is not None:
            headers["content-type"] = "application/json"
            return client.post("/v1/chat/completions", content=raw, headers=headers)
        return client.post("/v1/chat/completions", json=body, headers=headers)


def stated(**usage) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "Row nine."}}],
        "usage": usage,
    }


class Listed(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for piece in self.chunks:
            yield piece


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 10, "prompt_tokens_details": 5}, 10),
        ({"prompt_tokens": 12, "completion_tokens_details": "many"}, 12),
    ],
    ids=["details-int", "details-text"],
)
def test_a_stream_usage_chunk_with_an_odd_field_is_metered_and_ends(
    driver, world, test_clock, usage, expected
):
    def sse(event: dict) -> bytes:
        return b"data: " + json.dumps(event).encode() + b"\n\n"

    text = sse({"choices": [{"delta": {"content": "Row nine."}}]})
    finish = sse({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage})
    done = b"data: [DONE]\n\n"
    provider = Provider(lambda: httpx.Response(200, stream=Listed(text, finish, done)))

    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    )

    assert response.status_code == 200
    assert response.content == text + finish + done
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"]) == ("ok", expected)


ODD_USAGE = [
    ("usage-text", "lots"),
    ("usage-list", [1, 2]),
    ("usage-null", None),
    ("tokens-text", {"prompt_tokens": "ten", "completion_tokens": "two"}),
    ("tokens-float", {"prompt_tokens": 10.5, "completion_tokens": 2.5}),
]


@pytest.mark.parametrize("usage", [item for _, item in ODD_USAGE], ids=[n for n, _ in ODD_USAGE])
def test_gateway_estimates_an_answer_with_no_token_counts(driver, world, test_clock, usage):
    answer = {"choices": [{"message": {"role": "assistant", "content": "Row nine."}}]}
    answer["usage"] = usage
    provider = Provider(lambda: httpx.Response(200, json=answer))

    response = post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})

    assert response.status_code == 200 and response.json() == answer
    [row] = rows(driver, world)
    estimate_in = math.ceil((len(json.dumps(MESSAGES)) + len(json.dumps([]))) / 4)
    estimate_out = math.ceil(len("Row nine.") / 4)
    assert (row["outcome"], row["input_tokens"], row["output_tokens"]) == (
        "ok",
        estimate_in,
        estimate_out,
    )
    assert row["cost"] == pytest.approx(
        estimate_in * PRICE_IN + estimate_out * PRICE_OUT, abs=1e-12
    )


ODD_CHAT = [
    ("choices-int", {"choices": 5}),
    ("choices-text", {"choices": "none"}),
    ("choice-int", {"choices": [5]}),
    ("message-text", {"choices": [{"message": "Row nine."}]}),
    ("content-int", {"choices": [{"message": {"content": 5}}]}),
    ("no-choices", {}),
]


@pytest.mark.parametrize("doc", [item for _, item in ODD_CHAT], ids=[n for n, _ in ODD_CHAT])
def test_layer_chat_meters_an_odd_answer_and_raises_typed(driver, world, test_clock, doc):
    answer = {**doc, "usage": {"prompt_tokens": 900, "completion_tokens": 90}}
    provider = Provider(lambda: httpx.Response(200, json=answer))

    with layer_for(driver, provider) as layer, pytest.raises(InvalidOutput):
        layer.chat(world, job="extraction", messages=MESSAGES)

    assert len(provider.requests) == 1
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"], row["output_tokens"]) == (
        "invalid_output",
        900,
        90,
    )


ODD_RESPONSES = [
    ("output-int", {"output": 5}),
    ("output-item-int", {"output": [5]}),
    ("content-text", {"output": [{"type": "message", "content": "pear"}]}),
    (
        "text-int",
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": 5}]}]},
    ),
]


@pytest.mark.parametrize(
    "doc", [item for _, item in ODD_RESPONSES], ids=[n for n, _ in ODD_RESPONSES]
)
def test_layer_respond_meters_an_odd_answer_and_raises_typed(driver, world, test_clock, doc):
    answer = {
        **doc,
        "status": "completed",
        "usage": {"input_tokens": 9000, "output_tokens": 900, "input_tokens_details": 5},
    }
    provider = Provider(lambda: httpx.Response(200, json=answer))

    with layer_for(driver, provider) as layer, pytest.raises(InvalidOutput):
        layer.respond(world, job="extraction", input="Name one synthetic fruit.")

    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"], row["output_tokens"]) == (
        "invalid_output",
        9000,
        900,
    )


def test_the_readers_never_raise():
    for value in (None, 5, "x", [], [5], {"a": 1}):
        assert chat_usage(value) == Usage()
        assert responses_usage({"usage": value}) == Usage()
        assert responses_text({"output": value}) == ""
        assert chat_answer({"choices": value})[:2] == ("", ())
    parts = {"choices": [{"message": {"content": [{"type": "text", "text": "Row"}, 5, {}]}}]}
    assert chat_answer(parts)[0] == "Row"


def test_layer_chat_estimates_an_answer_with_no_token_counts(driver, world, test_clock):
    answer = {"choices": [{"message": {"role": "assistant", "content": "Row nine."}}]}
    provider = Provider(lambda: httpx.Response(200, json=answer))

    with layer_for(driver, provider) as layer:
        layer.chat(world, job="verification", messages=MESSAGES)

    sent = json.loads(provider.requests[0].content)
    [row] = rows(driver, world)
    assert row["input_tokens"] == math.ceil(len(json.dumps(sent)) / 4)
    assert row["output_tokens"] == math.ceil(len("Row nine.") / 4)
    assert row["cost"] > 0


def test_layer_respond_estimates_an_answer_with_no_token_counts(driver, world, test_clock):
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Pluot"}]}],
    }
    provider = Provider(lambda: httpx.Response(200, json=answer))

    with layer_for(driver, provider) as layer:
        layer.respond(world, job="extraction", input="Name one synthetic fruit.")

    [row] = rows(driver, world)
    assert row["input_tokens"] > 0 and row["output_tokens"] == math.ceil(len("Pluot") / 4)


@pytest.mark.parametrize(
    "body",
    [
        {"stream": "false"},
        {"stream": 1},
        {"stream": True, "stream_options": "usage"},
        {"stream": True, "stream_options": ["usage"]},
    ],
    ids=["stream-text", "stream-int", "options-text", "options-list"],
)
def test_a_stream_field_of_another_type_is_refused(driver, world, test_clock, body):
    provider = Provider(lambda: httpx.Response(200, json=stated(prompt_tokens=1)))

    response = post(
        driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES} | body
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert provider.requests == []


@pytest.mark.parametrize("run_id", ["x" * 81, "r\xe9n", ""], ids=["long", "non-ascii", "empty"])
def test_a_bad_run_id_is_refused_before_anything_leaves(driver, world, test_clock, run_id):
    provider = Provider(lambda: httpx.Response(200, json=stated(prompt_tokens=1)))

    with layer_for(driver, provider) as layer:
        with pytest.raises(ModelRefused, match="run_id"):
            layer.respond(world, job="extraction", input="Name a fruit.", run_id=run_id)
        with pytest.raises(ModelRefused, match="run_id"):
            layer.chat(world, job="verification", messages=MESSAGES, run_id=run_id)
    assert provider.requests == []
    assert rows(driver, world) == []


@pytest.mark.parametrize("status", [501, 520, 522, 524, 529, 599])
def test_any_5xx_but_503_charges_the_input_and_is_not_sent_again(driver, world, test_clock, status):
    provider = Provider(lambda: httpx.Response(status, json={"error": {"message": "boom"}}))

    response = post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})

    assert response.status_code == status
    assert len(provider.requests) == 1
    [row] = rows(driver, world)
    assert row["outcome"] == "provider_error" and row["input_tokens"] > 0 and row["cost"] > 0


def test_a_503_costs_nothing(driver, world, test_clock):
    provider = Provider(lambda: httpx.Response(503, json={"error": {"message": "busy"}}))

    response = post(driver, world, provider, {"model": "muse-spark-1.3", "messages": MESSAGES})

    assert response.status_code == 503
    assert len(provider.requests) == 4
    [row] = rows(driver, world)
    assert (row["input_tokens"], row["cost"]) == (0, 0.0)


def _wav(seconds: int) -> bytes:
    out = BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(1000)
        writer.writeframes(b"\x80" * (seconds * 1000))
    return out.getvalue()


@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(500, json={"error": {"message": "boom"}}),
        lambda: httpx.Response(200, content=b"<html>"),
    ],
    ids=["500", "not-json"],
)
def test_a_voice_call_that_may_have_run_charges_its_audio(driver, world, test_clock, answer):
    with (
        layer_for(driver, Provider(answer)) as layer,
        pytest.raises(ProviderError),
    ):
        layer.transcribe(world, audio_wav=_wav(100))

    [row] = rows(driver, world)
    assert row["audio_seconds"] == 100.0
    assert row["cost"] == pytest.approx(100 / 3600 * 0.18, abs=1e-12)


def test_a_refused_voice_call_costs_nothing(driver, world, test_clock):
    provider = Provider(lambda: httpx.Response(400, json={"error": {"message": "malformed"}}))
    with layer_for(driver, provider) as layer, pytest.raises(ProviderError):
        layer.transcribe(world, audio_wav=_wav(100))
    [row] = rows(driver, world)
    assert (row["audio_seconds"], row["cost"]) == (None, 0.0)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "x"}], '
        b'"temperature": NaN}',
        b'{"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "x"}], '
        b'"temperature": 1e999}',
        b'{"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "\\ud800"}]}',
        b'{"model": "muse-spark-1.3", "messages": ' + b"[" * 200_000,
        b'{"model": "muse-spark-1.3", "tools": ' + b"[" * 200_000 + b"]" * 200_000 + b"}",
    ],
    ids=["nan", "inf", "surrogate", "deep-open", "deep-closed"],
)
def test_a_body_that_cannot_be_sent_gets_a_typed_refusal(driver, world, test_clock, raw):
    provider = Provider(lambda: httpx.Response(200, json=stated(prompt_tokens=1)))

    response = post(driver, world, provider, None, raw=raw)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert provider.requests == []
    assert rows(driver, world) == []


def test_an_egress_refusal_through_the_gateway_is_typed_and_writes_nothing(
    driver, world, test_clock
):
    provider = Provider(lambda: httpx.Response(200, json=stated(prompt_tokens=1)))
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
    )
    gateway.settings = dataclasses.replace(
        gateway.settings, meta_base_url="https://elsewhere.example.net/v1"
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="r2", job="cleanup_pass")
    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "muse-spark-1.3", "messages": MESSAGES},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "egress_refused"
    assert provider.requests == []
    assert rows(driver, world) == []


def test_a_message_loses_each_part_of_a_two_part_key():
    key = "wk-0123456789abcdef.ws-fedcba9876543210"
    text = redact(f"bad token {key}; secret ws-fedcba9876543210; id wk-0123456789abcdef", (key,))
    assert "fedcba9876543210" not in text and "0123456789abcdef" not in text
    assert redact("a short ab.cd stays", ("ab.cd",)) == "a short ab.cd stays"


@pytest.mark.parametrize("name", ["MODEL_API_KEY", "GROVE_GLIMMER_KEY"])
@pytest.mark.parametrize("value", ["key\n", "ke y", "key\x07", "\tkey"])
def test_a_key_with_a_space_or_control_character_does_not_load(name, value):
    with pytest.raises(NotConfigured, match=name):
        load_model_settings({name: value})


def test_a_no_answer_error_carries_no_cause_that_could_quote_a_header(driver, world, test_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("Illegal header value b'Bearer planted-key'")

    with (
        ModelLayer(
            driver,
            settings=settings(),
            catalog=CATALOG,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        ) as layer,
        pytest.raises(ProviderError) as refused,
    ):
        layer.respond(world, job="extraction", input="Name a fruit.")
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://exfil.example.net/p"}},
        {"type": "image_url", "image_url": {"url": "data:image/svg+xml;base64,PHN2Zz4="}},
    ],
    ids=["remote", "svg"],
)
def test_the_layer_chat_refuses_what_a_provider_would_fetch(driver, world, test_clock, part):
    provider = Provider(lambda: httpx.Response(200, json=stated(prompt_tokens=1)))
    messages = [{"role": "user", "content": [{"type": "text", "text": "Look."}, part]}]
    with layer_for(driver, provider) as layer, pytest.raises(ModelRefused, match="data URLs"):
        layer.chat(world, job="triage", messages=messages)
    assert provider.requests == []


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "image_url": "https://exfil.example.net/p"},
        {"type": "input_file", "file_url": "https://exfil.example.net/p"},
    ],
    ids=["remote-image", "remote-file"],
)
def test_the_layer_respond_refuses_what_a_provider_would_fetch(driver, world, test_clock, part):
    provider = Provider(lambda: httpx.Response(200, json=stated(prompt_tokens=1)))
    with layer_for(driver, provider) as layer, pytest.raises(ModelRefused, match="data URLs"):
        layer.respond(world, job="extraction", input=[{"role": "user", "content": [part]}])
    assert provider.requests == []


def test_the_layer_respond_takes_a_data_image():
    # The shape the screenshot job will send.
    from grove.models.shape import clean_responses_input

    good = {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}
    clean_responses_input(
        [{"role": "user", "content": [{"type": "input_text", "text": "x"}, good]}]
    )
    for bad in (5, [], [5], [{"role": "user"}], [{"role": "user", "content": 5}]):
        with pytest.raises(ModelRefused):
            clean_responses_input(bad)


@pytest.mark.parametrize(
    "ref", ["https://exfil.example.net/schema.json", "file:///etc/passwd", 5], ids=str
)
def test_a_schema_that_refers_outside_itself_is_refused(ref):
    tool = {
        "type": "function",
        "function": {"name": "bash", "parameters": {"properties": {"x": {"$ref": ref}}}},
    }
    with pytest.raises(ModelRefused, match="inside itself"):
        clean_tools([tool])
    body = {
        "model": "muse-spark-1.3",
        "messages": MESSAGES,
        "response_format": {"type": "json_schema", "json_schema": {"schema": {"$ref": ref}}},
    }
    with pytest.raises(ModelRefused, match="inside itself"):
        upstream_body(body, spec=SPARK, effort="low", job="cleanup_pass")


def test_a_schema_that_refers_inside_itself_passes():
    parameters = {"$defs": {"a": {"type": "string"}}, "properties": {"x": {"$ref": "#/$defs/a"}}}
    tool = {"type": "function", "function": {"name": "bash", "parameters": parameters}}
    assert clean_tools([tool])[0]["function"]["parameters"] == parameters


def test_only_function_tool_calls_stay_in_the_history():
    good = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}", "extra": 1},
                "extra": 2,
            }
        ],
    }
    assert clean_messages([good])[0]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    ]
    for calls in ("bash", [{"type": "web_search"}], [{"type": "function"}]):
        with pytest.raises(ModelRefused, match="tool"):
            clean_messages([{"role": "assistant", "tool_calls": calls}])


@pytest.mark.parametrize(
    "url",
    ["https://:@api.meta.ai/v1", " https://api.meta.ai/v1", "https://[api.meta.ai]/v1"],
    ids=["empty-user", "leading-space", "bracket-host"],
)
def test_an_odd_base_url_does_not_load(url):
    with pytest.raises(NotConfigured, match="GROVE_META_BASE_URL"):
        load_model_settings({"GROVE_META_BASE_URL": url})


def test_the_glimmer_image_is_pinned_by_digest():
    app_file = Path(__file__).resolve().parents[3] / "deploy" / "modal" / "glimmer.py"
    assert '"@sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34"' in (
        app_file.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("options", ["usage", ["usage"], 5], ids=["text", "list", "int"])
def test_stream_options_of_another_type_are_refused_by_name(options):
    body = {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    with pytest.raises(ModelRefused, match="stream_options"):
        upstream_body(body | {"stream_options": options}, spec=SPARK, effort="low", job="smoke")


def test_the_client_stream_ends_even_when_the_record_fails(driver, world, test_clock):
    # The store can fail at the end of a stream. The client must not wait for ever.
    gateway = Gateway(driver, settings=settings(), catalog=CATALOG)

    def failing_record(*_: object) -> None:
        raise RuntimeError("the store is down")

    gateway._record = failing_record
    claims = verify(
        SECRET, CATALOG, mint(SECRET, CATALOG, world_id=world, run_id="r3", job="cleanup_pass")
    )
    chunk = b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}\n\n'
    response = httpx.Response(200, stream=Listed(chunk, b"data: [DONE]\n\n"))

    async def drive() -> list[bytes]:
        relay = gateway._relay(response, claims, SPARK, "low", clock.monotonic())

        async def collect() -> list[bytes]:
            return [piece async for piece in relay]

        got = await asyncio.wait_for(collect(), timeout=5.0)
        # The client is free. The lost record still fails the close, so it is never silent.
        with pytest.raises(DrainIncomplete, match="accounting_failed"):
            await gateway.aclose()
        return got

    assert asyncio.run(drive()) == [chunk, b"data: [DONE]\n\n"]
    assert response.is_closed
