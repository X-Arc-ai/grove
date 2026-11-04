"""Real scoped queries for the private packet input collector, not a D14 acceptance proof."""

import asyncio

import pytest

from grove.reads.capture import capture
from grove.reads.inputs import LEXICAL_INDEXES, collect_inputs
from grove.reads.runtime import ReadRuntime, request_budget
from grove.store.connection import open_read_driver
from grove.store.errors import WorldLeak
from grove.writer.testing import plant_link, plant_node
from grove.writer.tx import WorldWriter


def test_native_inventory_and_memberships_share_the_capture_bracket(driver, world, make_world):
    other = make_world()
    plant_node(driver, world, "World", next_sequence=7)
    first = plant_node(driver, world, "Item", title="Cedar project")
    last = plant_node(driver, world, "Item", title="Room 4")
    source_row = plant_node(driver, world, "Source", content="before")
    alias = plant_node(
        driver, world, "Alias", phrase="Cedar", target_type="item", target_id=first["id"]
    )
    value = plant_node(driver, world, "HistoryValue:PastValue", value="Cedar", item_id=first["id"])
    reason = plant_node(driver, world, "HistoryValue", value="An independent reason")
    plant_node(driver, world, "Belief", item_id=first["id"])
    mark = plant_node(driver, world, "Impact", link_id="invented-mark")
    plant_node(driver, world, "ChangeEntry", sequence=7, reason=reason["id"])
    link = plant_link(driver, world, first["id"], last["id"], meaning="invented-meaning")
    plant_node(driver, world, "Request", state="queued")
    plant_node(driver, other, "Item", title="Cedar other world")
    plant_node(driver, other, "Source", content="foreign source")

    def mutate():
        WorldWriter(driver, world).transaction(
            lambda tx: tx.run(
                "MATCH (s:Source) WHERE s.id = $id SET s.content = $text",
                id=source_row["id"],
                text="after",
            )
        )

    async def run():
        runtime = ReadRuntime(open_read_driver())
        observed = []

        async def materialize(source, now):
            result = await collect_inputs(source, runtime, search_text="Cedar")
            observed.append(result["tables"]["sources"][0]["row"]["content"])
            if len(observed) == 1:
                await runtime.worker(mutate)
            return result

        try:
            async with request_budget():
                result = await capture(runtime, world, materialize)
            payload = result.unpack()
            assert observed == ["before", "after"]
            assert result.commit == 1 and result.history_sequence == 7
            assert payload["tables"]["sources"][0]["row"]["content"] == "after"
            assert len(payload["tables"]["items"]) == 2
            assert len(payload["tables"]["history_values"]) == 2
            assert payload["tables"]["beliefs"][0]["row"]["item_id"] == first["id"]
            assert payload["tables"]["impacts"][0]["row"]["link_id"] == mark["link_id"]
            assert payload["tables"]["change_entries"][0]["row"]["reason"] == reason["id"]
            assert payload["links"] == [link]
            expected = (first["id"], alias["id"], value["id"])
            assert [
                [row["row"]["id"] for row in payload["lexical_membership"][index]]
                for index in LEXICAL_INDEXES
            ] == [[key] for key in expected]
            assert "requests" not in payload["tables"]
            assert payload["lexical_membership"]["grove_past_values"][0]["labels"] == [
                "HistoryValue",
                "PastValue",
            ]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_native_cross_world_link_is_refused_instead_of_filtered_away(driver, world, make_world):
    other = make_world()
    plant_node(driver, world, "World", next_sequence=0)
    first = plant_node(driver, world, "Item", title="Cedar")
    last = plant_node(driver, other, "Item", title="Other world")
    plant_link(driver, world, first["id"], last["id"], to_world=other)

    async def run():
        runtime = ReadRuntime(open_read_driver())

        async def materialize(source, now):
            return await collect_inputs(source, runtime)

        try:
            with pytest.raises(WorldLeak):
                async with request_budget():
                    await capture(runtime, world, materialize)
        finally:
            await runtime.close()

    asyncio.run(run())
