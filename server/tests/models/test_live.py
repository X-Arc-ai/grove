"""Live model calls on synthetic text, one per model, with the rows they write. Never in CI.

Run in a lane, from server/:

    GROVE_SLICE_ID=models GROVE_LIVE_EVIDENCE_DIR=<dir> \
        uv run pytest tests/models/test_live.py -m live

The Meta key comes from MODEL_API_KEY, else from ~/.meta/model-api.env. The Glimmer endpoint comes
from GROVE_GLIMMER_BASE_URL and GROVE_GLIMMER_KEY, else from ~/.grove/glimmer.env. A test writes
the model_calls rows it made, and nothing else, to GROVE_LIVE_EVIDENCE_DIR. No key or prompt is
written there.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
from datetime import UTC, date
from pathlib import Path

import httpx
import pytest
import uvicorn

from grove import clock
from grove.models.config import ModelSettings, load_catalog, load_model_settings
from grove.models.errors import BudgetReached
from grove.models.gateway import Gateway, create_app
from grove.models.gateway_auth import mint
from grove.models.layer import ModelLayer

pytestmark = pytest.mark.live

_META_FILE = Path.home() / ".meta" / "model-api.env"
_GLIMMER_FILE = Path.home() / ".grove" / "glimmer.env"
# A stable prefix of about 7,000 tokens: the cache needs a long one to show a hit.
PREFIX = "\n".join(
    f"Rule {i}: the synthetic orchard keeps crate {i} of pears beside row {i % 37} "
    f"and logs it on day {i % 29}."
    for i in range(260)
)
ROW_SCHEMA = {
    "type": "object",
    "properties": {"crate": {"type": "integer"}, "row": {"type": "integer"}},
    "required": ["crate", "row"],
    "additionalProperties": False,
}
SPOKEN = (
    "The synthetic orchard team moved the pear crates to row nine on Tuesday. "
    "The ladders get checked tomorrow morning."
)
BRAIN_STATUS = {
    "type": "function",
    "function": {
        "name": "brain_status",
        "description": "Report the status of the synthetic orchard store.",
        "parameters": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
            "required": [],
        },
    },
}


def _env_file(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name:
                values[name] = value.strip().strip("'\"")
    return values


def live_settings(**overrides) -> ModelSettings:
    env = {**_env_file(_META_FILE), **_env_file(_GLIMMER_FILE), **os.environ}
    base = load_model_settings(env)
    fields = {
        "meta_base_url": base.meta_base_url,
        "meta_key": base.meta_key,
        "glimmer_base_url": base.glimmer_base_url,
        "glimmer_key": base.glimmer_key,
        "daily_budget": 1.0,
        "gateway_secret": secrets.token_bytes(32),
    }
    return ModelSettings(**(fields | overrides))


def _plain(value):
    # Dates and datetimes from the store; read by their method, the lint keeps classes in place.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@pytest.fixture
def evidence(request):
    folder = os.environ.get("GROVE_LIVE_EVIDENCE_DIR")

    def write(rows: list[dict], **facts) -> None:
        if not folder:
            return
        Path(folder).mkdir(parents=True, exist_ok=True)
        doc = {
            "test": request.node.name,
            "rows": [{key: _plain(value) for key, value in row.items()} for row in rows],
            **facts,
        }
        target = Path(folder) / f"{request.node.name}.json"
        target.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")

    return write


def _today(world: str) -> date:
    return clock.now(world).astimezone(UTC).date()


def test_live_spark_structured_cached_and_metered(driver, world, evidence):
    settings = live_settings()
    with ModelLayer(driver, settings=settings) as layer:
        asked = "Which row holds crate 42? Answer with the crate and the row."
        first = layer.respond(
            world, job="extraction", instructions=PREFIX, input=asked, schema=ROW_SCHEMA
        )
        second = layer.respond(
            world, job="extraction", instructions=PREFIX, input=asked, schema=ROW_SCHEMA
        )
        rows = layer.meter.calls(world, _today(world))
    evidence(
        rows,
        answers=[first.data, second.data],
        cached=[first.usage.cached_tokens, second.usage.cached_tokens],
    )
    assert first.data == {"crate": 42, "row": 42 % 37}
    assert second.usage.cached_tokens > 0, "the second call of one prefix must read the cache"
    assert len(rows) == 2
    catalog = load_catalog()
    for row in rows:
        assert row["model"] == "muse-spark-1.3"
        assert row["effort"] == "minimal"
        assert row["outcome"] == "ok"
        assert row["input_tokens"] > 6000 and row["output_tokens"] > 0
        expected = catalog.cost(
            "muse-spark-1.3",
            input_tokens=row["input_tokens"],
            cached_tokens=row["cached_tokens"],
            output_tokens=row["output_tokens"],
        )
        assert row["cost"] == pytest.approx(expected, abs=1e-12) and row["cost"] > 0


def test_live_voice_transcribe_metered(driver, world, evidence, tmp_path):
    say = shutil.which("say")
    if say is None:
        pytest.fail("this live test makes its synthetic speech with macOS `say`")
    wav = tmp_path / "spoken.wav"
    subprocess.run([say, "-o", str(wav), "--data-format=LEI16@16000", SPOKEN], check=True)
    with ModelLayer(driver, settings=live_settings()) as layer:
        transcript = layer.transcribe(world, audio_wav=wav.read_bytes())
        rows = layer.meter.calls(world, _today(world))
    evidence(rows, transcript=transcript.text)
    assert "orchard" in transcript.text.lower() and "ladders" in transcript.text.lower()
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "muse-voice-transcribe-1.0" and row["outcome"] == "ok"
    assert row["audio_seconds"] > 3
    assert row["cost"] == pytest.approx(row["audio_seconds"] / 3600 * 0.18, abs=1e-12)


class _Served:
    """The gateway under uvicorn on a free loopback port, in a thread."""

    def __init__(self, gateway: Gateway) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = uvicorn.Config(
            create_app(gateway), host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        with httpx.Client() as client:
            for _ in range(200):
                try:
                    if client.get(f"http://127.0.0.1:{self.port}/health").status_code == 200:
                        return f"http://127.0.0.1:{self.port}/v1"
                except httpx.TransportError:
                    pass
                threading.Event().wait(0.05)
        raise RuntimeError("the gateway did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _stream_tool_call(lines: list[str]) -> dict:
    call = {"id": None, "name": "", "arguments": ""}
    for line in lines:
        if not line.startswith("data: {"):
            continue
        for choice in json.loads(line[6:]).get("choices", []):
            for part in choice.get("delta", {}).get("tool_calls") or []:
                call["id"] = part.get("id") or call["id"]
                function = part.get("function") or {}
                call["name"] += function.get("name") or ""
                call["arguments"] += function.get("arguments") or ""
    return call


def test_live_gateway_tool_round_trip(driver, world, evidence):
    settings = live_settings()
    catalog = load_catalog()
    gateway = Gateway(driver, settings=settings, catalog=catalog)
    token = mint(
        settings.gateway_secret, catalog, world_id=world, run_id="live-run-1", job="update_pass"
    )
    messages = [
        {"role": "system", "content": "You are a test agent for a synthetic orchard store."},
        {"role": "user", "content": "Call the brain_status tool once, with verbose false."},
    ]
    with _Served(gateway) as base, httpx.Client(timeout=120) as client:
        headers = {"Authorization": f"Bearer {token}"}
        first = {
            "model": "muse-spark-1.3",
            "max_tokens": 8192,
            "messages": messages,
            "tools": [BRAIN_STATUS],
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        with client.stream(
            "POST", f"{base}/chat/completions", json=first, headers=headers
        ) as response:
            assert response.status_code == 200, response.read()
            lines = [line for line in response.iter_lines() if line]
        call = _stream_tool_call(lines)
        assert call["name"] == "brain_status" and call["id"]
        follow = {
            "model": "muse-spark-1.3",
            "max_tokens": 8192,
            "tools": [BRAIN_STATUS],
            "messages": messages
            + [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call["id"], "content": '{"items": 4}'},
            ],
        }
        answer = client.post(f"{base}/chat/completions", json=follow, headers=headers)
        assert answer.status_code == 200, answer.text
        final = answer.json()["choices"][0]["message"]
    rows = gateway.meter.calls(world, _today(world))
    evidence(
        rows,
        tool_call={"name": call["name"], "arguments": call["arguments"]},
        final_answer=final.get("content"),
        stream_tail=lines[-2:],
    )
    assert final.get("content") or final.get("tool_calls")
    assert len(rows) == 2
    for row in rows:
        assert row["job"] == "update_pass" and row["run_id"] == "live-run-1"
        assert row["route"] == "meta" and row["outcome"] == "ok" and row["effort"] == "low"
        assert row["input_tokens"] > 0 and row["output_tokens"] > 0 and row["cost"] > 0


def test_live_ceiling_defers_and_ingest_continues(driver, world, evidence):
    # A ceiling far under one call: the first call reaches it.
    settings = live_settings(daily_budget=1e-7)
    catalog = load_catalog()
    with ModelLayer(driver, settings=settings, catalog=catalog) as layer:
        layer.respond(world, job="extraction", input="Name one synthetic fruit. One word.")
        assert layer.meter.budget(world).state == "reached"
        with pytest.raises(BudgetReached) as refused:
            layer.respond(world, job="verification", input="Is a pluot a fruit? One word.")
        layer.respond(world, job="extraction", input="Name one synthetic tree. One word.")
        rows = layer.meter.calls(world, _today(world))
    gateway = Gateway(driver, settings=settings, catalog=catalog)
    with _Served(gateway) as base, httpx.Client(timeout=60) as client:
        token = mint(
            settings.gateway_secret,
            catalog,
            world_id=world,
            run_id="live-run-2",
            job="cleanup_pass",
        )
        blocked = client.post(
            f"{base}/chat/completions",
            json={"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    after = gateway.meter.calls(world, _today(world))
    evidence(
        rows,
        refused=refused.value.as_body(),
        gateway_status=blocked.status_code,
        gateway_body=blocked.json(),
        budget=layer.meter.budget(world).__dict__,
    )
    assert [row["job"] for row in rows] == ["extraction", "extraction"]
    assert refused.value.details["job"] == "verification"
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "budget_reached"
    assert len(after) == 2, "a refused call never reaches the provider and writes no row"


def _glimmer_settings() -> ModelSettings:
    settings = live_settings()
    if not (settings.glimmer_base_url and settings.glimmer_key):
        pytest.fail(
            "BLOCKED: the Glimmer endpoint is not deployed: GROVE_GLIMMER_BASE_URL and "
            "GROVE_GLIMMER_KEY are unset"
        )
    return settings


def test_live_glimmer_endpoint_answers(driver, world, evidence):
    settings = _glimmer_settings()
    with ModelLayer(driver, settings=settings) as layer:
        result = layer.chat(
            world,
            job="second_opinion",
            messages=[
                {
                    "role": "user",
                    "content": "Do these two synthetic claims agree? A: the pears "
                    "sit in row nine. B: row nine holds the pears. Answer yes or no.",
                }
            ],
        )
        rows = layer.meter.calls(world, _today(world))
    evidence(rows, answer=result.text)
    assert result.text.strip()
    assert rows and rows[-1]["route"] == "modal" and rows[-1]["outcome"] == "ok"
    assert rows[-1]["cost"] > 0


def test_live_glimmer_effort_changes_the_reasoning(driver, world, evidence):
    # vLLM drops a wrong effort field without an error, so only the tokens can show the right one
    # took effect: the same question at high spends more of them than at low.
    settings = _glimmer_settings()
    asked = [
        {
            "role": "user",
            "content": "The synthetic orchard has 17 rows of 23 crates, and 40 crates moved out. "
            "How many crates are left? Answer with the number only.",
        }
    ]
    with ModelLayer(driver, settings=settings) as layer:
        low = [
            layer.chat(world, job="second_opinion", effort="low", messages=asked) for _ in range(2)
        ]
        high = [
            layer.chat(world, job="second_opinion", effort="high", messages=asked) for _ in range(2)
        ]
        rows = layer.meter.calls(world, _today(world))
    low_tokens = [result.usage.output_tokens for result in low]
    high_tokens = [result.usage.output_tokens for result in high]
    evidence(
        rows,
        low_output_tokens=low_tokens,
        high_output_tokens=high_tokens,
        answers=[result.text for result in (*low, *high)],
    )
    assert all(row["route"] == "modal" and row["outcome"] == "ok" for row in rows)
    assert [row["effort"] for row in rows] == ["low", "low", "high", "high"]
    assert sum(high_tokens) > sum(low_tokens)


def test_live_glimmer_tool_round_trip_through_the_gateway(driver, world, evidence):
    settings = _glimmer_settings()
    catalog = load_catalog()
    gateway = Gateway(driver, settings=settings, catalog=catalog)
    token = mint(
        settings.gateway_secret, catalog, world_id=world, run_id="live-run-3", job="second_opinion"
    )
    messages = [
        {"role": "system", "content": "You are a test agent for a synthetic orchard store."},
        {"role": "user", "content": "Call the brain_status tool once, with verbose false."},
    ]
    with _Served(gateway) as base, httpx.Client(timeout=300) as client:
        headers = {"Authorization": f"Bearer {token}"}
        first = {
            "model": "muse-glimmer-30b",
            "max_tokens": 8192,
            "messages": messages,
            "tools": [BRAIN_STATUS],
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        with client.stream(
            "POST", f"{base}/chat/completions", json=first, headers=headers
        ) as response:
            assert response.status_code == 200, response.read()
            lines = [line for line in response.iter_lines() if line]
        call = _stream_tool_call(lines)
        assert call["name"] == "brain_status" and call["id"], lines[-3:]
        follow = {
            "model": "muse-glimmer-30b",
            "max_tokens": 8192,
            "tools": [BRAIN_STATUS],
            "messages": messages
            + [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call["id"], "content": '{"items": 4}'},
            ],
        }
        answer = client.post(f"{base}/chat/completions", json=follow, headers=headers)
        assert answer.status_code == 200, answer.text
        final = answer.json()["choices"][0]["message"]
    rows = gateway.meter.calls(world, _today(world))
    evidence(
        rows,
        tool_call={"name": call["name"], "arguments": call["arguments"]},
        final_answer=final.get("content"),
    )
    assert final.get("content") or final.get("tool_calls")
    assert len(rows) == 2
    for row in rows:
        assert row["job"] == "second_opinion" and row["run_id"] == "live-run-3"
        assert row["route"] == "modal" and row["outcome"] == "ok" and row["cost"] > 0
