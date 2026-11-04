"""Response boundary tests use a lease double, not a D14 privacy implementation."""

import asyncio
from contextlib import contextmanager

import pytest
from starlette.concurrency import run_in_threadpool

from grove.api.errors import ApiError
from grove.api.packet_release import PacketReleaseMiddleware, retain_packet_lease

SCOPE = {"type": "http", "path": "/v1/recall", "method": "POST", "headers": []}


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


@contextmanager
def lease(events):
    events.append("enter")
    try:
        yield "test-lease"
    finally:
        events.append("exit")


def test_handler_without_response_guard_cannot_enter_lease():
    events = []
    with pytest.raises(ApiError) as caught:
        retain_packet_lease(lease(events))
    assert caught.value.code == "unavailable"
    assert events == []


def test_threadpool_lease_survives_chunk_buffering_and_delayed_final_send():
    async def scenario():
        events, output = [], []
        releasing, proceed = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            assert await run_in_threadpool(retain_packet_lease, lease(events)) == "test-lease"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"answer":', "more_body": True})
            assert output == []
            await send({"type": "http.response.body", "body": b'"found"}', "more_body": False})

        async def send(message):
            assert events == ["enter"]
            if message["type"] == "http.response.body":
                releasing.set()
                await proceed.wait()
                assert events == ["enter"]
            output.append(message)

        guarded = PacketReleaseMiddleware(app, paths=frozenset({"/v1/recall"}))
        task = asyncio.create_task(guarded(SCOPE, receive, send))
        await releasing.wait()
        assert events == ["enter"]
        proceed.set()
        await task
        assert events == ["enter", "exit"]
        assert len(output) == 2
        assert output[-1]["body"] == b'{"answer":"found"}'
        assert output[-1]["more_body"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "incomplete", "oversize"])
def test_failed_production_never_releases_a_partial_packet(failure):
    async def scenario():
        events, output = [], []

        async def app(scope, receive, send):
            retain_packet_lease(lease(events))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"private", "more_body": True})
            if failure == "exception":
                raise RuntimeError("synthetic")
            if failure == "oversize":
                await send({"type": "http.response.body", "body": b"x" * 100, "more_body": False})

        async def send(message):
            assert events == ["enter"]
            output.append(message)

        guarded = PacketReleaseMiddleware(app, paths=frozenset({"/v1/recall"}), max_body_bytes=50)
        if failure == "exception":
            with pytest.raises(RuntimeError):
                await guarded(SCOPE, receive, send)
            assert output == []
        else:
            await guarded(SCOPE, receive, send)
            assert output[0]["status"] == 503
            assert b"private" not in output[-1]["body"]
            assert b'"code":"unavailable"' in output[-1]["body"]
        assert events == ["enter", "exit"]

    asyncio.run(scenario())


def test_cancelled_final_send_releases_the_lease():
    async def scenario():
        events = []
        releasing = asyncio.Event()

        async def app(scope, receive, send):
            retain_packet_lease(lease(events))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def send(message):
            if message["type"] == "http.response.body":
                releasing.set()
                await asyncio.Event().wait()

        guarded = PacketReleaseMiddleware(app, paths=frozenset({"/v1/recall"}))
        task = asyncio.create_task(guarded(SCOPE, receive, send))
        await releasing.wait()
        assert events == ["enter"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["enter", "exit"]

    asyncio.run(scenario())


def test_mcp_batch_leases_survive_serialization_and_close_in_reverse_order():
    async def scenario():
        events = []

        @contextmanager
        def member(number):
            events.append(f"enter-{number}")
            try:
                yield
            finally:
                events.append(f"exit-{number}")

        async def app(scope, receive, send):
            await run_in_threadpool(retain_packet_lease, member(1))
            await run_in_threadpool(retain_packet_lease, member(2))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"[{},{ }]"})

        async def send(message):
            assert events == ["enter-1", "enter-2"]

        guarded = PacketReleaseMiddleware(app, paths=frozenset({"/mcp"}))
        await guarded({**SCOPE, "path": "/mcp"}, receive, send)
        assert events == ["enter-1", "enter-2", "exit-2", "exit-1"]

    asyncio.run(scenario())


def test_late_thread_context_cannot_retain_a_lease_after_response_completion():
    async def scenario():
        events = []
        proceed = asyncio.Event()
        late = None

        async def retained_context():
            await proceed.wait()
            with pytest.raises(ApiError):
                retain_packet_lease(lease(events))

        async def app(scope, receive, send):
            nonlocal late
            late = asyncio.create_task(retained_context())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def send(message):
            pass

        await PacketReleaseMiddleware(app, paths=frozenset({"/v1/recall"}))(SCOPE, receive, send)
        proceed.set()
        await late
        assert events == ["enter", "exit"]

    asyncio.run(scenario())
