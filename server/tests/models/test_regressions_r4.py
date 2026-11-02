"""Regression tests for the defects the fresh R4 verifier found, each red before its fix.

- A stream the client closed before the finish chunk was recorded with zero tokens, because Meta
  sends usage only on that chunk: the spend escaped the ceiling and the run cap.
- No test drove the record-on-close through the real ASGI response, where the server cancels.
- The gateway sent every field and remote image URLs on to the provider (an SSRF route).
- An empty completion was recorded as ok by the gateway.
- The layer's lower-effort retry skipped the budget gate.
- A background poll that answered no JSON object escaped metering, and a failed poll left the
  response running at Meta.
- A body that could not be decoded escaped metering.
- A provider's error message could carry a key back to the caller.
- A stream line that is not a JSON object broke the stream's metering.
"""

import asyncio
import gzip
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models import gateway as gateway_module
from grove.models.config import ModelSettings, load_catalog
from grove.models.errors import BudgetReached, ProviderError
from grove.models.gateway import Gateway, create_app, upstream_body
from grove.models.gateway_auth import mint, verify
from grove.models.layer import ModelLayer
from grove.store.query import WorldQuery
from grove.writer.model_calls import set_budget_ceiling

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"s" * 32
META_KEY = "test-meta-key-0123456789abcdef"
CATALOG = load_catalog()
SPARK = CATALOG.model("muse-spark-1.3")
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
USAGE_COST = (904 * 1.25 + 4096 * 0.15 + 80 * 4.25) / 1_000_000


def sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"


ROLE = sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
TEXT = sse({"choices": [{"index": 0, "delta": {"content": "Row nine."}, "finish_reason": None}]})
FINISH = sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": USAGE})
DONE = b"data: [DONE]\n\n"
_ROWS = (
    "MATCH (c:ModelCall) RETURN c.outcome AS outcome, c.input_tokens AS input_tokens, "
    "c.cached_tokens AS cached_tokens, c.output_tokens AS output_tokens, c.cost AS cost "
    "ORDER BY c.at, c.id"
)


def settings(**overrides) -> ModelSettings:
    fields = {
        "meta_key": META_KEY,
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


class Gated(httpx.AsyncByteStream):
    """A provider stream that sends its first chunk, then waits for the gate before the rest."""

    def __init__(self, first: bytes | None, rest: tuple[bytes, ...], *, forever=False) -> None:
        self.first, self.rest, self.forever = first, rest, forever
        self.gate = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        if self.first is not None:
            yield self.first
        await self.gate.wait()
        if self.forever:
            await asyncio.Event().wait()
        for piece in self.rest:
            yield piece

    async def aclose(self) -> None:
        self.closed = True


async def _asgi_disconnect(gateway: Gateway, relay, after_first: bool) -> list[bytes]:
    """Serve the relay as uvicorn does (ASGI spec 2.3), and drop the client."""
    app = StreamingResponse(relay, media_type="text/event-stream")
    sent: list[bytes] = []
    first = asyncio.Event()

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            sent.append(message["body"])
            first.set()

    async def receive() -> dict:
        if after_first:
            await first.wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
    }
    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize("after_first", [True, False], ids=["after-first-chunk", "before-any"])
def test_a_client_that_leaves_through_asgi_is_metered_with_the_real_usage(
    driver, world, test_clock, after_first
):
    gateway = Gateway(driver, settings=settings(), catalog=CATALOG)
    claims = verify(
        SECRET, CATALOG, mint(SECRET, CATALOG, world_id=world, run_id="r1", job="cleanup_pass")
    )
    body = Gated(ROLE, (TEXT, FINISH, DONE))
    response = httpx.Response(200, stream=body)

    async def drive() -> list[bytes]:
        relay = gateway._relay(response, claims, SPARK, "low", clock.monotonic())
        sent = await _asgi_disconnect(gateway, relay, after_first)
        body.gate.set()
        await gateway.aclose()
        return sent

    sent = asyncio.run(drive())
    assert sent == ([ROLE] if after_first else [])
    assert body.closed
    assert rows(driver, world) == [
        {
            "outcome": "client_closed",
            "input_tokens": 5000,
            "cached_tokens": 4096,
            "output_tokens": 80,
            "cost": pytest.approx(USAGE_COST, abs=1e-12),
        }
    ]


def test_a_stream_that_never_finishes_after_the_client_left_is_cut_and_estimated(
    driver, world, test_clock, monkeypatch
):
    monkeypatch.setattr(gateway_module, "DRAIN_SECONDS", 0.05)
    gateway = Gateway(driver, settings=settings(), catalog=CATALOG)
    claims = verify(
        SECRET, CATALOG, mint(SECRET, CATALOG, world_id=world, run_id="r2", job="update_pass")
    )
    body = Gated(TEXT, (), forever=True)
    response = httpx.Response(200, stream=body)
    input_chars = len(json.dumps(MESSAGES)) + 2

    async def drive() -> list[dict]:
        relay = gateway._relay(response, claims, SPARK, "low", clock.monotonic(), None, input_chars)
        await relay.__anext__()
        await relay.aclose()
        body.gate.set()
        # The cap must end the read on its own: this waits far less than the 600 s read timeout,
        # and the row must be there before the gateway closes anything.
        await gateway.settle(timeout=2.0)
        found = rows(driver, world)
        await gateway.aclose()
        return found

    found = asyncio.run(drive())
    assert found, "the drain cap did not end the read"
    estimate_in = math.ceil(input_chars / 4)
    estimate_out = math.ceil(len("Row nine.") / 4)
    assert found == [
        {
            "outcome": "client_closed",
            "input_tokens": estimate_in,
            "cached_tokens": 0,
            "output_tokens": estimate_out,
            "cost": pytest.approx((estimate_in * 1.25 + estimate_out * 4.25) / 1e6, abs=1e-12),
        }
    ]


class Listed(httpx.AsyncByteStream):
    """A provider stream of fixed chunks, read by the gateway as it reads a real one."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for piece in self.chunks:
            yield piece


def event_stream(*chunks: bytes) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Listed(*chunks)
    )


class Upstream:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


async def _no_wait(_: float) -> None:
    return None


def post(driver, world, upstream: Upstream, body: dict, job: str = "update_pass"):
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(upstream),
        sleep=_no_wait,
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="r3", job=job)
    with TestClient(create_app(gateway)) as client:
        return client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {token}"}
        )


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://exfil.example.net/p?d=orchard"}},
        {"type": "image_url", "image_url": "http://169.254.169.254/latest"},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        {"type": "video_url", "video_url": {"url": "https://exfil.example.net/v"}},
    ],
    ids=["https-image", "metadata-image", "audio", "video"],
)
def test_a_part_that_would_make_a_provider_fetch_or_decode_is_refused(
    driver, world, test_clock, part
):
    upstream = Upstream(httpx.Response(200, json={"choices": [], "usage": USAGE}))
    body = {
        "model": "muse-glimmer-30b",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Look."}, part]}],
    }

    response = post(driver, world, upstream, body, job="triage")

    assert response.status_code == 400
    assert "data URLs" in response.json()["error"]["message"]
    assert upstream.requests == []
    assert rows(driver, world) == []


def test_only_known_fields_and_message_keys_reach_the_provider():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    body = {
        "model": "muse-glimmer-30b",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Look."}, image],
                "audio": {"id": "x"},
                "extra": 1,
            },
        ],
        "store": True,
        "ignore_eos": True,
        "min_tokens": 100000,
        "chat_template": "{{ planted }}",
        "metadata": {"run": "x"},
        "temperature": 0.7,
    }
    out = upstream_body(body, spec=CATALOG.model("muse-glimmer-30b"), effort="low", job="triage")

    assert set(out) == {"model", "messages", "temperature", "max_tokens", "chat_template_kwargs"}
    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Look."}, image]}
    ]


def test_an_empty_completion_is_recorded_as_invalid_output(driver, world, test_clock):
    empty = {
        "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": USAGE,
    }
    upstream = Upstream(httpx.Response(200, json=empty))

    response = post(driver, world, upstream, {"model": "muse-spark-1.3", "messages": MESSAGES})

    assert response.status_code == 200 and response.json() == empty
    assert [row["outcome"] for row in rows(driver, world)] == ["invalid_output"]


def test_an_empty_stream_is_recorded_as_invalid_output(driver, world, test_clock):
    upstream = Upstream(event_stream(ROLE, FINISH, DONE))

    response = post(
        driver, world, upstream, {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    )

    assert response.status_code == 200
    assert [row["outcome"] for row in rows(driver, world)] == ["invalid_output"]


def test_a_stream_line_that_is_not_an_object_does_not_break_metering(driver, world, test_clock):
    odd = b"data: [1, 2]\n\n"
    upstream = Upstream(event_stream(ROLE, odd, TEXT, FINISH, DONE))

    response = post(
        driver, world, upstream, {"model": "muse-spark-1.3", "messages": MESSAGES, "stream": True}
    )

    assert response.status_code == 200
    assert response.content == ROLE + odd + TEXT + FINISH + DONE
    [row] = rows(driver, world)
    assert (row["outcome"], row["input_tokens"]) == ("ok", 5000)


def test_a_body_that_cannot_be_decoded_is_metered(driver, world, test_clock):
    # Built as a stream, so the bytes are decoded when the gateway reads them, not here.
    broken = httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Listed(b"not gzip at all")
    )
    response = post(
        driver, world, Upstream(broken), {"model": "muse-spark-1.3", "messages": MESSAGES}
    )

    assert response.status_code == 503
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


def test_the_layer_meters_a_body_that_cannot_be_decoded(driver, world, test_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
        )

    with ModelLayer(
        driver, settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(handler)
    ) as layer:
        with pytest.raises(ProviderError):
            layer.respond(world, job="extraction", input="Name one synthetic fruit.")
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


def test_a_provider_message_never_carries_a_key_back(driver, world, test_clock):
    leaked = {"error": {"message": f"bad key {META_KEY}; sent Bearer {META_KEY}"}}
    response = post(
        driver,
        world,
        Upstream(httpx.Response(401, json=leaked)),
        {"model": "muse-spark-1.3", "messages": MESSAGES},
    )

    assert response.status_code == 401
    assert META_KEY not in response.text
    assert "<redacted>" in response.json()["error"]["message"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=leaked)

    with ModelLayer(
        driver, settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(handler)
    ) as layer:
        with pytest.raises(ProviderError) as refused:
            layer.respond(world, job="extraction", input="Name one synthetic fruit.")
    assert META_KEY not in json.dumps(refused.value.as_body())


def test_a_lower_effort_retry_passes_the_budget_gate_again(driver, world, test_clock):
    # The empty answer at medium reaches the ceiling; second_opinion waits at the ceiling.
    set_budget_ceiling(driver, world, NOW.date(), 1e-9)
    empty = {
        "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10},
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        test_clock.advance(seconds=1)
        return httpx.Response(200, json=empty)

    with ModelLayer(
        driver, settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(handler)
    ) as layer:
        with pytest.raises(BudgetReached):
            layer.chat(world, job="second_opinion", messages=MESSAGES)
    assert len(calls) == 1
    assert [row["outcome"] for row in rows(driver, world)] == ["invalid_output"]


@pytest.mark.parametrize(
    "poll",
    [
        httpx.Response(200, content=b"<html>busy</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(404, json={"error": {"message": "gone"}}),
    ],
    ids=["html", "list", "404"],
)
def test_a_failed_background_poll_is_metered_and_cancelled(driver, world, test_clock, poll):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/responses":
            return httpx.Response(200, json={"id": "resp_orchard", "status": "queued"})
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "resp_orchard", "status": "cancelled"})
        return poll

    with ModelLayer(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ) as layer:
        with pytest.raises(ProviderError):
            layer.respond(
                world, job="extraction", input="Name one synthetic fruit.", background=True
            )
    assert ("POST", "/v1/responses/resp_orchard/cancel") in seen
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


def test_the_glimmer_server_fetches_no_remote_media():
    app_file = Path(__file__).resolve().parents[3] / "deploy" / "modal" / "glimmer.py"
    text = app_file.read_text(encoding="utf-8")
    assert '"--allowed-media-domains",\n    "media.invalid",' in text
    assert '"VLLM_MEDIA_URL_ALLOW_REDIRECTS": "0"' in text


def test_the_glimmer_server_scales_to_zero_and_stays_locked():
    app_file = Path(__file__).resolve().parents[3] / "deploy" / "modal" / "glimmer.py"
    text = app_file.read_text(encoding="utf-8")
    assert "min_containers=0," in text
    assert "max_containers=2," in text
    assert "unauthenticated=False," in text
    assert "scaledown_window=IDLE_SECONDS," in text and "IDLE_SECONDS = 300" in text
    assert """'{"reasoning_strength": "low"}',""" in text


def test_gzip_is_still_read_when_it_is_valid(driver, world, test_clock):
    # The decoding guard must not break a provider that compresses its answers.
    good = {
        "choices": [
            {"message": {"role": "assistant", "content": "Row nine."}, "finish_reason": "stop"}
        ],
        "usage": USAGE,
    }
    packed = httpx.Response(
        200,
        content=gzip.compress(json.dumps(good).encode()),
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )
    response = post(
        driver, world, Upstream(packed), {"model": "muse-spark-1.3", "messages": MESSAGES}
    )

    assert response.status_code == 200 and response.json() == good
    assert [row["outcome"] for row in rows(driver, world)] == ["ok"]
