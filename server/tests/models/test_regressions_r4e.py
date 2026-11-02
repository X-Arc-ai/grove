"""Regression tests for the last verifier pass, each red before its fix.

- A provider cookie went out on the next call of another world, because one client serves all.
- A crafted WAV header could ask for any number of calls, or understate the metered audio.
- A very long integer duration, an odd background status, a deep stream line and a deep error body
  each lost a row or raised untyped. NaN in a 200 answer lost a billed answer at the gateway.
- Text that cannot be encoded raised a raw error from the layer, and a store failure at the gate
  gave an untyped 500.
- A structured call could return ok with no data, and a cut answer could pass as complete.
- An agent's temperature, top_p and stop reached Glimmer, against D15.
- A row with estimated tokens had no marker, and the backfill had no job that stops at the ceiling.
"""

import io
import json
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models import audio
from grove.models.config import ModelSettings, load_catalog, load_model_settings
from grove.models.egress import EgressPolicy
from grove.models.errors import (
    AudioTooLong,
    BudgetReached,
    EgressRefused,
    InvalidOutput,
    ModelRefused,
    NotConfigured,
    ProviderError,
    Unauthenticated,
)
from grove.models.gateway import Gateway, create_app, upstream_body
from grove.models.gateway_auth import PREFIX, _b64, _sign, mint, verify
from grove.models.layer import ModelLayer
from grove.models.shape import clean_responses_input
from grove.store.query import WorldQuery
from grove.writer.model_calls import set_budget_ceiling

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"s" * 32
CATALOG = load_catalog()
MESSAGES = [
    {"role": "system", "content": "You keep the synthetic orchard ledger."},
    {"role": "user", "content": "Which row holds the pears?"},
]
SCHEMA = {"type": "object", "properties": {"row": {"type": "integer"}}, "required": ["row"]}
_ROWS = (
    "MATCH (c:ModelCall) RETURN c.outcome AS outcome, c.input_tokens AS input_tokens, "
    "c.audio_seconds AS audio_seconds, c.usage_estimated AS usage_estimated ORDER BY c.at, c.id"
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


def gateway_for(driver, provider: Provider) -> Gateway:
    return Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(provider.handle),
        sleep=_no_wait,
    )


def post(driver, world, provider: Provider, body: dict, *, job="cleanup_pass"):
    token = mint(SECRET, CATALOG, world_id=world, run_id="r1", job=job)
    with TestClient(create_app(gateway_for(driver, provider))) as client:
        return client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"}
        )


SPARK_BODY = {"model": "muse-spark-1.3", "messages": MESSAGES}
ANSWER = {
    "choices": [
        {"message": {"role": "assistant", "content": "Row nine."}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
}


def with_cookie() -> httpx.Response:
    return httpx.Response(200, json=ANSWER, headers={"set-cookie": "affinity=world-a; Path=/"})


def test_a_provider_cookie_does_not_reach_the_next_call(driver, make_world, test_clock):
    first, second = make_world(), make_world()
    provider = Provider(with_cookie)
    token_a = mint(SECRET, CATALOG, world_id=first, run_id="r1", job="cleanup_pass")
    token_b = mint(SECRET, CATALOG, world_id=second, run_id="r2", job="cleanup_pass")
    with TestClient(create_app(gateway_for(driver, provider))) as client:
        for token in (token_a, token_b):
            response = client.post(
                "/v1/chat/completions",
                json=SPARK_BODY,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
    assert [request.headers.get("cookie") for request in provider.requests] == [None, None]

    handler = Provider(with_cookie)
    with layer_for(driver, handler) as layer:
        layer.chat(first, job="verification", messages=MESSAGES)
        layer.chat(second, job="verification", messages=MESSAGES)
    assert [request.headers.get("cookie") for request in handler.requests] == [None, None]


def _wav(seconds: int, rate: int = 1000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(rate)
        writer.writeframes(b"\x80" * (seconds * rate))
    return out.getvalue()


def test_a_file_may_take_a_bounded_number_of_pieces():
    assert len(audio.split_wav(_wav(590 * audio.MAX_PIECES, rate=1))) == audio.MAX_PIECES
    with pytest.raises(AudioTooLong, match="at most 36 pieces"):
        audio.split_wav(_wav(590 * audio.MAX_PIECES + 1, rate=1))


def test_a_wav_header_that_breaks_the_writer_is_a_typed_refusal():
    # Two channels at the top rate: the byte rate that the piece's header must state needs more
    # than 32 bits, so the wave module raises struct.error when it writes the piece.
    good = _wav(1)
    crafted = good[:22] + struct.pack("<H", 2) + struct.pack("<I", 2**32 - 1) + good[28:]
    with pytest.raises(ModelRefused, match="readable WAV"):
        audio.split_wav(crafted)


def test_a_header_that_understates_the_audio_meters_what_the_provider_heard(
    driver, world, test_clock
):
    body = b'{"transcript": "pears", "audioDurationMs": 500000}'
    with layer_for(driver, Provider(lambda: httpx.Response(200, content=body))) as layer:
        transcript = layer.transcribe(world, audio_wav=_wav(1))
    assert transcript.audio_seconds == 500.0


def test_a_very_long_integer_duration_keeps_the_row(driver, world, test_clock):
    body = b'{"transcript": "pears", "audioDurationMs": 1' + b"0" * 400 + b"}"
    with layer_for(driver, Provider(lambda: httpx.Response(200, content=body))) as layer:
        assert layer.transcribe(world, audio_wav=_wav(100)).audio_seconds == 100.0
    assert [row["audio_seconds"] for row in rows(driver, world)] == [100.0]


def test_an_odd_background_status_keeps_the_row_and_cancels(driver, world, test_clock):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelled"})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": ["queued"]})
        return httpx.Response(404, json={"error": {"message": "gone"}})

    with layer_for(driver, handler) as layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name a fruit.", background=True)
    assert "/v1/responses/resp_1/cancel" in seen
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


class Listed(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for piece in self.chunks:
            yield piece


def test_a_deep_stream_line_neither_cuts_the_stream_nor_loses_the_usage(driver, world, test_clock):
    deep = b"data: " + DEEP + b"\n\n"
    text = b'data: {"choices": [{"delta": {"content": "Row nine."}}]}\n\n'
    finish = (
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
        b'"usage": {"prompt_tokens": 5000, "completion_tokens": 80}}\n\n'
    )
    provider = Provider(lambda: httpx.Response(200, stream=Listed(deep, text, finish)))

    response = post(driver, world, provider, SPARK_BODY | {"stream": True})

    assert response.status_code == 200 and response.content == deep + text + finish
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"]) == ("ok", 5000)


def test_a_deep_error_body_is_typed_in_both_paths(driver, world, test_clock):
    provider = Provider(lambda: httpx.Response(400, content=DEEP))
    response = post(driver, world, provider, SPARK_BODY)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "provider_error"

    handler = Provider(lambda: httpx.Response(400, content=DEEP))
    with layer_for(driver, handler) as layer, pytest.raises(ProviderError):
        layer.respond(world, job="extraction", input="Name a fruit.")
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"] * 2


def test_a_200_answer_with_nan_reaches_the_caller_as_it_came(driver, world, test_clock):
    raw = (
        b'{"choices": [{"message": {"role": "assistant", "content": "Row nine."}}], '
        b'"score": NaN, "usage": {"prompt_tokens": 10, "completion_tokens": 2}}'
    )
    provider = Provider(lambda: httpx.Response(200, content=raw))

    response = post(driver, world, provider, SPARK_BODY)

    assert response.status_code == 200 and response.content == raw
    assert [row["outcome"] for row in rows(driver, world)] == ["ok"]


@pytest.mark.parametrize("method", ["respond", "chat"])
def test_text_that_cannot_be_encoded_is_a_typed_refusal(driver, world, test_clock, method):
    handler = Provider(lambda: httpx.Response(200, json=ANSWER))
    cut = "half an emoji \ud83d"
    with layer_for(driver, handler) as layer, pytest.raises(ModelRefused, match="sent as JSON"):
        if method == "respond":
            layer.respond(world, job="extraction", input=cut)
        else:
            layer.chat(world, job="verification", messages=[{"role": "user", "content": cut}])
    assert handler.requests == []
    assert rows(driver, world) == []


def test_a_store_failure_at_the_gate_is_typed_and_sends_nothing(driver, world, test_clock):
    provider = Provider(lambda: httpx.Response(200, json=ANSWER))
    gateway = gateway_for(driver, provider)

    def down(*_: object, **__: object) -> None:
        raise RuntimeError("the store is down")

    gateway.meter.gate = down
    token = mint(SECRET, CATALOG, world_id=world, run_id="r1", job="cleanup_pass")
    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/chat/completions", json=SPARK_BODY, headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert response.json()["error"]["retryable"] is True
    assert provider.requests == []


def test_a_structured_call_with_no_text_is_invalid_output(driver, world, test_clock):
    call = {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    answer = {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    handler = Provider(lambda: httpx.Response(200, json=answer))
    with layer_for(driver, handler) as layer, pytest.raises(InvalidOutput, match="no text"):
        layer.chat(world, job="extraction", messages=MESSAGES, schema=SCHEMA)
    assert [row["outcome"] for row in rows(driver, world)] == ["invalid_output"]


def test_a_tool_call_with_tools_offered_is_ok(driver, world, test_clock):
    call = {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    tool = {"type": "function", "function": {"name": "bash"}}
    answer = {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    handler = Provider(lambda: httpx.Response(200, json=answer))
    with layer_for(driver, handler) as layer:
        result = layer.chat(
            world, job="verification", messages=MESSAGES, tools=[tool], schema=SCHEMA
        )
    assert result.tool_calls == (call,)
    assert [row["outcome"] for row in rows(driver, world)] == ["ok"]


def test_a_cut_answer_is_invalid_output(driver, world, test_clock):
    answer = {
        "choices": [
            {"message": {"role": "assistant", "content": "Row"}, "finish_reason": "length"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    handler = Provider(lambda: httpx.Response(200, json=answer))
    with layer_for(driver, handler) as layer, pytest.raises(InvalidOutput, match="cut"):
        layer.chat(world, job="verification", messages=MESSAGES)
    assert len(handler.requests) == 1
    assert [row["outcome"] for row in rows(driver, world)] == ["invalid_output"]


def test_glimmer_runs_on_its_own_sampling(driver, world, test_clock):
    answer = {"choices": [{"message": {"content": "Yes"}}], "usage": {"prompt_tokens": 1}}
    provider = Provider(lambda: httpx.Response(200, json=answer))
    body = {
        "model": "muse-glimmer-30b",
        "messages": MESSAGES,
        "temperature": 0,
        "top_p": 0.1,
        "stop": ["<|eom|>"],
    }
    assert post(driver, world, provider, body, job="second_opinion").status_code == 200
    sent = json.loads(provider.requests[0].content)
    assert not {"temperature", "top_p", "stop"} & set(sent)


def test_spark_keeps_its_sampling_fields():
    # Only Glimmer drops them: Meta takes them on Spark, and `stop` is dropped for its own reason.
    spark = CATALOG.model("muse-spark-1.3")
    out = upstream_body(
        SPARK_BODY | {"temperature": 0.2, "top_p": 0.9}, spec=spark, effort="low", job="smoke"
    )
    assert (out["temperature"], out["top_p"]) == (0.2, 0.9)


def test_a_row_says_when_its_tokens_are_an_estimate(driver, world, test_clock):
    plain = {"choices": [{"message": {"role": "assistant", "content": "Yes."}}]}
    provider = Provider(lambda: httpx.Response(200, json=plain))
    post(driver, world, provider, SPARK_BODY)
    handler = Provider(lambda: httpx.Response(200, json=ANSWER))
    with layer_for(driver, handler) as layer:
        layer.chat(world, job="verification", messages=MESSAGES)
    # Two rows at one instant have no fixed order, so the test reads them as a set.
    assert {row["usage_estimated"] for row in rows(driver, world)} == {None, True}


def test_the_backfill_stops_at_the_ceiling(driver, world, test_clock):
    set_budget_ceiling(driver, world, NOW.date(), 0.0)
    handler = Provider(lambda: httpx.Response(200, json=ANSWER))
    with layer_for(driver, handler) as layer:
        for job in ("backfill_triage", "backfill_extraction"):
            with pytest.raises(BudgetReached):
                layer.respond(world, job=job, input="Name a fruit.")
    assert handler.requests == []


def test_the_layer_can_attach_a_receipt(driver, world, test_clock):
    handler = Provider(lambda: httpx.Response(200, json=ANSWER))
    with layer_for(driver, handler) as layer:
        result = layer.chat(world, job="verification", messages=MESSAGES)
        layer.attach_receipt(world, result.model_call_id, "receipt-1")
    [row] = WorldQuery(driver, world).read(
        "MATCH (c:ModelCall) RETURN c.receipt_reference AS receipt"
    )
    assert row["receipt"] == "receipt-1"


def test_a_malformed_url_is_an_egress_refusal():
    policy = EgressPolicy.for_settings(settings())
    with pytest.raises(EgressRefused, match="malformed"):
        policy.check("model_layer", "https://[api.meta.ai/v1")


@pytest.mark.parametrize("url", ["https://api.meta.ai\t/v1", "https://api.\nmeta.ai/v1"])
def test_a_base_url_with_a_space_or_control_character_does_not_load(url):
    with pytest.raises(NotConfigured, match="GROVE_META_BASE_URL"):
        load_model_settings({"GROVE_META_BASE_URL": url})


def test_a_signed_token_with_a_job_that_is_not_text_is_401(test_clock, world):
    claims = {"w": world, "r": "r1", "j": ["cleanup_pass"], "exp": 2**40}
    payload = _b64(json.dumps(claims).encode())
    token = f"{PREFIX}.{payload}.{_sign(SECRET, payload)}"
    with pytest.raises(Unauthenticated):
        verify(SECRET, CATALOG, token)


def test_a_responses_input_keeps_only_role_and_content():
    message = {"role": "user", "content": "Name a fruit.", "extra": 1}
    assert clean_responses_input([message]) == [{"role": "user", "content": "Name a fruit."}]


def test_the_glimmer_server_reports_to_no_one():
    app_file = Path(__file__).resolve().parents[3] / "deploy" / "modal" / "glimmer.py"
    text = app_file.read_text(encoding="utf-8")
    assert '"VLLM_NO_USAGE_STATS": "1"' in text and '"DO_NOT_TRACK": "1"' in text


TOOL = {
    "type": "function",
    "function": {
        "name": "brain_status",
        "description": "Report the status of the synthetic orchard store.",
        "parameters": {"type": "object", "properties": {"verbose": {"type": "boolean"}}},
    },
}


def test_the_tool_result_leg_of_a_round_trip_passes_through_the_gateway(driver, world, test_clock):
    # The exact shape of OpenCode's second request: the assistant turn with its call, then the
    # tool's result linked to it. A refused role or a lost link would break every agent run.
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "brain_status", "arguments": '{"verbose":false}'},
    }
    history = [
        *MESSAGES,
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"items": 4}'},
    ]
    provider = Provider(lambda: httpx.Response(200, json=ANSWER))

    response = post(
        driver,
        world,
        provider,
        {
            "model": "muse-spark-1.3",
            "messages": history,
            "tools": [TOOL],
            "tool_choice": "auto",
            "max_tokens": 8192,
        },
    )

    assert response.status_code == 200 and response.json() == ANSWER
    sent = json.loads(provider.requests[0].content)
    assert sent["messages"] == history
    assert sent["tools"] == [TOOL]
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"]) == ("ok", 10)


NUMBER_SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}}}


@pytest.mark.parametrize(
    "text",
    ['{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}', '{"score": 1e999}'],
    ids=["nan", "infinity", "minus-infinity", "past-the-range"],
)
def test_a_number_that_is_not_json_is_declined_by_the_validator(driver, world, test_clock, text):
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    handler = Provider(lambda: httpx.Response(200, json=answer))
    with layer_for(driver, handler) as layer, pytest.raises(InvalidOutput):
        layer.respond(world, job="extraction", input="Score it.", schema=NUMBER_SCHEMA)
    assert [row["outcome"] for row in rows(driver, world)] == ["invalid_output"]


@pytest.mark.parametrize("value", [0.5, 1e10, 3, -2.5])
def test_an_ordinary_number_still_passes_the_validator(driver, world, test_clock, value):
    text = json.dumps({"score": value})
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    handler = Provider(lambda: httpx.Response(200, json=answer))
    with layer_for(driver, handler) as layer:
        result = layer.respond(world, job="extraction", input="Score it.", schema=NUMBER_SCHEMA)
    assert result.data == {"score": value}


def test_the_glimmer_image_gives_modal_a_python():
    # The first download on Modal failed with "unable to determine the version of Python": the
    # vLLM image has python3 and no python.
    app_file = Path(__file__).resolve().parents[3] / "deploy" / "modal" / "glimmer.py"
    text = app_file.read_text(encoding="utf-8")
    link = """.run_commands('ln -sf "$(which python3)" /usr/bin/python')"""
    assert link in text
    assert text.index(".entrypoint([])") < text.index(link) < text.index(".env(")
