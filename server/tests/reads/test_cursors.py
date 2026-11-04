import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.api.errors import ApiError
from grove.reads import cursors
from grove.reads.cursors import CursorBinding, CursorStore, PreparedPages, handles, prepare
from grove.reads.runtime import request_budget

BINDING = CursorBinding("world", "principal", "grant", "policy", "query", 2, "capture", "rev1")


def accepted(binding):
    assert binding.privacy_revision == "rev1"


def test_immutable_pages_keep_original_metadata_after_ordinary_writes():
    async def scenario():
        store = CursorStore()
        original = [b'{"commit":2,"coverage":"old","items":[1,2]}', b'{"commit":2,"items":[3]}']
        page_handles = handles(2)
        async with request_budget():
            prepared = prepare(BINDING, page_handles, original)
            assert store.admit(prepared, check_lease=accepted) == original[0]
        original[1] = b'{"commit":3,"items":[4]}'
        checks = []

        def check(binding):
            checks.append(binding)

        async with request_budget():
            assert store.page(page_handles[1], BINDING.context(), check_lease=check) == (
                b'{"commit":2,"items":[3]}'
            )
        assert checks == [BINDING]
        assert store.retained_bytes == sum(map(len, prepared.pages))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field", ["world_id", "principal", "grant", "policy", "query_digest", "page_size"]
)
def test_wrong_context_cannot_release_retained_bytes(field):
    async def scenario():
        store, page_handles = CursorStore(), handles(1)
        async with request_budget():
            store.admit(prepare(BINDING, page_handles, [b"private"]), check_lease=accepted)
            context = replace(BINDING.context(), **{field: 3 if field == "page_size" else "other"})
            with pytest.raises(ApiError) as caught:
                store.page(page_handles[0], context, check_lease=accepted)
        assert caught.value.details == {"rule": "cursor_expired"}
        assert "private" not in str(caught.value.body())

    asyncio.run(scenario())


def test_changed_privacy_revision_discards_entire_old_capture():
    async def scenario():
        store, page_handles = CursorStore(), handles(2)
        checks = []

        def refused(binding):
            checks.append(binding.privacy_revision)
            raise ApiError(409, "privacy_changed", "restart the query")

        async with request_budget():
            store.admit(
                prepare(BINDING, page_handles, [b"private1", b"private2"]), check_lease=accepted
            )
            with pytest.raises(ApiError) as caught:
                store.page(page_handles[1], BINDING.context(), check_lease=refused)
            assert caught.value.code == "privacy_changed"
            assert checks == ["rev1"] and store.retained_bytes == 0
            with pytest.raises(ApiError) as gone:
                store.page(page_handles[0], BINDING.context(), check_lease=accepted)
            assert gone.value.details == {"rule": "cursor_expired"}

    asyncio.run(scenario())


def test_byte_capacity_admission_is_atomic_and_never_evicts_promised_pages(monkeypatch):
    monkeypatch.setattr(cursors, "MAX_BYTES", 10)

    async def scenario():
        store, first, second = CursorStore(), handles(2), handles(1)
        async with request_budget():
            store.admit(prepare(BINDING, first, [b"1234", b"5678"]), check_lease=accepted)
            with pytest.raises(ApiError):
                store.admit(
                    prepare(replace(BINDING, capture_id="other"), second, [b"abc"]),
                    check_lease=accepted,
                )
            assert store.retained_bytes == 8
            assert store.page(first[1], BINDING.context(), check_lease=accepted) == b"5678"
            with pytest.raises(ApiError):
                store.page(second[0], BINDING.context(), check_lease=accepted)
            with pytest.raises(ApiError):
                prepare(BINDING, handles(1), [b"x" * 11])

    asyncio.run(scenario())


def test_expiry_and_restart_never_recapture_old_cursor():
    async def scenario():
        test_clock = clock.TestClock()
        test_clock.set(datetime(2026, 11, 4, tzinfo=UTC))
        with clock.using(test_clock):
            store, page_handles = CursorStore(), handles(1)
            async with request_budget():
                store.admit(prepare(BINDING, page_handles, [b"private"]), check_lease=accepted)
            test_clock.advance(seconds=300)
            async with request_budget():
                with pytest.raises(ApiError):
                    store.page(page_handles[0], BINDING.context(), check_lease=accepted)
                assert store.retained_bytes == 0
                restarted = CursorStore()
                assert restarted.incarnation != store.incarnation
                with pytest.raises(ApiError):
                    restarted.page(page_handles[0], BINDING.context(), check_lease=accepted)

    asyncio.run(scenario())


def test_closed_request_cannot_admit_late_worker_result():
    async def scenario():
        store = CursorStore()
        async with request_budget() as budget:
            prepared = prepare(BINDING, handles(1), [b"private"])
            budget.cancelled.set()
            with pytest.raises(ApiError):
                store.admit(prepared, check_lease=accepted)

    # The outer guard also refuses an otherwise successful exit after cancellation.
    with pytest.raises(ApiError):
        asyncio.run(scenario())


def test_capture_and_structural_page_capacity_are_bounded(monkeypatch):
    monkeypatch.setattr(cursors, "MAX_CAPTURES", 1)
    monkeypatch.setattr(cursors, "MAX_PAGES", 2)

    async def scenario():
        async with request_budget():
            with pytest.raises(ApiError):
                prepare(BINDING, handles(3), [b"", b"", b""])
            store = CursorStore()
            store.admit(prepare(BINDING, handles(1), [b""]), check_lease=accepted)
            with pytest.raises(ApiError):
                store.admit(
                    prepare(replace(BINDING, capture_id="new"), handles(1), [b""]),
                    check_lease=accepted,
                )

    asyncio.run(scenario())


def test_prepared_pages_cannot_underreport_bytes_or_retain_mutable_payloads():
    with pytest.raises(ValueError):
        PreparedPages(BINDING, handles(1), (b"private",), 0)
    with pytest.raises(TypeError):
        PreparedPages(BINDING, handles(1), (bytearray(b"private"),), 7)
