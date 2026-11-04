"""Pure renderer/page integration; this does not establish production authorization."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from grove.api.errors import ApiError
from grove.reads.capture import Capture
from grove.reads.coverage import ConnectorState, CoverageStatement, evaluate_coverage
from grove.reads.cursors import CursorBinding, CursorStore
from grove.reads.requests import parse_recall
from grove.reads.runtime import request_budget
from grove.reads.service import prepare_renderer_pages, query_digest

WORLD = "t_hosts-b1_22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.mark.parametrize("link_count,expanded", [(125, False), (125, True), (5000, True)])
def test_real_current_cards_keep_provenance_and_every_link_across_page_chains(link_count, expanded):
    from grove.reads.current_packet import CurrentRenderer, CurrentRows

    def row(identity, **values):
        return {"id": identity, "world_id": WORLD, **values}

    async def scenario():
        items = [
            row(
                f"item-{index:05}",
                kind="place",
                title="Cedar" if index == 0 else f"Room {index}",
                properties="{}",
                recorded=NOW - timedelta(hours=2),
                last_confirmed=NOW - timedelta(hours=1),
                derived_source_type="said",
                provenance_evidence_id="proof" if index == 0 else None,
            )
            for index in range(link_count + 1)
        ]
        links = [
            row(
                f"link-{index:05}",
                from_item=items[0]["id"],
                to_item=items[index]["id"],
                meaning="related",
                active_key=f"link-{index:05}",
                recorded=NOW - timedelta(minutes=1),
            )
            for index in range(1, link_count + 1)
        ]
        coverage = evaluate_coverage(
            WORLD,
            [ConnectorState(WORLD, "connector", "web_pages", "nothing_new", 3600, NOW)],
            now=NOW,
            inventory_complete=True,
            disclosure_complete=True,
        )
        data = CurrentRows(
            world_id=WORLD,
            items=items,
            kinds=[row("place", name="place", lifecycle_class="entity")],
            meanings=[row("related", name="related")],
            links=links,
            evidence=[
                row(
                    "proof",
                    source_id="source",
                    episode_id="episode",
                    locator="line:1",
                    message_key="message-1",
                    source_time=NOW - timedelta(hours=1),
                    recorded=NOW,
                    target_type="item",
                    target_id=items[0]["id"],
                    polarity="supports",
                    source_type="said",
                    derivation="direct",
                    speaker_kind="person",
                    modality="asserted",
                    active=True,
                )
            ],
            sources=[row("source", connector_id="connector", content="Cedar uses room five.")],
            episodes=[row("episode", connector_id="connector", source_ids=["source"])],
        )
        renderer = CurrentRenderer(data, captured_at=NOW, coverage=coverage, complete=True)
        arguments = {"about": "Cedar", "limit": 1}
        if expanded:
            arguments["include"] = ["evidence"]
        query = parse_recall(arguments)
        capture = Capture(WORLD, 12, 40, "captured", NOW, b"raw inputs must stay private")
        binding = CursorBinding(
            WORLD, "principal", "grant", "policy", query_digest(query), 1, "captured", "privacy"
        )
        async with request_budget():
            prepared = prepare_renderer_pages(
                capture,
                query,
                binding,
                renderer,
                [value["id"] for value in items[:2]],
                complete=True,
            )
            store = CursorStore()
            first = json.loads(store.admit(prepared, check_lease=lambda value: None))
            card = first["items"][0]
            assert card["provenance"]["excerpt"] == "Cedar uses room five."
            assert card["newest_evidence"]["message_key"] == "message-1"
            if expanded:
                assert card["evidence"] == [card["newest_evidence"]]
            else:
                assert "evidence" not in card
            assert card["link_counts"]["total"] == link_count
            assert card["link_counts"]["by_meaning"] == {"related": link_count}
            assert len(card["links"]) == 5
            ids = [value["id"] for value in card["links"]]
            cursor = card["links_cursor"]
            items[0]["title"] = "Later source title"
            renderer.items[items[0]["id"]]["title"] = "Later renderer title"
            while cursor is not None:
                packet = json.loads(
                    store.page(cursor, binding.context(), check_lease=lambda value: None)
                )
                current = packet["items"][0]
                assert current["title"] == "Cedar"
                assert current["provenance"] == card["provenance"]
                assert current["link_counts"] == card["link_counts"]
                assert packet["commit"] == 12 and packet["history_sequence"] == 40
                assert packet["capture_id"] == "captured"
                assert packet["reflected_at"] == NOW.isoformat()
                ids.extend(value["id"] for value in current["links"])
                cursor = packet["next_cursor"]
            assert ids == [value["id"] for value in links]
            assert len(set(ids)) == link_count
            next_item = json.loads(
                store.page(first["next_cursor"], binding.context(), check_lease=lambda value: None)
            )
            assert next_item["items"][0]["id"] == items[1]["id"]
            assert next_item["next_cursor"] is None
            assert b"raw inputs must stay private" not in b"".join(prepared.pages)

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["world", "instant", "completeness"])
def test_renderer_and_capture_must_refer_to_the_same_complete_view(mismatch):
    from grove.reads.current_packet import CurrentRenderer, CurrentRows

    async def scenario():
        renderer = CurrentRenderer(
            CurrentRows(WORLD),
            captured_at=NOW,
            coverage=CoverageStatement((), (), False, None),
            complete=True,
        )
        capture = Capture(WORLD, 12, 40, "captured", NOW, b"empty view")
        query = parse_recall({"about": "Cedar"})
        if mismatch == "world":
            capture = replace(capture, world_id="another-world")
        elif mismatch == "instant":
            capture = replace(capture, reflected_at=NOW + timedelta(seconds=1))
        binding = CursorBinding(
            capture.world_id,
            "principal",
            "grant",
            "policy",
            query_digest(query),
            query.limit,
            "captured",
            "privacy",
        )
        async with request_budget():
            with pytest.raises(ApiError):
                prepare_renderer_pages(
                    capture, query, binding, renderer, [], complete=mismatch != "completeness"
                )

    asyncio.run(scenario())
