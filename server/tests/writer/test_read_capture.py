import asyncio
from datetime import date

import pytest

from grove.api.errors import ApiError
from grove.reads.capture import capture
from grove.reads.runtime import ReadRuntime, request_budget
from grove.store.errors import ScopeError, WorldLeak
from tests.writer.test_read_runtime import Driver, Session

WORLD = "10000000-0000-7000-8000-000000000001"
OTHER = "10000000-0000-7000-8000-000000000002"


class Result:
    def __init__(self, rows, *, stall=False):
        self.rows = rows
        self.stall = stall
        self.consumed = False

    def __aiter__(self):
        async def rows():
            if self.stall:
                await asyncio.Event().wait()
            for row in self.rows:
                yield row

        return rows()

    async def consume(self):
        self.consumed = True


class Tx:
    def __init__(self, markers, *, row_world=WORLD, stall=False):
        self.markers = iter(markers)
        self.row_world, self.stall = row_world, stall
        self.queries, self.results = [], []
        self.rolled_back = False

    async def run(self, cypher, params):
        self.queries.append((cypher, params))
        if "w.read_generation AS generation" in cypher:
            result = Result([{"id": WORLD, "generation": next(self.markers), "sequence": 9}])
        else:
            result = Result([{"row": {"world_id": self.row_world, "id": "one"}}], stall=self.stall)
        self.results.append(result)
        return result

    async def rollback(self):
        self.rolled_back = True


class CaptureDriver(Driver):
    def __init__(self, transactions):
        super().__init__()
        self.transactions = iter(transactions)
        self.sessions = []

    def session(self, **options):
        session = Session()
        session.tx = next(self.transactions)
        self.sessions.append(session)
        return session


async def rows(source, now):
    return {"items": await source.read("MATCH (n:Item) RETURN n AS row"), "time": now}


def test_changed_marker_discards_entire_attempt_then_detaches_complete_result():
    async def scenario():
        transactions = [Tx([1, 2]), Tx([2, 2])]
        driver = CaptureDriver(transactions)
        runtime = ReadRuntime(driver)
        async with request_budget():
            result = await capture(runtime, WORLD, rows)
        assert result.commit == 2 and result.history_sequence == 9
        assert result.capture_id and result.reflected_at.utcoffset().total_seconds() == 0
        changed = result.unpack()
        changed["items"].clear()
        assert len(result.unpack()["items"]) == 1
        assert len(driver.sessions) == 2
        for tx in transactions:
            assert tx.rolled_back and all(result.consumed for result in tx.results)
            assert all(params["world_id"] == WORLD for _, params in tx.queries)
            assert len(tx.queries) == 3
        await runtime.close()

    asyncio.run(scenario())


def test_three_inconsistent_attempts_refuse_without_empty_answer():
    async def scenario():
        driver = CaptureDriver([Tx([n, n + 1]) for n in range(3)])
        runtime = ReadRuntime(driver)
        with pytest.raises(ApiError) as caught:
            async with request_budget():
                await capture(runtime, WORLD, rows)
        assert caught.value.status == 503
        assert caught.value.details == {"rule": "snapshot_busy"}
        assert len(driver.sessions) == 3
        await runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("marker", [None, True, -1, "1", 1.0])
def test_invalid_metadata_never_repairs_or_reads_packet_rows(marker):
    async def scenario():
        tx = Tx([marker])
        runtime = ReadRuntime(CaptureDriver([tx]))
        with pytest.raises(ApiError):
            async with request_budget():
                await capture(runtime, WORLD, rows)
        assert len(tx.queries) == 1
        assert "SET" not in tx.queries[0][0]
        await runtime.close()

    asyncio.run(scenario())


def test_packet_source_refuses_platform_queries_and_caller_world_before_driver_call():
    async def scenario():
        tx = Tx([1, 1])
        runtime = ReadRuntime(CaptureDriver([tx]))

        async def check(source, now):
            # scope_cypher enforces world scope; the existing writer lint enforces read-only
            # application queries. This API never accepts caller-supplied Cypher.
            with pytest.raises(ScopeError):
                await source.read("MATCH (w:World) RETURN w")
            with pytest.raises(ScopeError):
                await source.read("MATCH (n:Item) RETURN n", world_id=OTHER)
            return {"safe": True}

        async with request_budget():
            result = await capture(runtime, WORLD, check)
        assert result.unpack() == {"safe": True}
        assert len(tx.queries) == 2
        await runtime.close()

    asyncio.run(scenario())


def test_foreign_row_aborts_capture_and_cancels_session():
    async def scenario():
        driver = CaptureDriver([Tx([1], row_world=OTHER)])
        runtime = ReadRuntime(driver)
        with pytest.raises(WorldLeak):
            async with request_budget():
                await capture(runtime, WORLD, rows)
        assert driver.sessions[0].cancelled
        await runtime.close()

    asyncio.run(scenario())


def test_stalled_stream_cancels_native_session_and_prevents_final_marker():
    async def scenario():
        tx = Tx([1, 1], stall=True)
        driver = CaptureDriver([tx])
        runtime = ReadRuntime(driver)
        with pytest.raises(ApiError):
            async with request_budget(seconds=0.02):
                await capture(runtime, WORLD, rows)
        assert driver.sessions[0].cancelled
        assert len(tx.queries) == 2
        await runtime.close()

    asyncio.run(scenario())


def test_capture_detaches_native_date_properties():
    async def scenario():
        runtime = ReadRuntime(CaptureDriver([Tx([1, 1])]))

        async def materialize(source, now):
            return {"date": date(2026, 11, 4)}

        try:
            async with request_budget():
                result = await capture(runtime, WORLD, materialize)
            assert result.unpack() == {"date": "2026-11-04"}
        finally:
            await runtime.close()

    asyncio.run(scenario())
