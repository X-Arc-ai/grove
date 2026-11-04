import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from grove.api.errors import ApiError
from grove.reads.capture import Capture
from grove.reads.coverage import CoverageStatement
from grove.reads.cursors import CursorBinding, CursorStore
from grove.reads.requests import parse_recall
from grove.reads.runtime import request_budget
from grove.reads.service import prepare_current_pages, query_digest

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
CAPTURE = Capture("world", 4, 9, "capture", NOW, b"private raw rows")
COVERAGE = CoverageStatement((), ("calendar",), False, None)


@dataclass
class Rendered:
    card: dict
    link_ids: tuple[str, ...]


def item(number, links=0, shown=0):
    link_ids = tuple(f"link-{number}-{n}" for n in range(links))
    return Rendered(
        {
            "id": f"item-{number}",
            "title": "Cedar",
            "links": [{"id": key} for key in link_ids[:shown]],
            "links_remaining": links - shown,
            "links_need_cursor": links > shown,
        },
        link_ids,
    )


def setup(limit=50):
    query = parse_recall({"about": "Cedar", "limit": limit})
    binding = CursorBinding(
        "world", "principal", "grant", "policy", query_digest(query), limit, "capture", "privacy"
    )
    return query, binding


def render_link(item_id, link_id):
    return {"id": link_id, "from": item_id, "title": "Room 4"}


def test_all_item_and_hub_pages_are_immutable_before_first_admission():
    async def scenario():
        query, binding = setup(2)
        rendered = [item(1, 123, 5), item(2), item(3)]
        async with request_budget():
            prepared = prepare_current_pages(
                CAPTURE, query, binding, rendered, COVERAGE, render_link, complete=True
            )
            assert len(prepared.pages) == 5  # two item pages plus three link pages
            store = CursorStore()
            first = json.loads(store.admit(prepared, check_lease=lambda value: None))
            assert first["coverage"] == {
                "sources": [],
                "not_connected": ["calendar"],
                "incomplete": False,
                "since": None,
                "returned": 2,
                "total": 3,
                "truncated": True,
            }
            assert first["answer"] == "truncated"
            second = json.loads(
                store.page(first["next_cursor"], binding.context(), check_lease=lambda value: None)
            )
            assert [row["id"] for row in second["items"]] == ["item-3"]
            assert second["next_cursor"] is None
            cursor = first["items"][0]["links_cursor"]
            seen = [row["id"] for row in first["items"][0]["links"]]
            rendered[0].card["title"] = "later mutable value"
            rendered[0].card["links"].clear()
            while cursor:
                page = json.loads(
                    store.page(cursor, binding.context(), check_lease=lambda value: None)
                )
                assert page["items"][0]["title"] == "Cedar"
                assert page["page"]["total"] == 123
                assert page["commit"] == 4 and page["history_sequence"] == 9
                assert page["capture_id"] == "capture"
                assert page["reflected_at"] == NOW.isoformat()
                seen += [row["id"] for row in page["items"][0]["links"]]
                cursor = page["next_cursor"]
                assert page["items"][0]["links_cursor"] == cursor
            assert seen == list(rendered[0].link_ids)
            assert len(set(seen)) == 123
            assert b"private raw rows" not in b"".join(prepared.pages)

    asyncio.run(scenario())


def test_two_hub_chains_do_not_cross_or_hide_items():
    async def scenario():
        query, binding = setup(1)
        async with request_budget():
            prepared = prepare_current_pages(
                CAPTURE,
                query,
                binding,
                [item(1, 120, 5), item(2, 80, 3)],
                COVERAGE,
                render_link,
                complete=True,
            )
        pages = dict(zip(prepared.handles, map(json.loads, prepared.pages), strict=True))
        for index, total in ((0, 120), (1, 80)):
            initial = json.loads(prepared.pages[index])
            cursor = initial["items"][0]["links_cursor"]
            seen = list(initial["items"][0]["links"])
            while cursor:
                page = pages[cursor]
                assert page["page"]["item"] == f"item-{index + 1}"
                seen += page["items"][0]["links"]
                cursor = page["next_cursor"]
            assert len(seen) == total

    asyncio.run(scenario())


@pytest.mark.parametrize("incomplete,answer", [(False, "no_record"), (True, "not_seen_since")])
def test_empty_inventory_uses_complete_coverage_instead_of_raw_absence(incomplete, answer):
    async def scenario():
        query, binding = setup()
        async with request_budget():
            prepared = prepare_current_pages(
                CAPTURE,
                query,
                binding,
                [],
                replace(COVERAGE, incomplete=incomplete),
                render_link,
                complete=True,
            )
        packet = json.loads(prepared.pages[0])
        assert len(prepared.pages) == 1 and packet["items"] == []
        assert packet["answer"] == answer and packet["next_cursor"] is None
        assert packet["coverage"]["total"] == 0

    asyncio.run(scenario())


def test_ambiguity_survives_pagination_and_query_digest_ignores_only_cursor():
    async def scenario():
        query, binding = setup(1)
        assert query_digest(replace(query, cursor="opaque")) == binding.query_digest
        assert query_digest(replace(query, about="different")) != binding.query_digest
        async with request_budget():
            prepared = prepare_current_pages(
                CAPTURE,
                query,
                binding,
                [item(1), item(2)],
                COVERAGE,
                render_link,
                complete=True,
                ambiguous=True,
            )
        assert all(json.loads(page)["answer"] == "ambiguous" for page in prepared.pages)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [("world_id", "other"), ("capture_id", "other"), ("page_size", 1), ("query_digest", "other")],
)
def test_wrong_capture_or_query_binding_refuses_before_retention(field, value):
    async def scenario():
        query, binding = setup()
        async with request_budget():
            with pytest.raises(ApiError):
                prepare_current_pages(
                    CAPTURE,
                    query,
                    replace(binding, **{field: value}),
                    [item(1)],
                    COVERAGE,
                    render_link,
                    complete=True,
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_item",
        "duplicate_link",
        "foreign_preview",
        "wrong_remaining",
        "wrong_signal",
        "incomplete",
        "bad_link_renderer",
    ],
)
def test_partial_or_invalid_inventory_never_creates_a_successful_prefix(fault):
    async def scenario():
        query, binding = setup()
        rows = [item(1, 3, 1)]
        complete, renderer = True, render_link
        if fault == "duplicate_item":
            rows *= 2
        elif fault == "duplicate_link":
            rows[0].link_ids = ("duplicate", "duplicate")
        elif fault == "foreign_preview":
            rows[0].card["links"] = [{"id": "outside"}]
        elif fault == "wrong_remaining":
            rows[0].card["links_remaining"] = 1
        elif fault == "wrong_signal":
            rows[0].card["links_need_cursor"] = False
        elif fault == "incomplete":
            complete = False
        elif fault == "bad_link_renderer":

            def renderer(*args):
                return {"id": "outside"}

        store = CursorStore()
        async with request_budget():
            with pytest.raises(ApiError):
                prepared = prepare_current_pages(
                    CAPTURE, query, binding, rows, COVERAGE, renderer, complete=complete
                )
                store.admit(prepared, check_lease=lambda value: None)
        assert store.retained_bytes == 0

    asyncio.run(scenario())


def test_oversized_later_page_refuses_before_any_page_is_admitted(monkeypatch):
    from grove.reads import service

    monkeypatch.setattr(service, "MAX_PAGE_BYTES", 1000)

    async def scenario():
        query, binding = setup(1)
        rows = [item(1), item(2)]
        rows[1].card["title"] = "x" * 1001
        async with request_budget():
            with pytest.raises(ApiError):
                prepare_current_pages(
                    CAPTURE, query, binding, rows, COVERAGE, render_link, complete=True
                )

    asyncio.run(scenario())


def test_five_thousand_link_hub_has_every_promised_page_before_admission():
    async def scenario():
        query, binding = setup()
        hub = item(1, 5000, 5)
        async with request_budget():
            prepared = prepare_current_pages(
                CAPTURE, query, binding, [hub], COVERAGE, render_link, complete=True
            )
        assert len(prepared.pages) == 101
        lookup = dict(zip(prepared.handles, map(json.loads, prepared.pages), strict=True))
        first = json.loads(prepared.pages[0])
        cursor = first["items"][0]["links_cursor"]
        ids = [row["id"] for row in first["items"][0]["links"]]
        while cursor is not None:
            page = lookup[cursor]
            assert len(page["items"][0]["links"]) <= 50
            ids += [row["id"] for row in page["items"][0]["links"]]
            cursor = page["next_cursor"]
        assert tuple(ids) == hub.link_ids
        assert prepared.byte_size == sum(map(len, prepared.pages))

    asyncio.run(scenario())


def test_total_byte_capacity_refuses_before_cursor_admission(monkeypatch):
    from grove.reads import service

    monkeypatch.setattr(service, "MAX_BYTES", 1000)

    async def scenario():
        query, binding = setup(1)
        async with request_budget():
            with pytest.raises(ApiError):
                prepare_current_pages(
                    CAPTURE,
                    query,
                    binding,
                    [item(1), item(2)],
                    COVERAGE,
                    render_link,
                    complete=True,
                )

    asyncio.run(scenario())
