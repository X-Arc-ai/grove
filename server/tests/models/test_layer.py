"""ModelLayer against the live dev store, with every provider faked by httpx.MockTransport (D15).

Each test reads back the model_calls rows and the day's budget row that the layer wrote. The clock
is a TestClock at 2026-11-02 09:00 UTC, so the day of every row is known.
"""

import hashlib
import io
import json
import re
import wave
from datetime import UTC, date, datetime

import httpx
import pytest

from grove import clock
from grove.clock import TestClock
from grove.models.config import Catalog, ModelSettings, ModelSpec, load_catalog
from grove.models.errors import (
    BudgetReached,
    InvalidOutput,
    ModelRefused,
    NotConfigured,
    ProviderError,
    RateLimited,
)
from grove.models.http import MODAL_ATTEMPTS
from grove.models.layer import ModelLayer, Usage
from grove.store.query import WorldQuery
from grove.writer.model_calls import BudgetState, set_budget_ceiling

NINE = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
DAY = date(2026, 11, 2)
NEXT_DAY = date(2026, 11, 3)
RESPONSES_URL = "https://api.meta.ai/v1/responses"
META_CHAT_URL = "https://api.meta.ai/v1/chat/completions"
GLIMMER_CHAT_URL = "https://glimmer.example.test/v1/chat/completions"
TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"
META_AUTH = "Bearer test-meta-key"
GLIMMER_AUTH = "Bearer test-glimmer-key"
SPARK = "muse-spark-1.3"
GLIMMER = "muse-glimmer-30b"
VOICE = "muse-voice-transcribe-1.0"

INSTRUCTIONS = "Extract the facts about the orchard as JSON."
EVENT = "Lio Varn planted twelve pear trees in the orchard."
SCHEMA = {
    "type": "object",
    "properties": {"place": {"type": "string"}, "planted_by": {"type": "string"}},
    "required": ["place", "planted_by"],
    "additionalProperties": False,
}
ANSWER = {"place": "orchard", "planted_by": "Lio Varn"}

SPARK_USAGE = {
    "input_tokens": 1200,
    "input_tokens_details": {"cached_tokens": 800},
    "output_tokens": 300,
    "output_tokens_details": {"reasoning_tokens": 120},
    "total_tokens": 1500,
}
# 400 uncached x 1.25 + 800 cached x 0.15 + 300 output (reasoning included) x 4.25, per million.
SPARK_COST = 0.001895

GLIMMER_USAGE = {
    "prompt_tokens": 500,
    "completion_tokens": 40,
    "completion_tokens_details": {"reasoning_tokens": 30},
    "total_tokens": 540,
}
GLIMMER_PER_SECOND = 0.001097
VOICE_PER_HOUR = 0.18
SYSTEM_JUDGE = "Judge one claim about the orchard. Answer agree or disagree."
JUDGE_MESSAGES = [
    {"role": "system", "content": SYSTEM_JUDGE},
    {"role": "user", "content": "Lio Varn owns the orchard."},
]


def digest_key(job: str, prefix: str) -> str:
    return f"grove:{job}:{hashlib.sha256(prefix.encode('utf-8')).hexdigest()[:24]}"


def settings(**changes) -> ModelSettings:
    values = {
        "meta_key": "test-meta-key",
        "glimmer_base_url": "https://glimmer.example.test/v1",
        "glimmer_key": "test-glimmer-key",
        "daily_budget": 5.0,
        "gateway_secret": b"s" * 32,
    }
    return ModelSettings(**{**values, **changes})


def reply(doc: object = None, status: int = 200, headers: dict | None = None) -> tuple:
    return status, doc, headers or {}


class FakeProvider:
    """Gives each request the next scripted reply; the last reply repeats. No reply means a
    request is a test failure."""

    def __init__(self, *replies: tuple, on_request=None, error: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._replies = list(replies)
        self._on_request = on_request
        self._error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request(request)
        if self._error is not None:
            raise self._error
        if not self._replies:
            raise AssertionError(f"the provider was called: {request.method} {request.url}")
        status, doc, headers = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if doc is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=doc, headers=headers)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def targets(self) -> list[tuple[str, str, str]]:
        return [
            (request.method, str(request.url), request.headers.get("authorization"))
            for request in self.requests
        ]


def spark_doc(
    *texts: str,
    status: str = "completed",
    usage: dict | None = None,
    response_id: str = "resp_orchard",
    incomplete: dict | None = None,
) -> dict:
    doc = {
        "id": response_id,
        "object": "response",
        "status": status,
        "output": [
            # A reasoning item is never part of the answer, whatever it holds.
            {"type": "reasoning", "content": [{"type": "output_text", "text": "hidden thought"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text} for text in texts],
            },
        ],
        "usage": SPARK_USAGE if usage is None else usage,
    }
    if incomplete is not None:
        doc["incomplete_details"] = incomplete
    return doc


def chat_doc(
    content: str | None,
    *,
    usage: dict | None = None,
    tool_calls: list[dict] | None = None,
    finish: str = "stop",
) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chat_orchard",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": GLIMMER_USAGE if usage is None else usage,
    }


def model_calls(driver, world_id: str) -> list[dict]:
    rows = WorldQuery(driver, world_id).read("MATCH (c:ModelCall) RETURN c ORDER BY c.at, c.id")
    return [row["c"] for row in rows]


def budgets(driver, world_id: str) -> list[dict]:
    rows = WorldQuery(driver, world_id).read("MATCH (b:Budget) RETURN b ORDER BY b.day")
    return [row["b"] for row in rows]


def call_row(world_id: str, row_id: str, **fields) -> dict:
    """The full model_calls row the layer must write: zero tokens and no latency by default."""
    row = {
        "id": row_id,
        "world_id": world_id,
        "route": "meta",
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost": 0.0,
        "latency_ms": 0,
        "at": NINE,
        "day": DAY,
    }
    row.update(fields)
    if isinstance(row["cost"], float):
        row["cost"] = pytest.approx(row["cost"], abs=1e-12)
    return row


def spark_row(world_id: str, row_id: str, **fields) -> dict:
    values = {
        "job": "extraction",
        "model": SPARK,
        "outcome": "ok",
        "effort": "minimal",
        "input_tokens": 1200,
        "cached_tokens": 800,
        "output_tokens": 300,
        "reasoning_tokens": 120,
        "cost": SPARK_COST,
    }
    return call_row(world_id, row_id, **{**values, **fields})


def wav(seconds: int, *, rate: int, width: int) -> tuple[bytes, bytes]:
    count = seconds * rate * width
    pattern = bytes(range(251))
    frames = (pattern * (count // len(pattern) + 1))[:count]
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return out.getvalue(), frames


def multipart(request: httpx.Request) -> dict[str, tuple[dict[str, str], bytes]]:
    """The parts of a multipart/form-data request: name -> (lower-case headers, exact bytes)."""
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = b"--" + content_type.split("boundary=", 1)[1].encode("ascii")
    chunks = request.content.split(boundary)
    assert chunks[0] == b"" and chunks[-1] == b"--\r\n"
    parts = {}
    for chunk in chunks[1:-1]:
        assert chunk.startswith(b"\r\n") and chunk.endswith(b"\r\n")
        head, _, data = chunk[2:-2].partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode("ascii").split("\r\n"):
            name, _, value = line.partition(": ")
            headers[name.lower()] = value
        name = re.search(r'name="([^"]*)"', headers["content-disposition"]).group(1)
        parts[name] = (headers, data)
    return parts


def wav_frames(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(io.BytesIO(data), "rb") as reader:
        return (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
            reader.readframes(reader.getnframes()),
        )


@pytest.fixture
def test_clock():
    fixed = TestClock()
    fixed.set(NINE)
    with clock.using(fixed):
        yield fixed


@pytest.fixture
def make_layer(driver, migrated, test_clock):
    opened = []

    def make(provider: FakeProvider, *, catalog: Catalog | None = None, **changes) -> ModelLayer:
        layer = ModelLayer(
            driver,
            settings=settings(**changes),
            catalog=catalog,
            transport=httpx.MockTransport(provider),
            sleep=provider.sleeps.append,
        )
        opened.append(layer)
        return layer

    yield make
    for layer in opened:
        layer.close()


# respond(): Spark through the Responses API.


def test_respond_sends_the_responses_body_and_writes_one_ok_row(driver, world, make_layer):
    provider = FakeProvider(reply(spark_doc('{"place": "orchard", ', '"planted_by": "Lio Varn"}')))
    layer = make_layer(provider)

    result = layer.respond(
        world,
        job="extraction",
        input=EVENT,
        instructions=INSTRUCTIONS,
        schema=SCHEMA,
        run_id="run-orchard-1",
        max_output_tokens=2048,
    )

    assert provider.targets() == [("POST", RESPONSES_URL, META_AUTH)]
    assert provider.bodies() == [
        {
            "model": SPARK,
            "input": EVENT,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 2048,
            "store": False,
            "instructions": INSTRUCTIONS,
            "prompt_cache_key": digest_key("extraction", INSTRUCTIONS),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "strict": True,
                    "schema": SCHEMA,
                }
            },
        }
    ]
    assert result.data == ANSWER
    assert result.text == '{"place": "orchard", "planted_by": "Lio Varn"}'
    assert (result.model, result.effort, result.finish) == (SPARK, "minimal", "completed")
    assert result.usage == Usage(
        input_tokens=1200, cached_tokens=800, output_tokens=300, reasoning_tokens=120
    )
    assert result.cost == pytest.approx(SPARK_COST, abs=1e-12)
    assert result.budget == BudgetState(
        spent=pytest.approx(SPARK_COST, abs=1e-12), ceiling=5.0, state="open"
    )
    assert model_calls(driver, world) == [
        spark_row(world, result.model_call_id, run_id="run-orchard-1")
    ]
    assert budgets(driver, world) == [
        {
            "world_id": world,
            "day": DAY,
            "spent": pytest.approx(SPARK_COST, abs=1e-12),
            "ceiling": 5.0,
            "state": "open",
        }
    ]
    assert provider.sleeps == []


@pytest.mark.parametrize(
    ("job", "effort"),
    [("extraction", "minimal"), ("verification", "low"), ("consolidation", "high")],
)
def test_respond_takes_the_effort_from_the_job_table(driver, world, make_layer, job, effort):
    provider = FakeProvider(reply(spark_doc("Lio Varn planted pears.")))

    result = make_layer(provider).respond(world, job=job, input=EVENT)

    assert provider.bodies()[0]["reasoning"] == {"effort": effort}
    assert result.effort == effort
    assert [(row["job"], row["effort"]) for row in model_calls(driver, world)] == [(job, effort)]


def test_respond_sends_an_explicit_effort_and_no_optional_fields(driver, world, make_layer):
    provider = FakeProvider(reply(spark_doc("Lio Varn planted pears.")))
    event = [{"role": "user", "content": EVENT}]

    result = make_layer(provider).respond(world, job="extraction", input=event, effort="max")

    assert provider.bodies() == [
        {
            "model": SPARK,
            "input": event,
            "reasoning": {"effort": "max"},
            "max_output_tokens": 8192,
            "store": False,
        }
    ]
    assert result.data is None
    assert result.text == "Lio Varn planted pears."
    assert model_calls(driver, world) == [spark_row(world, result.model_call_id, effort="max")]


def test_respond_cache_key_is_a_stable_digest_of_the_instructions(driver, world, make_layer):
    provider = FakeProvider(reply(spark_doc("noted")))
    layer = make_layer(provider)
    other = "Extract the people in the orchard as JSON."

    layer.respond(world, job="extraction", input=EVENT, instructions=INSTRUCTIONS)
    layer.respond(world, job="extraction", input="A second event.", instructions=INSTRUCTIONS)
    layer.respond(world, job="extraction", input=EVENT, instructions=other)
    layer.respond(world, job="verification", input=EVENT, instructions=INSTRUCTIONS)
    layer.respond(
        world, job="extraction", input=EVENT, instructions=INSTRUCTIONS, cache_key="orchard-key"
    )

    keys = [body.get("prompt_cache_key") for body in provider.bodies()]
    assert keys == [
        digest_key("extraction", INSTRUCTIONS),
        digest_key("extraction", INSTRUCTIONS),
        digest_key("extraction", other),
        digest_key("verification", INSTRUCTIONS),
        "orchard-key",
    ]
    assert keys[0] != keys[2] and keys[0] != keys[3]
    assert INSTRUCTIONS not in keys[0]
    assert len(model_calls(driver, world)) == 5


@pytest.mark.parametrize("max_output_tokens", [1023, 1, 0, 65537])
def test_respond_refuses_max_output_tokens_off_the_range_before_any_request(
    driver, world, make_layer, max_output_tokens
):
    provider = FakeProvider()

    with pytest.raises(ModelRefused, match="max_output_tokens must be from 1024 to 65536"):
        make_layer(provider).respond(
            world, job="extraction", input=EVENT, max_output_tokens=max_output_tokens
        )

    assert provider.requests == []
    assert model_calls(driver, world) == []
    assert budgets(driver, world) == []


def test_respond_takes_exactly_1024_output_tokens(driver, world, make_layer):
    provider = FakeProvider(reply(spark_doc("noted")))

    make_layer(provider).respond(world, job="extraction", input=EVENT, max_output_tokens=1024)

    assert provider.bodies()[0]["max_output_tokens"] == 1024
    assert len(model_calls(driver, world)) == 1


@pytest.mark.parametrize(
    "call",
    [
        {"job": "second_opinion"},
        {"job": "no_such_job"},
        {"job": "extraction", "effort": "turbo"},
        {"job": "extraction", "schema": {"type": "object", "$defs": {}}},
        {"job": "extraction", "schema": {"type": "array"}},
    ],
    ids=["glimmer-job", "unknown-job", "unknown-effort", "schema-reference", "not-object"],
)
def test_respond_refuses_a_bad_call_before_any_request(driver, world, make_layer, call):
    provider = FakeProvider()

    with pytest.raises(ModelRefused):
        make_layer(provider).respond(world, input=EVENT, **call)

    assert provider.requests == []
    assert model_calls(driver, world) == []


def test_respond_with_no_meta_key_raises_not_configured_and_sends_nothing(
    driver, world, make_layer
):
    provider = FakeProvider()

    with pytest.raises(NotConfigured, match="MODEL_API_KEY") as raised:
        make_layer(provider, meta_key=None).respond(world, job="extraction", input=EVENT)

    assert raised.value.status == 503
    assert provider.requests == []
    assert model_calls(driver, world) == []


def test_respond_non_json_answer_raises_invalid_output_and_writes_an_invalid_output_row(
    driver, world, make_layer
):
    provider = FakeProvider(reply(spark_doc("The orchard belongs to Lio Varn.")))

    with pytest.raises(InvalidOutput, match="not JSON") as raised:
        make_layer(provider).respond(
            world, job="extraction", input=EVENT, instructions=INSTRUCTIONS, schema=SCHEMA
        )

    assert raised.value.code == "invalid_output"
    assert len(provider.requests) == 1
    rows = model_calls(driver, world)
    assert rows == [spark_row(world, rows[0]["id"], outcome="invalid_output")]
    assert budgets(driver, world)[0]["spent"] == pytest.approx(SPARK_COST, abs=1e-12)


def test_respond_schema_miss_raises_invalid_output_and_writes_an_invalid_output_row(
    driver, world, make_layer
):
    provider = FakeProvider(reply(spark_doc('{"place": 7}')))

    with pytest.raises(InvalidOutput, match="does not fit the schema") as raised:
        make_layer(provider).respond(
            world, job="extraction", input=EVENT, instructions=INSTRUCTIONS, schema=SCHEMA
        )

    # The missing required field sits at the root, the wrong type at /place.
    assert raised.value.details["paths"] == ["", "place"]
    assert len(provider.requests) == 1
    rows = model_calls(driver, world)
    assert rows == [spark_row(world, rows[0]["id"], outcome="invalid_output")]


@pytest.mark.parametrize(
    ("status", "texts", "incomplete"),
    [
        ("incomplete", ("Lio Varn planted",), {"reason": "max_output_tokens"}),
        ("completed", (" \n ",), None),
        ("failed", ("Lio Varn planted pears.",), None),
    ],
    ids=["incomplete-with-text", "completed-blank", "failed-with-text"],
)
def test_respond_without_a_complete_answer_raises_invalid_output_with_a_row(
    driver, world, make_layer, status, texts, incomplete
):
    doc = spark_doc(*texts, status=status, incomplete=incomplete)
    provider = FakeProvider(reply(doc))

    with pytest.raises(InvalidOutput) as raised:
        make_layer(provider).respond(world, job="extraction", input=EVENT, run_id="run-orchard-2")

    rows = model_calls(driver, world)
    assert rows == [
        spark_row(world, rows[0]["id"], outcome="invalid_output", run_id="run-orchard-2")
    ]
    assert raised.value.details == {
        "status": status,
        "incomplete": incomplete,
        "model_call_id": rows[0]["id"],
    }


def test_respond_429_twice_then_200_sleeps_the_stated_reset_and_writes_one_ok_row(
    driver, world, make_layer
):
    provider = FakeProvider(
        reply({"error": {"message": "slow down"}}, 429, {"Retry-After": "7"}),
        reply({"error": {"message": "slow down"}}, 429, {"x-ratelimit-reset-tokens": "2.5s"}),
        reply(spark_doc("noted")),
    )

    result = make_layer(provider).respond(world, job="extraction", input=EVENT)

    assert provider.sleeps == [7.0, 2.5]
    assert len(provider.requests) == 3
    assert provider.bodies()[0] == provider.bodies()[2]
    assert model_calls(driver, world) == [spark_row(world, result.model_call_id)]


def test_respond_429_on_every_attempt_raises_rate_limited_with_a_row(driver, world, make_layer):
    provider = FakeProvider(reply({"error": {"message": "slow down"}}, 429, {"Retry-After": "7"}))

    with pytest.raises(RateLimited) as raised:
        make_layer(provider).respond(world, job="extraction", input=EVENT)

    assert raised.value.details == {"reset_after_seconds": 7.0}
    assert raised.value.retryable is True
    assert len(provider.requests) == 4
    assert provider.sleeps == [7.0, 7.0, 7.0]
    rows = model_calls(driver, world)
    assert rows == [
        call_row(
            world,
            rows[0]["id"],
            job="extraction",
            model=SPARK,
            outcome="rate_limited",
            effort="minimal",
        )
    ]


def test_respond_400_writes_a_provider_error_row_and_raises_not_retryable(
    driver, world, make_layer
):
    provider = FakeProvider(reply({"error": {"message": "unknown parameter: verbosity"}}, 400))

    with pytest.raises(ProviderError) as raised:
        make_layer(provider).respond(world, job="verification", input=EVENT, run_id="run-v")

    assert raised.value.retryable is False
    assert raised.value.details == {
        "status": 400,
        "provider_message": "unknown parameter: verbosity",
    }
    assert len(provider.requests) == 1
    assert provider.sleeps == []
    rows = model_calls(driver, world)
    assert rows == [
        call_row(
            world,
            rows[0]["id"],
            job="verification",
            model=SPARK,
            outcome="provider_error",
            effort="low",
            run_id="run-v",
        )
    ]
    assert budgets(driver, world)[0]["spent"] == 0.0


def test_respond_503_after_all_attempts_raises_a_retryable_provider_error(
    driver, world, make_layer
):
    provider = FakeProvider(reply({"error": {"message": "warming up"}}, 503))

    with pytest.raises(ProviderError) as raised:
        make_layer(provider).respond(world, job="extraction", input=EVENT)

    assert raised.value.retryable is True
    assert raised.value.details == {"status": 503, "provider_message": "warming up"}
    assert len(provider.requests) == 4
    assert provider.sleeps == [2.0, 4.0, 8.0]
    rows = model_calls(driver, world)
    assert rows == [
        call_row(
            world,
            rows[0]["id"],
            job="extraction",
            model=SPARK,
            outcome="provider_error",
            effort="minimal",
        )
    ]


def test_respond_with_no_answer_from_the_provider_writes_a_provider_error_row(
    driver, world, make_layer
):
    provider = FakeProvider(error=httpx.ConnectError("connection refused"))

    with pytest.raises(ProviderError) as raised:
        make_layer(provider).respond(world, job="extraction", input=EVENT)

    assert raised.value.retryable is True
    assert len(provider.requests) == 4
    assert provider.sleeps == [2.0, 4.0, 8.0]
    assert [row["outcome"] for row in model_calls(driver, world)] == ["provider_error"]


def test_respond_background_polls_until_completed_and_writes_one_row(driver, world, make_layer):
    provider = FakeProvider(
        reply({"id": "resp_bg1", "object": "response", "status": "queued"}),
        reply({"id": "resp_bg1", "object": "response", "status": "in_progress"}),
        reply(spark_doc("Lio Varn planted pears.", response_id="resp_bg1")),
    )

    result = make_layer(provider).respond(
        world, job="consolidation", input=EVENT, instructions=INSTRUCTIONS, background=True
    )

    assert provider.targets() == [
        ("POST", RESPONSES_URL, META_AUTH),
        ("GET", f"{RESPONSES_URL}/resp_bg1", META_AUTH),
        ("GET", f"{RESPONSES_URL}/resp_bg1", META_AUTH),
    ]
    assert json.loads(provider.requests[0].content) == {
        "model": SPARK,
        "input": EVENT,
        "reasoning": {"effort": "high"},
        "max_output_tokens": 8192,
        "store": True,
        "instructions": INSTRUCTIONS,
        "prompt_cache_key": digest_key("consolidation", INSTRUCTIONS),
        "background": True,
    }
    assert provider.sleeps == [1.0, 2.0]
    assert result.text == "Lio Varn planted pears."
    assert model_calls(driver, world) == [
        spark_row(world, result.model_call_id, job="consolidation", effort="high")
    ]


# chat(): Glimmer on Modal, or Spark on Meta.


def test_chat_glimmer_sends_reasoning_strength_to_modal_and_meters_gpu_seconds(
    driver, world, make_layer, test_clock
):
    provider = FakeProvider(
        reply(chat_doc("agree")), on_request=lambda _: test_clock.advance(seconds=2)
    )

    result = make_layer(provider).chat(
        world, job="second_opinion", messages=JUDGE_MESSAGES, run_id="run-g", max_tokens=2048
    )

    assert provider.targets() == [("POST", GLIMMER_CHAT_URL, GLIMMER_AUTH)]
    body = provider.bodies()[0]
    assert body == {
        "model": GLIMMER,
        "messages": JUDGE_MESSAGES,
        "max_tokens": 2048,
        "chat_template_kwargs": {"reasoning_strength": "medium"},
    }
    assert "reasoning_effort" not in body and "prompt_cache_key" not in body
    assert (result.text, result.effort, result.model, result.finish) == (
        "agree",
        "medium",
        GLIMMER,
        "stop",
    )
    assert result.usage == Usage(input_tokens=500, output_tokens=40, reasoning_tokens=30)
    # Two seconds of wall time on the GPU route.
    assert result.cost == pytest.approx(2 * GLIMMER_PER_SECOND, abs=1e-12)
    assert model_calls(driver, world) == [
        call_row(
            world,
            result.model_call_id,
            job="second_opinion",
            model=GLIMMER,
            route="modal",
            outcome="ok",
            effort="medium",
            run_id="run-g",
            input_tokens=500,
            output_tokens=40,
            reasoning_tokens=30,
            cost=2 * GLIMMER_PER_SECOND,
            latency_ms=2000,
            at=datetime(2026, 11, 2, 9, 0, 2, tzinfo=UTC),
        )
    ]


def test_chat_glimmer_with_a_schema_sends_response_format_and_parses(driver, world, make_layer):
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string", "enum": ["agree", "disagree"]}},
        "required": ["verdict"],
        "additionalProperties": False,
    }
    provider = FakeProvider(reply(chat_doc('{"verdict": "disagree"}')))

    result = make_layer(provider).chat(
        world, job="second_opinion", messages=JUDGE_MESSAGES, schema=schema, schema_name="verdict"
    )

    assert provider.bodies()[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "verdict", "strict": True, "schema": schema},
    }
    assert result.data == {"verdict": "disagree"}


def test_chat_schema_miss_raises_invalid_output_and_writes_an_invalid_output_row(
    driver, world, make_layer
):
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    provider = FakeProvider(reply(chat_doc("I agree with Lio Varn.")))

    with pytest.raises(InvalidOutput, match="not JSON"):
        make_layer(provider).chat(
            world, job="second_opinion", messages=JUDGE_MESSAGES, schema=schema
        )

    # A non-empty answer is never retried at a lower effort.
    assert len(provider.requests) == 1
    assert [(row["outcome"], row["effort"]) for row in model_calls(driver, world)] == [
        ("invalid_output", "medium")
    ]


def test_chat_glimmer_empty_answer_at_medium_is_retried_at_low(
    driver, world, make_layer, test_clock
):
    provider = FakeProvider(
        reply(chat_doc("   ")),
        reply(chat_doc("agree")),
        on_request=lambda _: test_clock.advance(seconds=1),
    )

    result = make_layer(provider).chat(world, job="second_opinion", messages=JUDGE_MESSAGES)

    assert [body["chat_template_kwargs"] for body in provider.bodies()] == [
        {"reasoning_strength": "medium"},
        {"reasoning_strength": "low"},
    ]
    assert (result.text, result.effort) == ("agree", "low")
    rows = model_calls(driver, world)
    assert [(row["outcome"], row["effort"]) for row in rows] == [
        ("invalid_output", "medium"),
        ("ok", "low"),
    ]
    assert rows[1]["id"] == result.model_call_id
    assert [row["input_tokens"] for row in rows] == [500, 500]


def test_chat_glimmer_empty_answer_at_low_raises_invalid_output(driver, world, make_layer):
    provider = FakeProvider(reply(chat_doc("", finish="length")))

    with pytest.raises(InvalidOutput, match="empty answer") as raised:
        make_layer(provider).chat(
            world, job="second_opinion", messages=JUDGE_MESSAGES, effort="low"
        )

    assert len(provider.requests) == 1
    assert provider.bodies()[0]["chat_template_kwargs"] == {"reasoning_strength": "low"}
    rows = model_calls(driver, world)
    assert [(row["outcome"], row["effort"], row["route"]) for row in rows] == [
        ("invalid_output", "low", "modal")
    ]
    assert raised.value.details == {
        "empty": True,
        "finish": "length",
        "model_call_id": rows[0]["id"],
    }


def test_chat_glimmer_503_is_retried_past_four_attempts_and_then_succeeds(
    driver, world, make_layer
):
    cold = reply({"error": {"message": "no container yet"}}, 503)
    provider = FakeProvider(cold, cold, cold, cold, cold, cold, reply(chat_doc("agree")))

    result = make_layer(provider).chat(world, job="second_opinion", messages=JUDGE_MESSAGES)

    assert len(provider.requests) == 7
    assert provider.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert result.text == "agree"
    assert [row["outcome"] for row in model_calls(driver, world)] == ["ok"]


def test_chat_glimmer_503_on_every_attempt_stops_at_modal_attempts(driver, world, make_layer):
    provider = FakeProvider(reply({"error": {"message": "no container yet"}}, 503))

    with pytest.raises(ProviderError) as raised:
        make_layer(provider).chat(world, job="second_opinion", messages=JUDGE_MESSAGES)

    assert raised.value.retryable is True
    assert MODAL_ATTEMPTS > 4
    assert len(provider.requests) == MODAL_ATTEMPTS
    assert [row["outcome"] for row in model_calls(driver, world)] == ["provider_error"]


def test_chat_spark_503_stops_at_four_attempts(driver, world, make_layer):
    cold = reply({"error": {"message": "busy"}}, 503)
    provider = FakeProvider(cold, cold, cold, cold, reply(chat_doc("agree")))

    with pytest.raises(ProviderError):
        make_layer(provider).chat(world, job="verification", messages=JUDGE_MESSAGES)

    assert len(provider.requests) == 4
    assert provider.sleeps == [2.0, 4.0, 8.0]
    assert [row["outcome"] for row in model_calls(driver, world)] == ["provider_error"]


@pytest.mark.parametrize(
    ("changes", "variable"),
    [
        ({"glimmer_base_url": None}, "GROVE_GLIMMER_BASE_URL"),
        ({"glimmer_key": None}, "GROVE_GLIMMER_KEY"),
    ],
)
def test_chat_glimmer_not_configured_raises_and_writes_nothing(
    driver, world, make_layer, changes, variable
):
    provider = FakeProvider()

    with pytest.raises(NotConfigured, match=variable) as raised:
        make_layer(provider, **changes).chat(world, job="second_opinion", messages=JUDGE_MESSAGES)

    assert "test-glimmer-key" not in str(raised.value)
    assert provider.requests == []
    assert model_calls(driver, world) == []
    assert budgets(driver, world) == []


def test_chat_spark_job_goes_to_meta_with_effort_cache_key_and_tools(driver, world, make_layer):
    system = "You verify claims about the orchard with the lookup tool."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "Who planted?"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_place",
                "description": "Find a place by name.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        }
    ]
    tool_call = {
        "id": "call_orchard",
        "type": "function",
        "function": {"name": "lookup_place", "arguments": '{"name": "orchard"}'},
    }
    usage = {
        "prompt_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 600},
        "completion_tokens": 50,
        "completion_tokens_details": {"reasoning_tokens": 20},
    }
    provider = FakeProvider(
        reply(chat_doc(None, usage=usage, tool_calls=[tool_call], finish="tool_calls"))
    )

    result = make_layer(provider).chat(
        world, job="verification", messages=messages, tools=tools, max_tokens=4096
    )

    assert provider.targets() == [("POST", META_CHAT_URL, META_AUTH)]
    assert provider.bodies() == [
        {
            "model": SPARK,
            "messages": messages,
            "max_tokens": 4096,
            "reasoning_effort": "low",
            "prompt_cache_key": digest_key("verification", system),
            "tools": tools,
            "tool_choice": "auto",
        }
    ]
    assert result.tool_calls == (tool_call,)
    assert (result.text, result.data, result.finish) == ("", None, "tool_calls")
    assert result.usage == Usage(
        input_tokens=900, cached_tokens=600, output_tokens=50, reasoning_tokens=20
    )
    # 300 uncached x 1.25 + 600 cached x 0.15 + 50 output x 4.25, per million.
    assert result.cost == pytest.approx(0.0006775, abs=1e-12)
    assert model_calls(driver, world) == [
        call_row(
            world,
            result.model_call_id,
            job="verification",
            model=SPARK,
            outcome="ok",
            effort="low",
            input_tokens=900,
            cached_tokens=600,
            output_tokens=50,
            reasoning_tokens=20,
            cost=0.0006775,
        )
    ]


def test_chat_spark_job_with_no_system_message_sends_no_cache_key(driver, world, make_layer):
    provider = FakeProvider(reply(chat_doc("agree")))

    make_layer(provider).chat(
        world, job="verification", messages=[{"role": "user", "content": "Who planted?"}]
    )

    body = provider.bodies()[0]
    assert "prompt_cache_key" not in body
    assert "tools" not in body and "tool_choice" not in body
    assert body["reasoning_effort"] == "low"


# transcribe(): Voice Transcribe.


def test_transcribe_posts_multipart_and_meters_the_stated_seconds(driver, world, make_layer):
    audio, frames = wav(3, rate=8000, width=2)
    provider = FakeProvider(
        reply({"transcript": "  Lio Varn walked to the orchard. ", "audioDurationMs": 3120})
    )

    transcript = make_layer(provider).transcribe(world, audio_wav=audio, run_id="run-voice")

    assert provider.targets() == [("POST", TRANSCRIBE_URL, META_AUTH)]
    parts = multipart(provider.requests[0])
    assert sorted(parts) == ["audio", "request"]
    request_headers, request_body = parts["request"]
    assert request_headers == {
        "content-disposition": 'form-data; name="request"',
        "content-type": "application/json",
    }
    assert json.loads(request_body) == {
        "model": VOICE,
        "audioEncoding": "WAV",
        "mode": "PUSH_TO_TALK",
    }
    audio_headers, audio_body = parts["audio"]
    assert audio_headers == {
        "content-disposition": 'form-data; name="audio"; filename="audio.wav"',
        "content-type": "audio/wav",
    }
    assert wav_frames(audio_body) == (1, 2, 8000, frames)
    cost = 3.12 / 3600 * VOICE_PER_HOUR
    assert transcript.text == "Lio Varn walked to the orchard."
    assert transcript.audio_seconds == pytest.approx(3.12, abs=1e-12)
    assert transcript.cost == pytest.approx(cost, abs=1e-12)
    assert len(transcript.model_call_ids) == 1
    rows = model_calls(driver, world)
    assert rows == [
        call_row(
            world,
            transcript.model_call_ids[0],
            job="transcription",
            model=VOICE,
            outcome="ok",
            run_id="run-voice",
            audio_seconds=pytest.approx(3.12, abs=1e-12),
            cost=cost,
        )
    ]
    assert "effort" not in rows[0]


def test_transcribe_splits_a_1250_second_wav_into_three_calls_and_rows(
    driver, world, make_layer, test_clock
):
    audio, frames = wav(1250, rate=1000, width=1)
    provider = FakeProvider(
        reply({"transcript": "Lio Varn"}),
        reply({"transcript": "walked to"}),
        reply({"transcript": "the orchard."}),
        on_request=lambda _: test_clock.advance(seconds=1),
    )

    transcript = make_layer(provider).transcribe(world, audio_wav=audio)

    assert provider.targets() == [("POST", TRANSCRIBE_URL, META_AUTH)] * 3
    sent = [wav_frames(multipart(request)["audio"][1]) for request in provider.requests]
    assert sent == [
        (1, 1, 1000, frames[:590_000]),
        (1, 1, 1000, frames[590_000:1_180_000]),
        (1, 1, 1000, frames[1_180_000:]),
    ]
    assert transcript.text == "Lio Varn walked to the orchard."
    assert transcript.audio_seconds == pytest.approx(1250.0, abs=1e-12)
    assert transcript.cost == pytest.approx(1250 / 3600 * VOICE_PER_HOUR, abs=1e-12)
    rows = model_calls(driver, world)
    # No stated duration, so each row carries its piece's own length.
    assert [
        (row["outcome"], row["audio_seconds"], row["cost"], row["latency_ms"]) for row in rows
    ] == [
        ("ok", 590.0, pytest.approx(590 / 3600 * VOICE_PER_HOUR, abs=1e-12), 1000),
        ("ok", 590.0, pytest.approx(590 / 3600 * VOICE_PER_HOUR, abs=1e-12), 1000),
        ("ok", 70.0, pytest.approx(70 / 3600 * VOICE_PER_HOUR, abs=1e-12), 1000),
    ]
    assert tuple(row["id"] for row in rows) == transcript.model_call_ids
    assert budgets(driver, world)[0]["spent"] == pytest.approx(transcript.cost, abs=1e-12)


def test_transcribe_missing_transcript_raises_invalid_output_with_a_metered_row(
    driver, world, make_layer
):
    audio, _ = wav(3, rate=8000, width=2)
    provider = FakeProvider(reply({"audioDurationMs": 3000}))

    with pytest.raises(InvalidOutput, match="transcript is missing") as raised:
        make_layer(provider).transcribe(world, audio_wav=audio)

    rows = model_calls(driver, world)
    assert rows == [
        call_row(
            world,
            rows[0]["id"],
            job="transcription",
            model=VOICE,
            outcome="invalid_output",
            audio_seconds=3.0,
            cost=3 / 3600 * VOICE_PER_HOUR,
        )
    ]
    assert raised.value.details == {"model_call_id": rows[0]["id"]}


# The daily budget (D15).


def _call(layer: ModelLayer, world_id: str, method: str, job: str):
    if method == "respond":
        return layer.respond(world_id, job=job, input=EVENT)
    if method == "chat":
        return layer.chat(world_id, job=job, messages=JUDGE_MESSAGES)
    return layer.transcribe(world_id, audio_wav=wav(3, rate=8000, width=2)[0], job=job)


def _answer(method: str) -> tuple:
    if method == "respond":
        return reply(spark_doc("noted"))
    if method == "chat":
        return reply(chat_doc("agree"))
    return reply({"transcript": "Lio Varn walked.", "audioDurationMs": 3000})


@pytest.mark.parametrize(
    ("method", "job"),
    [
        ("respond", "verification"),
        ("respond", "consolidation"),
        ("respond", "cleanup_pass"),
        ("respond", "consolidation_pass"),
        ("chat", "second_opinion"),
    ],
)
def test_a_deferrable_job_waits_when_the_budget_is_reached(driver, world, make_layer, method, job):
    set_budget_ceiling(driver, world, DAY, 0.0)
    provider = FakeProvider(_answer(method))

    with pytest.raises(BudgetReached) as raised:
        _call(make_layer(provider), world, method, job)

    error = raised.value
    assert (error.code, error.status, error.retryable) == ("budget_reached", 429, True)
    assert error.details == {
        "job": job,
        "day": "2026-11-02",
        "spent": 0.0,
        "ceiling": 0.0,
        "resets_at": "2026-11-03T00:00:00+00:00",
        # At 09:00, fifteen hours to the reset.
        "retry_after_seconds": 54000,
    }
    assert provider.requests == []
    assert model_calls(driver, world) == []
    assert budgets(driver, world) == [
        {"world_id": world, "day": DAY, "spent": 0.0, "ceiling": 0.0, "state": "reached"}
    ]


@pytest.mark.parametrize(
    ("method", "job"),
    [
        ("respond", "extraction"),
        ("respond", "update_pass"),
        ("chat", "triage"),
        ("transcribe", "transcription"),
    ],
)
def test_a_continuing_job_still_runs_when_the_budget_is_reached(
    driver, world, make_layer, method, job
):
    set_budget_ceiling(driver, world, DAY, 0.0)
    provider = FakeProvider(_answer(method))

    _call(make_layer(provider), world, method, job)

    assert len(provider.requests) == 1
    assert [(row["job"], row["outcome"]) for row in model_calls(driver, world)] == [(job, "ok")]
    assert budgets(driver, world)[0]["state"] == "reached"


def test_a_ceiling_on_another_day_does_not_defer_today(driver, world, make_layer):
    set_budget_ceiling(driver, world, date(2026, 11, 1), 0.0)
    provider = FakeProvider(reply(spark_doc("noted")))

    make_layer(provider).respond(world, job="verification", input=EVENT)

    assert len(provider.requests) == 1
    assert [row["day"] for row in model_calls(driver, world)] == [DAY]


def test_a_zero_daily_budget_defers_with_no_budget_row(driver, world, make_layer):
    provider = FakeProvider(reply(spark_doc("noted")))

    with pytest.raises(BudgetReached):
        make_layer(provider, daily_budget=0.0).respond(world, job="verification", input=EVENT)

    assert provider.requests == []
    assert model_calls(driver, world) == []


def test_the_call_that_reaches_the_ceiling_defers_the_next_one_until_the_reset(
    driver, world, make_layer, test_clock
):
    provider = FakeProvider(reply(spark_doc("noted")))
    layer = make_layer(provider, daily_budget=0.001)

    first = layer.respond(world, job="extraction", input=EVENT)

    assert first.budget == BudgetState(
        spent=pytest.approx(SPARK_COST, abs=1e-12), ceiling=0.001, state="reached"
    )
    with pytest.raises(BudgetReached) as raised:
        layer.respond(world, job="verification", input=EVENT)
    assert raised.value.details["spent"] == pytest.approx(SPARK_COST, abs=1e-12)
    assert raised.value.details["resets_at"] == "2026-11-03T00:00:00+00:00"
    assert len(provider.requests) == 1

    test_clock.set(datetime(2026, 11, 3, 0, 0, tzinfo=UTC))
    layer.respond(world, job="verification", input=EVENT)

    assert len(provider.requests) == 2
    assert [(row["job"], row["day"]) for row in model_calls(driver, world)] == [
        ("extraction", DAY),
        ("verification", NEXT_DAY),
    ]
    assert [(row["day"], row["ceiling"]) for row in budgets(driver, world)] == [
        (DAY, 0.001),
        (NEXT_DAY, 0.001),
    ]


# A contributor model id is never called (decision 12).


def test_a_contributor_model_id_is_never_called(driver, world, make_layer):
    base = load_catalog()
    contributor = "muse-spark-contributor-1.3"
    spec = ModelSpec(contributor, "spark", "meta", base.models[SPARK].efforts, True)
    catalog = Catalog(
        families={**base.families, "spark": contributor},
        models={**base.models, contributor: spec},
        jobs=base.jobs,
        prices={**base.prices, contributor: base.prices[SPARK]},
    )
    provider = FakeProvider(reply(spark_doc("noted")), reply(chat_doc("agree")))
    layer = make_layer(provider, catalog=catalog)

    with pytest.raises(ModelRefused, match="contributor"):
        layer.respond(world, job="extraction", input=EVENT)
    with pytest.raises(ModelRefused, match="contributor"):
        layer.chat(world, job="verification", messages=JUDGE_MESSAGES)

    assert provider.requests == []
    assert model_calls(driver, world) == []
