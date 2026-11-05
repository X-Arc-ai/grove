"""Import profile controls, with invented input and an in-process provider only."""

import asyncio
import base64
import hashlib
import hmac
import json
import threading

import httpx
import pytest
from starlette.requests import Request

from grove.models import gateway as gateway_module
from grove.models.errors import ModelRefused, Unauthenticated
from grove.models.gateway import Gateway
from grove.models.gateway_auth import IMPORT_PROFILE, mint, verify
from tests.models.test_gateway import CATALOG, SECRET, SPARK, SPARK_DOC, settings

WORLD = "t_import_00000000-0000-0000-0000-000000000001"


def token(profile=IMPORT_PROFILE):
    return mint(
        SECRET,
        CATALOG,
        world_id=WORLD,
        run_id="import-limit-test",
        job="update_pass",
        profile=profile,
    )


def request(body=None, *, auth=None):
    raw = json.dumps(
        body
        or {"model": SPARK, "messages": [{"role": "user", "content": "Invented orchard notes"}]}
    ).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"authorization", ("Bearer " + (auth or token())).encode())],
        },
        receive,
    )


def gateway(monkeypatch, provider):
    found = Gateway(
        object(), settings=settings(), catalog=CATALOG, transport=httpx.MockTransport(provider)
    )
    records = []
    monkeypatch.setattr(found, "_admit", lambda claims: None)
    monkeypatch.setattr(found, "_record", lambda *args: records.append(args))
    return found, records


@pytest.mark.parametrize("profile", ["future", "", None, 1, {}])
def test_unknown_signed_profiles_are_rejected(profile):
    original = token()
    _, payload, _ = original.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["profile"] = profile
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(SECRET, ("g1." + payload).encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(Unauthenticated):
        verify(SECRET, CATALOG, f"g1.{payload}.{signature}")


def test_profile_requires_signature_and_update_job():
    assert verify(SECRET, CATALOG, token()).profile == IMPORT_PROFILE
    assert verify(SECRET, CATALOG, token(None)).profile is None
    original = token()
    with pytest.raises(Unauthenticated):
        verify(SECRET, CATALOG, original[:-4] + "fake")
    with pytest.raises(ModelRefused):
        mint(SECRET, CATALOG, world_id=WORLD, run_id="test", job="update_pass", profile="unknown")
    with pytest.raises(ModelRefused):
        mint(SECRET, CATALOG, world_id=WORLD, run_id="test", job="screen", profile=IMPORT_PROFILE)


@pytest.mark.parametrize(
    "body,status",
    [
        ({"messages": [{"role": "user", "content": "🌳" * 33000}]}, 413),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aW52ZW50ZWQ="},
                            }
                        ],
                    }
                ]
            },
            400,
        ),
        (
            {
                "messages": [{"role": "user", "content": "Orchard"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "orchard",
                            "description": "x" * 132000,
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
            413,
        ),
    ],
)
def test_rejected_payload_never_reaches_provider_and_frees_slot(monkeypatch, body, status):
    async def run():
        calls = []

        async def provider(req):
            calls.append(req)
            return httpx.Response(200, json=SPARK_DOC)

        found, records = gateway(monkeypatch, provider)
        try:
            result = await found.chat_completions(request({"model": SPARK, **body}))
            assert result.status_code == status
            assert not calls and not records
            assert (await found.chat_completions(request())).status_code == 200
            assert len(calls) == len(records) == 1
        finally:
            await found.aclose()

    asyncio.run(run())


def test_output_limit_and_absent_profile_compatibility(monkeypatch):
    async def run():
        calls = []

        async def provider(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200, json=SPARK_DOC)

        found, _ = gateway(monkeypatch, provider)
        body = {
            "model": SPARK,
            "messages": [{"role": "user", "content": "Invented orchard"}],
            "max_tokens": 20000,
        }
        try:
            assert (await found.chat_completions(request(body))).status_code == 200
            assert calls[-1]["max_tokens"] == 8192
            assert (
                await found.chat_completions(request(body, auth=token(None)))
            ).status_code == 200
            assert calls[-1]["max_tokens"] > 8192
        finally:
            await found.aclose()

    asyncio.run(run())


def test_cancelled_client_holds_slot_until_provider_and_meter_finish(monkeypatch):
    async def run():
        entered, finish_provider = asyncio.Event(), asyncio.Event()
        meter_entered, finish_meter = threading.Event(), threading.Event()
        calls = []

        async def provider(req):
            calls.append(req)
            entered.set()
            await finish_provider.wait()
            return httpx.Response(200, json=SPARK_DOC)

        found, _ = gateway(monkeypatch, provider)

        def record(*args):
            meter_entered.set()
            assert finish_meter.wait(5)

        monkeypatch.setattr(found, "_record", record)
        first = asyncio.create_task(found.chat_completions(request()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await found.chat_completions(request())).status_code == 429
            assert len(calls) == 1
            finish_provider.set()
            assert await asyncio.to_thread(meter_entered.wait, 2)
            assert (await found.chat_completions(request())).status_code == 429
            finish_meter.set()
            await found.settle(timeout=2)
            assert (await found.chat_completions(request())).status_code == 200
        finally:
            finish_provider.set()
            finish_meter.set()
            await found.aclose()

    asyncio.run(run())


def test_stream_disconnect_drains_before_releasing_import_slot(monkeypatch):
    async def run():
        drained = asyncio.Event()
        finish = asyncio.Event()
        meter_entered, finish_meter = threading.Event(), threading.Event()
        calls = []

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"Pear"}}]}\n\n'
                await finish.wait()
                yield (
                    b'data: {"choices":[],"usage":{"prompt_tokens":12,'
                    b'"completion_tokens":3,"total_tokens":15}}\n\n'
                )
                drained.set()
                yield b"data: [DONE]\n\n"

        async def provider(req):
            calls.append(req)
            if len(calls) == 1:
                return httpx.Response(200, stream=Chunks())
            return httpx.Response(200, json=SPARK_DOC)

        found, records = gateway(monkeypatch, provider)
        original_record = found._record

        def record(*args):
            meter_entered.set()
            assert finish_meter.wait(5)
            original_record(*args)

        monkeypatch.setattr(found, "_record", record)
        try:
            result = await found.chat_completions(
                request(
                    {
                        "model": SPARK,
                        "messages": [{"role": "user", "content": "Invented"}],
                        "stream": True,
                    }
                )
            )
            await anext(result.body_iterator)
            await result.body_iterator.aclose()
            assert (await found.chat_completions(request())).status_code == 429
            assert not records
            finish.set()
            await asyncio.wait_for(drained.wait(), 2)
            assert await asyncio.to_thread(meter_entered.wait, 2)
            assert (await found.chat_completions(request())).status_code == 429
            finish_meter.set()
            await found.settle(timeout=2)
            assert records[0][3].input_tokens == 12
            assert records[0][4] == "client_closed"
            assert (await found.chat_completions(request())).status_code == 200
        finally:
            finish.set()
            finish_meter.set()
            await found.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("where", ["admit", "provider", "meter"])
def test_exception_paths_release_import_slot(monkeypatch, where):
    async def run():
        async def provider(req):
            return httpx.Response(200, json=SPARK_DOC)

        found, _ = gateway(monkeypatch, provider)
        original = {"admit": found._admit, "provider": found.client.send, "meter": found._record}[
            where
        ]

        def fail(*args, **kwargs):
            raise RuntimeError("invented failure")

        async def fail_async(*args, **kwargs):
            raise RuntimeError("invented failure")

        target, attr = (
            (found.client, "send")
            if where == "provider"
            else (found, "_admit" if where == "admit" else "_record")
        )
        monkeypatch.setattr(target, attr, fail_async if where == "provider" else fail)
        try:
            if where == "admit":
                assert (await found.chat_completions(request())).status_code == 503
            else:
                with pytest.raises(RuntimeError, match="invented failure"):
                    await found.chat_completions(request())
            monkeypatch.setattr(target, attr, original)
            assert (await found.chat_completions(request())).status_code == 200
        finally:
            if where == "meter":
                with pytest.raises(gateway_module.DrainIncomplete, match="accounting_failed"):
                    await found.aclose()
            else:
                await found.aclose()

    asyncio.run(run())


def test_close_waits_for_late_created_stream_pump(monkeypatch):
    """Original control: the first settle snapshot contains only the dispatch."""

    async def run():
        provider_entered, provider_finish = asyncio.Event(), asyncio.Event()
        pump_entered, pump_finish = asyncio.Event(), asyncio.Event()
        close_entered = asyncio.Event()

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                pump_entered.set()
                yield b'data: {"choices":[{"delta":{"content":"Pear"}}]}\n\n'
                await pump_finish.wait()
                yield b"data: [DONE]\n\n"

        async def provider(req):
            provider_entered.set()
            await provider_finish.wait()
            return httpx.Response(200, stream=Chunks())

        found, records = gateway(monkeypatch, provider)
        settle = found.settle

        async def observed_settle(timeout=None):
            close_entered.set()
            await settle(timeout)

        monkeypatch.setattr(found, "settle", observed_settle)
        admitted = asyncio.create_task(
            found.chat_completions(
                request(
                    {
                        "model": SPARK,
                        "messages": [{"role": "user", "content": "Invented"}],
                        "stream": True,
                    }
                )
            )
        )
        closer = None
        try:
            await asyncio.wait_for(provider_entered.wait(), 2)
            closer = asyncio.create_task(found.aclose())
            await asyncio.wait_for(close_entered.wait(), 2)
            provider_finish.set()
            await asyncio.wait_for(pump_entered.wait(), 2)
            await admitted
            done, _ = await asyncio.wait({closer}, timeout=0.02)
            assert not done, "shutdown returned before the late stream pump finished"
            assert not records and found._import_slot.locked()
            pump_finish.set()
            await asyncio.wait_for(closer, 2)
            assert len(records) == 1 and not found._import_slot.locked()
            assert found.client.is_closed
        finally:
            provider_finish.set()
            pump_finish.set()
            await asyncio.gather(admitted, return_exceptions=True)
            if closer is not None:
                await asyncio.gather(closer, return_exceptions=True)
            await found.settle(timeout=2)
            await found.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("profile", [IMPORT_PROFILE, None])
def test_close_waits_for_nonstream_meter_and_refuses_new_admission(monkeypatch, profile):
    async def run():
        entered, finish = threading.Event(), threading.Event()
        calls = []

        async def provider(req):
            calls.append(req)
            return httpx.Response(200, json=SPARK_DOC)

        found, records = gateway(monkeypatch, provider)
        original = found._record

        def record(*args):
            entered.set()
            assert finish.wait(5)
            original(*args)

        monkeypatch.setattr(found, "_record", record)
        admitted = asyncio.create_task(found.chat_completions(request(auth=token(profile))))
        closer = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            closer = asyncio.create_task(found.aclose())
            done, _ = await asyncio.wait({closer}, timeout=0.02)
            assert not done
            assert (await found.chat_completions(request())).status_code == 503
            assert (await found.chat_completions(request(auth=token(None)))).status_code == 503
            assert len(calls) == 1 and not records
            if profile == IMPORT_PROFILE:
                assert found._import_slot.locked()
            second_close = asyncio.create_task(found.aclose())
            await asyncio.sleep(0)
            second_close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_close
            assert not closer.done()
            finish.set()
            await asyncio.wait_for(closer, 2)
            assert len(records) == 1 and found.client.is_closed
            assert (await admitted).status_code == 200
            await found.aclose()
        finally:
            finish.set()
            await asyncio.gather(admitted, return_exceptions=True)
            if closer is not None:
                await asyncio.gather(closer, return_exceptions=True)
            await found.aclose()

    asyncio.run(run())


def test_close_deadline_waits_for_cancelled_pumps_late_meter_finalizer(monkeypatch):
    async def run():
        entered, finish = threading.Event(), threading.Event()
        pump_entered = asyncio.Event()
        never = asyncio.Event()

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                pump_entered.set()
                yield b'data: {"choices":[{"delta":{"content":"Pear"}}]}\n\n'
                await never.wait()

        async def provider(req):
            return httpx.Response(200, stream=Chunks())

        found, records = gateway(monkeypatch, provider)
        original = found._record

        def record(*args):
            entered.set()
            assert finish.wait(5)
            original(*args)

        monkeypatch.setattr(found, "_record", record)
        closer = None
        try:
            await found.chat_completions(
                request(
                    {
                        "model": SPARK,
                        "messages": [{"role": "user", "content": "Invented"}],
                        "stream": True,
                    }
                )
            )
            await asyncio.wait_for(pump_entered.wait(), 2)
            closer = asyncio.create_task(found.aclose(drain_timeout=0, finalizer_timeout=2))
            assert await asyncio.to_thread(entered.wait, 2)
            assert not closer.done() and found._import_slot.locked()
            assert not records and not found.client.is_closed
            finish.set()
            with pytest.raises(gateway_module.DrainIncomplete, match="deadline"):
                await asyncio.wait_for(closer, 2)
            assert len(records) == 1 and not found._import_slot.locked()
            assert records[0][4] == "provider_error"
            assert found.client.is_closed
            with pytest.raises(gateway_module.DrainIncomplete, match="deadline"):
                await found.aclose()
        finally:
            finish.set()
            never.set()
            if closer is not None:
                await asyncio.gather(closer, return_exceptions=True)

    asyncio.run(run())


def test_incomplete_meter_deadline_retains_slot_until_late_cleanup(monkeypatch):
    async def run():
        entered, finish = threading.Event(), threading.Event()

        async def provider(req):
            return httpx.Response(200, json=SPARK_DOC)

        found, records = gateway(monkeypatch, provider)
        original = found._record

        def record(*args):
            entered.set()
            assert finish.wait(5)
            original(*args)

        monkeypatch.setattr(found, "_record", record)
        admitted = asyncio.create_task(found.chat_completions(request()))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            with pytest.raises(gateway_module.DrainIncomplete, match="deadline"):
                await found.aclose(drain_timeout=0, finalizer_timeout=0)
            assert not records and found._import_slot.locked()
            assert not found.client.is_closed
            assert (await found.chat_completions(request())).status_code == 503
            finish.set()
            await asyncio.gather(admitted, return_exceptions=True)
            await asyncio.wait_for(found._late_close, 2)
            assert len(records) == 1 and not found._import_slot.locked()
            assert found.client.is_closed
            with pytest.raises(gateway_module.DrainIncomplete, match="deadline"):
                await found.aclose()
        finally:
            finish.set()
            await asyncio.gather(admitted, return_exceptions=True)
            if found._late_close is not None:
                await asyncio.wait_for(found._late_close, 2)

    asyncio.run(run())


def test_close_cancellation_before_provider_headers_records_estimated_usage(monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def provider(req):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=SPARK_DOC)

        found, records = gateway(monkeypatch, provider)
        admitted = asyncio.create_task(found.chat_completions(request()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(gateway_module.DrainIncomplete, match="deadline"):
                await found.aclose(drain_timeout=0, finalizer_timeout=2)
            assert len(records) == 1
            assert records[0][4] == "provider_error" and records[0][3].estimated
            assert records[0][3].input_tokens > 0
            assert not found._import_slot.locked() and found.client.is_closed
        finally:
            release.set()
            await asyncio.gather(admitted, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["deadline", "error"])
def test_client_close_failure_never_reports_complete(monkeypatch, failure):
    async def run():
        release = asyncio.Event()

        async def provider(req):
            return httpx.Response(200, json=SPARK_DOC)

        found, _ = gateway(monkeypatch, provider)
        original = found.client.aclose
        calls = []

        async def close():
            calls.append(True)
            if failure == "error":
                raise RuntimeError("invented close failure")
            await release.wait()
            await original()

        monkeypatch.setattr(found.client, "aclose", close)
        reason = "client_close_deadline" if failure == "deadline" else "client_close_failed"
        try:
            with pytest.raises(gateway_module.DrainIncomplete, match=reason):
                await found.aclose(finalizer_timeout=0.02)
            assert (await found.chat_completions(request())).status_code == 503
            release.set()
            await asyncio.gather(found._late_close, return_exceptions=True)
            with pytest.raises(gateway_module.DrainIncomplete, match=reason):
                await found.aclose()
            assert calls == [True]
        finally:
            release.set()
            if found._late_close is not None:
                await asyncio.gather(found._late_close, return_exceptions=True)
            await original()

    asyncio.run(run())
