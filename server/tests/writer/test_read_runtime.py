"""Bounded I/O doubles exercise cancellation, not Neo4j isolation or CPU preemption."""

import asyncio
import threading
from contextlib import contextmanager

import pytest

from grove.api.errors import ApiError
from grove.api.packet_release import PacketReleaseMiddleware, retain_packet_lease
from grove.reads.runtime import ReadRuntime, deadline, request_budget


class Transaction:
    def __init__(self):
        self.rolled_back = False

    async def rollback(self):
        self.rolled_back = True


class Session:
    def __init__(self, *, stall=False):
        self.stall = stall
        self.cancelled = False
        self.closed = False
        self.tx = Transaction()
        self.options = None

    async def begin_transaction(self, **options):
        self.options = options
        if self.stall:
            await asyncio.Event().wait()
        return self.tx

    async def close(self):
        self.closed = True

    def cancel(self):
        self.cancelled = True


class Driver:
    def __init__(self, session=None):
        self.owned = session or Session()
        self.options = None
        self.closed = False

    def session(self, **options):
        self.options = options
        return self.owned

    async def close(self):
        self.closed = True


def test_one_budget_reaches_acquisition_and_native_transaction():
    async def scenario():
        driver = Driver()
        runtime = ReadRuntime(driver)
        async with request_budget() as original:
            async with request_budget() as nested:
                assert original is nested
                async with runtime.transaction() as tx:
                    assert tx is driver.owned.tx
                    assert driver.options["default_access_mode"] == "READ"
                    assert driver.options["max_transaction_retry_time"] == 0
                    assert 0 < driver.options["connection_acquisition_timeout"] <= 2
                    assert 0 < driver.owned.options["timeout"] <= 2
        assert driver.owned.tx.rolled_back and driver.owned.closed
        assert not driver.owned.cancelled
        with pytest.raises(ApiError):
            original.remaining()
        await runtime.close()
        assert driver.closed

    asyncio.run(scenario())


def test_delayed_connection_cancels_session_and_returns_typed_failure():
    async def scenario():
        driver = Driver(Session(stall=True))
        runtime = ReadRuntime(driver)
        with pytest.raises(ApiError) as caught:
            async with request_budget(seconds=0.02):
                async with runtime.transaction():
                    pytest.fail("the connection never completes")
        assert caught.value.status == 503
        assert caught.value.details == {"rule": "snapshot_busy"}
        assert driver.owned.cancelled
        await runtime.close()

    asyncio.run(scenario())


def test_cancelled_helper_cannot_query_after_request_exit():
    async def scenario():
        runtime = ReadRuntime(Driver())
        entered, stopped = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls, refused = [], []

        class Source:
            async def read(self, cypher, **params):
                calls.append(cypher)
                return []

        def helper(source):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1)
            try:
                source.read("MATCH (n:Item) RETURN n")
            except ApiError:
                refused.append(True)
            finally:
                loop.call_soon_threadsafe(stopped.set)

        async def request():
            async with request_budget():
                await runtime.helper(Source(), helper)

        task = asyncio.create_task(request())
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            await stopped.wait()
            assert calls == [] and refused == [True]
        finally:
            release.set()
            await runtime.close()

    asyncio.run(scenario())


def test_two_running_cpu_callbacks_keep_slots_until_they_really_finish():
    async def scenario():
        runtime = ReadRuntime(Driver())
        release = threading.Event()
        entered = [asyncio.Event(), asyncio.Event()]
        loop = asyncio.get_running_loop()

        def blocked(index):
            loop.call_soon_threadsafe(entered[index].set)
            assert release.wait(1)
            return index

        try:
            async with request_budget():
                jobs = [asyncio.create_task(runtime.worker(blocked, i)) for i in range(2)]
                await asyncio.gather(*(event.wait() for event in entered))
                with pytest.raises(ApiError) as caught:
                    await runtime.worker(lambda: "must not enter")
                assert caught.value.details == {"rule": "snapshot_busy"}
                release.set()
                assert await asyncio.gather(*jobs) == [0, 1]
        finally:
            release.set()
            await runtime.close()

    asyncio.run(scenario())


def test_bridge_uses_owner_loop_and_fully_consumes_a_stalled_result():
    async def scenario():
        runtime = ReadRuntime(Driver())
        loop = asyncio.get_running_loop()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Source:
            async def read(self, cypher, **params):
                assert asyncio.get_running_loop() is loop
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with pytest.raises(ApiError):
            async with request_budget(seconds=0.02):
                await runtime.helper(Source(), lambda source: source.read("read"))
        await entered.wait()
        await cancelled.wait()
        await runtime.close()

    asyncio.run(scenario())


def test_deadline_required_and_cannot_be_extended_by_nested_budget():
    async def scenario():
        with pytest.raises(ApiError):
            deadline()
        with pytest.raises(ApiError):
            async with request_budget(seconds=0.02) as first:
                async with request_budget() as second:
                    assert first is second
                    await asyncio.Event().wait()
        with pytest.raises(ValueError):
            async with request_budget(seconds=3):
                pass

    asyncio.run(scenario())


def test_response_deadline_cancels_stalled_transport_before_lease_release():
    async def scenario():
        events = []

        @contextmanager
        def lease():
            events.append("lease_enter")
            try:
                yield
            finally:
                events.append("lease_exit")

        async def app(scope, receive, send):
            retain_packet_lease(lease())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"private"})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            if message["type"] == "http.response.body":
                assert events == ["lease_enter"]
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append("transport_cancelled")
                pytest.fail("the stalled body must not finish")

        guarded = PacketReleaseMiddleware(app, paths=frozenset({"/v1/recall"}))
        with pytest.raises(ApiError) as caught:
            async with request_budget(seconds=0.02):
                await guarded({"type": "http", "path": "/v1/recall"}, receive, send)
        assert caught.value.details == {"rule": "snapshot_busy"}
        assert events == ["lease_enter", "transport_cancelled", "lease_exit"]

    asyncio.run(scenario())
