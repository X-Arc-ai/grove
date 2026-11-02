"""Regression tests for the defects the slice's own review found, each red before its fix.

- A Glimmer call's GPU seconds counted the 503 waits of a cold start (layer and gateway).
- A provider redirect left the gateway unmetered, and the layer read it as an answer.
- A price row with a missing rate loaded, and metered that part of a call as free.
- A bad GROVE_GATEWAY_PORT raised a bare ValueError instead of NotConfigured.
- An effort that is not text raised TypeError in the writer instead of ModelCallRefused.
"""

import copy
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.clock import TestClock
from grove.models import config
from grove.models.config import ModelSettings, load_catalog, load_model_settings
from grove.models.errors import NotConfigured, ProviderError
from grove.models.gateway import Gateway, create_app
from grove.models.gateway_auth import mint
from grove.models.layer import ModelLayer
from grove.store.query import WorldQuery
from grove.writer.model_calls import ModelCall, ModelCallRefused, record_model_call

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"s" * 32
CATALOG = load_catalog()
GPU_PRICE = CATALOG.prices["muse-glimmer-30b"].per_second
MESSAGES = [{"role": "user", "content": "Do these claims about the orchard agree? Yes or no."}]
GLIMMER_DOC = {
    "choices": [{"message": {"role": "assistant", "content": "yes"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 30, "completion_tokens": 12},
}
_ROWS = (
    "MATCH (c:ModelCall) RETURN c.outcome AS outcome, c.cost AS cost, c.latency_ms AS latency_ms "
    "ORDER BY c.at, c.id"
)


def settings() -> ModelSettings:
    return ModelSettings(
        meta_key="test-meta-key",
        glimmer_base_url="https://glimmer.example.test/v1",
        glimmer_key="test-glimmer-key",
        daily_budget=5.0,
        gateway_secret=SECRET,
    )


@pytest.fixture
def test_clock():
    frozen = TestClock()
    frozen.set(NOW)
    with clock.using(frozen):
        yield frozen


def rows(driver, world) -> list[dict]:
    return WorldQuery(driver, world).read(_ROWS)


class ColdStart:
    """Two 503s, the sleeps between them moving the clock, then an answer that takes 2.5 s."""

    def __init__(self, test_clock: TestClock) -> None:
        self.clock = test_clock
        self.calls = 0

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)

    async def asleep(self, seconds: float) -> None:
        self.clock.advance(seconds=seconds)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= 2:
            return httpx.Response(503, json={"error": {"message": "no container yet"}})
        self.clock.advance(seconds=2.5)
        return httpx.Response(200, json=GLIMMER_DOC)


def test_layer_glimmer_gpu_seconds_leave_out_the_cold_start(driver, world, test_clock):
    cold = ColdStart(test_clock)
    with ModelLayer(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(cold),
        sleep=cold.sleep,
    ) as layer:
        result = layer.chat(world, job="second_opinion", messages=MESSAGES)

    # The back-off waited 2 s and 4 s; the GPU served the answer for 2.5 s.
    assert result.cost == pytest.approx(2.5 * GPU_PRICE, abs=1e-12)
    [row] = rows(driver, world)
    assert row == {
        "outcome": "ok",
        "cost": pytest.approx(2.5 * GPU_PRICE, abs=1e-12),
        "latency_ms": 8500,
    }


def test_gateway_glimmer_gpu_seconds_leave_out_the_cold_start(driver, world, test_clock):
    cold = ColdStart(test_clock)
    gateway = Gateway(
        driver,
        settings=settings(),
        catalog=CATALOG,
        transport=httpx.MockTransport(cold),
        sleep=cold.asleep,
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="run-cold-1", job="second_opinion")
    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "muse-glimmer-30b", "messages": MESSAGES},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    [row] = rows(driver, world)
    assert row == {
        "outcome": "ok",
        "cost": pytest.approx(2.5 * GPU_PRICE, abs=1e-12),
        "latency_ms": 8500,
    }


def redirect(request: httpx.Request) -> httpx.Response:
    # A body that reads as a good answer, so only the status check can refuse it.
    answer = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "pear"}]}],
        "choices": [{"message": {"role": "assistant", "content": "pear"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "input_tokens": 5,
            "output_tokens": 3,
        },
    }
    return httpx.Response(
        307, json=answer, headers={"location": "https://elsewhere.example.test/v1"}
    )


def test_gateway_meters_and_refuses_a_provider_redirect(driver, world, test_clock):
    gateway = Gateway(
        driver, settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(redirect)
    )
    token = mint(SECRET, CATALOG, world_id=world, run_id="run-redirect-1", job="update_pass")
    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "muse-spark-1.3", "messages": MESSAGES},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


def test_layer_meters_and_refuses_a_provider_redirect(driver, world, test_clock):
    with ModelLayer(
        driver, settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(redirect)
    ) as layer:
        with pytest.raises(ProviderError):
            layer.respond(world, job="extraction", input="Name one synthetic fruit.")
    assert [row["outcome"] for row in rows(driver, world)] == ["provider_error"]


@pytest.mark.parametrize(
    ("model", "rate"),
    [
        ("muse-spark-1.3", "output_per_million"),
        ("muse-spark-1.3", "cached_per_million"),
        ("muse-voice-transcribe-1.0", "per_hour"),
        ("muse-glimmer-30b", "per_second"),
    ],
)
def test_a_price_row_missing_a_rate_is_refused(monkeypatch, model, rate):
    planted = {name: config._read_json(name) for name in ("jobs.json", "prices.json")}
    del planted["prices.json"]["models"][model][rate]
    monkeypatch.setattr(config, "_read_json", lambda name: copy.deepcopy(planted[name]))

    with pytest.raises(ValueError, match=f"price {model}: missing"):
        load_catalog()


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-1"])
def test_a_bad_gateway_port_is_not_configured(value):
    with pytest.raises(NotConfigured, match="GROVE_GATEWAY_PORT"):
        load_model_settings({"GROVE_GATEWAY_PORT": value})


def test_an_effort_that_is_not_text_is_refused(driver, world):
    call = ModelCall(
        job="extraction", model="muse-spark-1.3", route="meta", outcome="ok", at=NOW, effort=["low"]
    )
    with pytest.raises(ModelCallRefused, match="effort"):
        record_model_call(driver, world, call, default_ceiling=5.0)
