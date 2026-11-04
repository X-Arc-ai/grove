"""Activated ASGI response bounds, with lease doubles and the actual AnyIO bridge."""

import asyncio
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

import anyio
import httpx
import pytest
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.errors import ApiError, unavailable
from grove.api.packet_release import (
    PacketReleaseMiddleware,
    activate_packet_response,
    require_packet_response,
    retain_packet_lease,
)
from grove.reads.runtime import ReadRuntime, deadline, request_budget, response_budget

SCOPE = {"type": "http", "path": "/mcp", "method": "POST", "headers": []}


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def body(send, value=b"{}"):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": value, "more_body": False})


@contextmanager
def lease(events):
    events.append("enter")
    try:
        yield "lease"
    finally:
        events.append("exit")


def guarded(app, **options):
    return PacketReleaseMiddleware(
        app, paths=frozenset({"/mcp", "/mcp/"}), require_activation=True, **options
    )


def test_explicit_activation_is_required_before_any_private_operation():
    async def scenario():
        events, output = [], []
        with pytest.raises(ApiError):
            require_packet_response()
        with pytest.raises(ApiError):
            await activate_packet_response()

        async def app(scope, receive, send):
            with pytest.raises(ApiError):
                require_packet_response()
            with pytest.raises(ApiError):
                deadline()
            with pytest.raises(ApiError):
                async with request_budget():
                    pytest.fail("a helper cannot activate the response")
            with pytest.raises(ApiError):
                retain_packet_lease(lease(events))
            await body(send)

        async def send(message):
            output.append(message)

        await guarded(app)(SCOPE, receive, send)
        assert events == [] and output[0]["status"] == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_unactivated_requests_keep_duration_size_and_streaming_behavior(path):
    async def scenario():
        output = []
        fake = clock.TestClock()
        fake.set(datetime(2026, 11, 4, tzinfo=UTC))

        async def app(scope, receive, send):
            fake.advance(seconds=30)
            await asyncio.sleep(0)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            assert len(output) == 1
            await send({"type": "http.response.body", "body": b"a" * 100, "more_body": True})
            assert len(output) == 2
            await send({"type": "http.response.body", "body": b"b" * 100, "more_body": False})

        async def send(message):
            output.append(message)

        with clock.using(fake):
            await guarded(app, max_body_bytes=10)({**SCOPE, "path": path}, receive, send)
        assert len(output) == 3 and output[-2]["more_body"] is True
        assert output[-1]["body"] == b"b" * 100

    asyncio.run(scenario())


def test_activation_after_passthrough_start_refuses_without_replacing_the_response():
    async def scenario():
        output = []

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await activate_packet_response()

        async def send(message):
            output.append(message)

        with pytest.raises(ApiError):
            await guarded(app)(SCOPE, receive, send)
        assert len(output) == 1 and output[0]["status"] == 200

    asyncio.run(scenario())


def test_one_active_deadline_reaches_anyio_worker_returned_coroutine_and_final_send():
    async def scenario():
        events, output, handles = [], [], []

        async def service():
            assert require_packet_response() is handles[0]
            async with request_budget() as nested:
                assert nested is handles[0]
                await asyncio.sleep(0)
            return {"answer": "found"}

        def handler():
            assert require_packet_response() is handles[0]
            assert retain_packet_lease(lease(events)) == "lease"
            result = anyio.from_thread.run(service)
            assert require_packet_response() is handles[0]
            return json.dumps(result).encode()

        async def app(scope, receive, send):
            handles.append(await activate_packet_response())
            result = await run_in_threadpool(handler)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": result[:5], "more_body": True})
            assert output == []
            await send({"type": "http.response.body", "body": result[5:], "more_body": False})

        async def send(message):
            assert require_packet_response() is handles[0]
            assert events == ["enter"]
            output.append(message)

        await guarded(app)(SCOPE, receive, send)
        assert events == ["enter", "exit"]
        assert len(output) == 2 and json.loads(output[-1]["body"]) == {"answer": "found"}
        with pytest.raises(ApiError):
            handles[0].remaining()

    asyncio.run(scenario())


def test_repeated_activation_and_nested_budgets_cannot_extend_the_bound():
    async def scenario():
        handles, output = [], []

        async def app(scope, receive, send):
            first = await activate_packet_response(seconds=0.04)
            handles.append(first)
            original = first.at
            await asyncio.sleep(0)
            second = await activate_packet_response()
            assert second is first and second.at == original
            async with request_budget() as third:
                assert third is first
            await asyncio.Event().wait()

        async def send(message):
            output.append(message)

        await guarded(app)(SCOPE, receive, send)
        assert output[0]["status"] == 503
        assert json.loads(output[-1]["body"])["error"]["details"] == {"rule": "snapshot_busy"}
        assert handles[0].cancelled.is_set()

    asyncio.run(scenario())


def test_later_activation_can_shorten_a_captured_handle():
    async def scenario():
        output = []

        async def app(scope, receive, send):
            first = await activate_packet_response()
            original = first.at
            second = await activate_packet_response(seconds=0.02)
            assert first is second and first.at < original
            await asyncio.Event().wait()

        async def send(message):
            output.append(message)

        await guarded(app)(SCOPE, receive, send)
        assert output[0]["status"] == 503

    asyncio.run(scenario())


def test_activation_uses_event_loop_timeout_without_assuming_the_clock_origin():
    async def scenario():
        fake = clock.TestClock()
        fake.set(datetime(2026, 11, 4, tzinfo=UTC))
        output = []

        async def app(scope, receive, send):
            active = await activate_packet_response(seconds=0.05)
            assert active.at == 0.05
            await asyncio.sleep(0)
            await body(send)

        async def send(message):
            output.append(message)

        with clock.using(fake):
            await guarded(app)(SCOPE, receive, send)
        assert output[0]["status"] == 200

    asyncio.run(scenario())


def test_activation_cannot_extend_an_existing_outer_budget():
    async def scenario():
        output = []

        async def app(scope, receive, send):
            active = await activate_packet_response()
            assert active.at <= parent.at
            await body(send)

        async def send(message):
            output.append(message)

        async with request_budget(seconds=0.05) as parent:
            await guarded(app)(SCOPE, receive, send)
            assert parent.remaining() > 0
        assert output[0]["status"] == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [0, -1, 3, True, float("nan"), float("inf")])
def test_activation_rejects_invalid_duration(seconds):
    async def scenario():
        async def app(scope, receive, send):
            with pytest.raises(ValueError):
                await activate_packet_response(seconds=seconds)
            await body(send)

        async def send(message):
            assert message.get("status", 200) == 200

        await guarded(app)(SCOPE, receive, send)

    asyncio.run(scenario())


def test_activated_packet_size_limit_replaces_no_private_prefix():
    async def scenario():
        output, events = [], []

        async def app(scope, receive, send):
            await activate_packet_response()
            retain_packet_lease(lease(events))
            await body(send, b"private" * 10)

        async def send(message):
            assert events == ["enter", "exit"]
            output.append(message)

        await guarded(app, max_body_bytes=10)(SCOPE, receive, send)
        assert output[0]["status"] == 503
        assert b"private" not in output[-1]["body"]
        assert events == ["enter", "exit"]

    asyncio.run(scenario())


@pytest.mark.parametrize("stall_stage", ["start", "body"])
def test_timeout_at_transport_cancels_send_and_never_releases_later_body(stall_stage):
    async def scenario():
        events, output = [], []

        async def app(scope, receive, send):
            await activate_packet_response(seconds=0.02)
            retain_packet_lease(lease(events))
            await body(send, b"private")

        async def send(message):
            assert events == ["enter"]
            if message["type"] == f"http.response.{stall_stage}":
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append("send_cancelled")
            output.append(message)

        with pytest.raises(ApiError):
            await guarded(app)(SCOPE, receive, send)
        assert events == ["enter", "send_cancelled", "exit"]
        assert not any(message["type"] == "http.response.body" for message in output)

    asyncio.run(scenario())


def test_suppressed_send_cancellation_does_not_pass_the_next_handoff_check():
    async def scenario():
        output = []

        async def app(scope, receive, send):
            await activate_packet_response(seconds=0.02)
            await body(send, b"private")

        async def send(message):
            if message["type"] == "http.response.start":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
            output.append(message)

        with pytest.raises(ApiError):
            await guarded(app)(SCOPE, receive, send)
        assert [message["type"] for message in output] == ["http.response.start"]

    asyncio.run(scenario())


def test_external_cancellation_invalidates_late_worker_and_closes_the_retained_lease():
    async def scenario():
        events, output, handles = [], [], []
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def worker():
            require_packet_response()
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            with pytest.raises(ApiError):
                require_packet_response()
            with pytest.raises(ApiError):
                retain_packet_lease(lease(events))
            loop.call_soon_threadsafe(finished.set)

        async def app(scope, receive, send):
            handles.append(await activate_packet_response())
            retain_packet_lease(lease(events))
            await run_in_threadpool(worker)
            await body(send, b"private")

        async def send(message):
            output.append(message)

        task = asyncio.create_task(guarded(app)(SCOPE, receive, send))
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert handles[0].cancelled.is_set()
            release.set()
            await finished.wait()
            assert events == ["enter", "exit"] and output == []
        finally:
            release.set()

    asyncio.run(scenario())


def test_timeout_across_raw_anyio_bridge_invalidates_the_late_service_context():
    async def scenario():
        events, output, refused = [], [], []
        stopped, proceed = asyncio.Event(), asyncio.Event()
        service_tasks = []

        async def service():
            service_tasks.append(asyncio.current_task())
            try:
                await proceed.wait()
                with pytest.raises(ApiError):
                    require_packet_response()
                with pytest.raises(ApiError):
                    deadline()
                refused.append(True)
            finally:
                stopped.set()

        def handler():
            require_packet_response()
            retain_packet_lease(lease(events))
            return anyio.from_thread.run(service)

        async def app(scope, receive, send):
            await activate_packet_response(seconds=0.02)
            await run_in_threadpool(handler)
            await body(send, b"private")

        async def send(message):
            output.append(message)

        try:
            await guarded(app)(SCOPE, receive, send)
            # Raw AnyIO starts another task. The actual MCP binding owns its cancellation;
            # this boundary owns the shared invalidation that also stops late unowned work.
            proceed.set()
            async with asyncio.timeout(0.2):
                await stopped.wait()
        finally:
            for task in service_tasks:
                task.cancel()
            await asyncio.gather(*service_tasks, return_exceptions=True)
        assert output[0]["status"] == 503
        assert b"private" not in output[-1]["body"]
        assert events == ["enter", "exit"]
        assert refused == [True]

    asyncio.run(scenario())


def test_stalled_lease_entry_closes_the_late_manager_after_timeout():
    async def scenario():
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        events, output = [], []

        @contextmanager
        def late_lease():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def handler():
            try:
                with pytest.raises(ApiError):
                    retain_packet_lease(late_lease())
            finally:
                loop.call_soon_threadsafe(finished.set)

        async def app(scope, receive, send):
            await activate_packet_response(seconds=0.04)
            await run_in_threadpool(handler)
            await body(send, b"private")

        async def send(message):
            output.append(message)

        task = asyncio.create_task(guarded(app)(SCOPE, receive, send))
        try:
            await entered.wait()
            await task
            release.set()
            async with asyncio.timeout(0.5):
                await finished.wait()
            assert output[0]["status"] == 503 and b"private" not in output[-1]["body"]
            assert events == ["enter", "exit"]
        finally:
            release.set()

    asyncio.run(scenario())


def test_cancelled_native_worker_retains_its_existing_slot_until_exit():
    async def scenario():
        class Driver:
            async def close(self):
                pass

        runtime = ReadRuntime(Driver())
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        handles, output = [], []

        def worker():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            with pytest.raises(ApiError):
                deadline()
            loop.call_soon_threadsafe(finished.set)

        async def app(scope, receive, send):
            handles.append(await activate_packet_response())
            await runtime.worker(worker)
            await body(send, b"private")

        async def send(message):
            output.append(message)

        task = asyncio.create_task(guarded(app)(SCOPE, receive, send))
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert handles[0].cancelled.is_set()
            assert len(runtime._jobs) == 1
            release.set()
            await finished.wait()
            assert output == []
        finally:
            release.set()
            await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["oversize", "timeout", "api_error"])
@pytest.mark.parametrize("stage", ["start", "body"])
def test_error_delivery_uses_remaining_budget_and_releases_lease(failure, stage):
    async def scenario():
        events, handles, output = [], [], []

        async def app(scope, receive, send):
            handles.append(await activate_packet_response(seconds=0.025))
            retain_packet_lease(lease(events))
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "api_error":
                raise unavailable("synthetic refusal")
            await body(send, b"private" * 4)

        async def send(message):
            assert events == ["enter", "exit"]
            assert handles[0].cancelled.is_set()
            with pytest.raises(ApiError):
                require_packet_response()
            output.append(message)
            if message["type"] == "http.response.start":
                assert message["status"] == 503
            if message["type"] == f"http.response.{stage}":
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append("send_cancelled")

        task = asyncio.create_task(guarded(app, max_body_bytes=8)(SCOPE, receive, send))
        try:
            # Observe component completion without cancelling it through wait_for().
            done, _ = await asyncio.wait({task}, timeout=0.25)
            assert task in done, "error delivery outlived the packet budget"
            with pytest.raises(ApiError) as caught:
                await task
            assert caught.value.details == {"rule": "snapshot_busy"}
            assert handles[0].cancelled.is_set()
            assert events == ["enter", "exit", "send_cancelled"]
            assert not any(b"private" in message.get("body", b"") for message in output)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_reserved_error_delivery_keeps_typed_503_through_the_actual_app_stack(monkeypatch):
    monkeypatch.delenv("GROVE_WEB_DIST", raising=False)

    async def scenario():
        events, handles = [], []
        app = create_app(ApiSettings("http://127.0.0.1"), driver=object())

        @app.post("/budget-probe")
        async def route():
            handles.append(await activate_packet_response(seconds=0.1))
            retain_packet_lease(lease(events))
            await asyncio.Event().wait()

        app.add_middleware(
            PacketReleaseMiddleware, paths=frozenset({"/budget-probe"}), require_activation=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post("/budget-probe")
        assert response.status_code == 503
        assert response.json()["error"]["details"] == {"rule": "snapshot_busy"}
        assert response.headers["cache-control"] == "no-store"
        assert handles[0].cancelled.is_set()
        assert events == ["enter", "exit"]

    asyncio.run(scenario())


@pytest.mark.parametrize(("seconds", "reserve"), [(0.1, 0.01), (2, 0.05)])
def test_error_reserve_and_hard_end_do_not_extend_on_reactivation(seconds, reserve):
    async def scenario():
        async with response_budget() as budget:
            current = budget.activate(seconds=seconds)
            original = current.at
            hard_when, work_when = budget._hard_when, budget._timeout.when()
            assert hard_when - work_when == pytest.approx(reserve, abs=0.001)
            await asyncio.sleep(0)
            assert budget.activate() is current
            assert current.at == original
            assert budget._hard_when <= hard_when
            assert budget._timeout.when() <= work_when
            budget.activate(seconds=0.05)
            assert current.at < original
            assert budget._hard_when < hard_when
            assert budget._timeout.when() < work_when

    asyncio.run(scenario())


def test_consumed_hard_bound_does_not_start_an_error_response():
    async def scenario():
        events, output = [], []
        fake = clock.TestClock()
        fake.set(datetime(2026, 11, 4, tzinfo=UTC))

        async def app(scope, receive, send):
            await activate_packet_response(seconds=0.1)
            retain_packet_lease(lease(events))
            fake.advance(seconds=1)
            raise unavailable("synthetic refusal")

        async def send(message):
            output.append(message)

        with clock.using(fake), pytest.raises(ApiError) as caught:
            await guarded(app)(SCOPE, receive, send)
        assert caught.value.details == {"rule": "snapshot_busy"}
        assert events == ["enter", "exit"]
        assert output == []

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["app", "send"])
@pytest.mark.parametrize("activate", [False, True])
def test_unrelated_timeout_exception_keeps_its_identity(origin, activate):
    async def scenario():
        original = TimeoutError("synthetic unrelated timeout")

        async def app(scope, receive, send):
            if activate:
                await activate_packet_response()
            if origin == "app":
                raise original
            await body(send, b"store-only")

        async def send(message):
            raise original

        with pytest.raises(TimeoutError) as caught:
            await guarded(app)(SCOPE, receive, send)
        assert caught.value is original

    asyncio.run(scenario())
