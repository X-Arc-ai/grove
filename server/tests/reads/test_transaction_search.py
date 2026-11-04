import asyncio

import pytest

from grove.store.errors import ScopeError
from grove.store.query import transaction_search

WORLD = "10000000-0000-7000-8000-000000000001"


def test_complete_lexical_inventory_controls_search_bound_and_discards_corpus_scores():
    async def scenario():
        calls = []

        async def run(cypher, params):
            calls.append((cypher, params))
            if "count(n)" in cypher:
                return [{"total": 240}]
            assert params["limit"] == 240
            assert params["q"].startswith(f'+world_id:"{WORLD}" +(')
            return [
                {"n": {"id": str(i)}, "labels": ["Item"], "score": 1000 - i} for i in range(240)
            ]

        found = await transaction_search(run, WORLD, "grove_items_lexical", "quarry")
        assert len(found) == 240
        assert all("score" not in row for row in found)
        assert len(calls) == 2 and all(params["world_id"] == WORLD for _, params in calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("total", [True, -1, 2_147_483_648, None])
def test_invalid_complete_inventory_refuses_before_search(total):
    async def scenario():
        calls = []

        async def run(cypher, params):
            calls.append(cypher)
            return [{"total": total}]

        with pytest.raises(ScopeError):
            await transaction_search(run, WORLD, "grove_items_lexical", "quarry")
        assert len(calls) == 1

    asyncio.run(scenario())
