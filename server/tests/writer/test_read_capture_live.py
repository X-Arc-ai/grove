"""Native consistency controls, without any claim about a D14 production provider."""

import asyncio

import pytest

from grove.reads.capture import capture
from grove.reads.runtime import ReadRuntime, request_budget
from grove.store.connection import open_read_driver
from grove.writer.testing import plant_node
from grove.writer.tx import WorldWriter

# Every class here is a mutable D7 input. The fixture uses its actual content/metadata property.
INPUTS = (
    ("Item", "title", "before", "after"),
    ("Source", "content", "before", "after"),
    ("Connector", "last_outcome", "nothing_new", "failed"),
    ("Alias", "phrase", "before", "after"),
    ("Kind", "name", "before", "after"),
    ("Area", "member_count", 1, 2),
    ("Impact", "state", "open", "dismissed"),
)


@pytest.mark.parametrize("label,field,before,after", INPUTS)
def test_interleaved_input_write_restarts_complete_capture(
    driver, world, label, field, before, after
):
    plant_node(driver, world, "World", next_sequence=0)
    row = plant_node(driver, world, label, **{field: before})

    def mutate():
        WorldWriter(driver, world).transaction(
            lambda tx: tx.run(
                f"MATCH (n:{label}) WHERE n.id = $id SET n.{field} = $value",
                id=row["id"],
                value=after,
            )
        )

    async def scenario():
        runtime = ReadRuntime(open_read_driver())
        observed = []

        async def materialize(source, now):
            records = await source.read(f"MATCH (n:{label}) RETURN n AS row")
            observed.append(records[0]["row"][field])
            if len(observed) == 1:
                await runtime.worker(mutate)
            return records

        try:
            async with request_budget():
                result = await capture(runtime, world, materialize)
            assert observed == [before, after]
            assert result.commit == 1
            assert result.history_sequence == 0
            assert result.unpack()[0]["row"][field] == after
        finally:
            await runtime.close()

    asyncio.run(scenario())
