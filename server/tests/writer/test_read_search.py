import asyncio

from grove.reads.capture import PacketSource
from grove.reads.runtime import ReadRuntime, request_budget
from grove.store.connection import open_read_driver
from grove.writer.testing import plant_node


def test_native_complete_search_matches_every_world_row_and_ignores_other_world(driver, make_world):
    mine, other = make_world(), make_world()
    wanted = [plant_node(driver, mine, "Item", title="quarryfinch") for _ in range(24)]
    for _ in range(30):
        plant_node(driver, other, "Item", title="quarryfinch quarryfinch quarryfinch")

    async def scenario():
        runtime = ReadRuntime(open_read_driver())
        try:
            async with request_budget():
                async with runtime.transaction() as tx:
                    source = PacketSource(tx, mine)
                    rows = await source.search("grove_items_lexical", "quarryfinch")
                    source.invalidate()
            assert [row["node"]["id"] for row in rows] == sorted(row["id"] for row in wanted)
            assert all(row["node"]["world_id"] == mine for row in rows)
            assert all("score" not in row for row in rows)
        finally:
            await runtime.close()

    asyncio.run(scenario())
