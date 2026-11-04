import asyncio
import copy
import threading

import pytest

from grove.api.errors import ApiError
from grove.reads.inputs import LEXICAL_INDEXES, collect_inputs
from grove.reads.runtime import ReadRuntime, request_budget
from grove.store.primitives import MEANING_DESCRIBES, PRIMITIVE_MEANINGS

WORLD = "10000000-0000-7000-8000-000000000001"
OTHER = "10000000-0000-7000-8000-000000000002"


class Driver:
    async def close(self):
        pass


def node(label, key="one", **values):
    labels = label.split(":")
    identity = {"id": key}
    if labels[0] == "Belief":
        identity = {"item_id": key}
    elif labels[0] == "Impact":
        identity = {"link_id": key}
    elif labels[0] == "ChangeEntry":
        identity = {"sequence": key}
    return {"labels": labels, "row": {"world_id": WORLD, **identity, **values}}


def relation(first, last, **values):
    return {
        "row": {
            "world_id": WORLD,
            "id": "link",
            "from_item": first["row"]["id"],
            "to_item": last["row"]["id"],
            **values,
        },
        "from_node": first["row"],
        "from_labels": first["labels"],
        "to_node": last["row"],
        "to_labels": last["labels"],
    }


def match(record):
    return {"node": record["row"], "labels": record["labels"]}


class Source:
    world_id = WORLD

    def __init__(self, nodes=(), links=(), memberships=None):
        self.nodes, self.links = list(nodes), list(links)
        self.memberships = memberships or {index: [] for index in LEXICAL_INDEXES}
        self.reads, self.searches = [], []

    async def read(self, query):
        self.reads.append(query)
        return copy.deepcopy(self.nodes if len(self.reads) % 2 else self.links)

    async def search(self, index, text):
        self.searches.append((index, text))
        return copy.deepcopy(self.memberships[index])


def collect(source, search_text=None):
    async def run():
        runtime = ReadRuntime(Driver())
        try:
            async with request_budget():
                return await collect_inputs(source, runtime, search_text=search_text)
        finally:
            await runtime.close()

    return asyncio.run(run())


def test_inventory_keeps_all_text_homes_aliases_and_non_id_rows():
    rows = [
        node("Item", "item", title="Cedar", properties='{"room":"4"}'),
        node("Alias", "alias", target_type="item", target_id="item", phrase="Cedar project"),
        node("Source", "source", content="Room 4 at 14:00 UTC"),
        node("Connector", "connector", last_outcome="nothing_new"),
        node("ChangeEntry", 2, reason="reason"),
        node("ChangeEntry", 1, old_value_id="old", new_value_id="new"),
        node("Belief", "item"),
        node("Belief", "other"),
        node("Impact", "first-mark", explanation="explanation"),
        node("Impact", "second-mark"),
    ]
    rows += [
        node("HistoryValue", key, value=key)
        for key in ("old", "new", "reason", "clock", "explanation")
    ]
    rows.append(node("HistoryValue:PastValue", "past", item_id="item", value="Old Cedar"))
    result = collect(Source(reversed(rows)))
    tables = result["tables"]
    assert len(tables["history_values"]) == 6
    assert [row["row"]["sequence"] for row in tables["change_entries"]] == [1, 2]
    assert len(tables["beliefs"]) == len(tables["impacts"]) == 2
    assert tables["aliases"][0]["row"]["target_type"] == "item"
    assert tables["sources"][0]["row"]["content"] == "Room 4 at 14:00 UTC"
    past = next(row for row in tables["history_values"] if row["row"]["id"] == "past")
    assert past["labels"] == ["HistoryValue", "PastValue"]
    assert result["lexical_membership"] is None


def test_complete_native_membership_has_no_page_limit_and_keeps_three_indexes_separate():
    items = [node("Item", f"item-{number:03}", title="Cedar") for number in range(61)]
    alias = node("Alias", "alias", target_type="item", target_id="item-000")
    past = node("HistoryValue:PastValue", "past", item_id="item-000", value="Cedar")
    source = Source(
        [*reversed(items), alias, past],
        memberships=dict(
            zip(
                LEXICAL_INDEXES,
                ([match(row) for row in reversed(items)], [match(alias)], [match(past)]),
                strict=True,
            )
        ),
    )
    result = collect(source, "Cedar")
    assert len(result["lexical_membership"][LEXICAL_INDEXES[0]]) == 61
    assert result["lexical_membership"][LEXICAL_INDEXES[0]][0]["row"]["id"] == "item-000"
    assert len(result["lexical_membership"][LEXICAL_INDEXES[1]]) == 1
    assert len(result["lexical_membership"][LEXICAL_INDEXES[2]]) == 1
    assert source.searches == [(index, "Cedar") for index in LEXICAL_INDEXES]
    assert len(source.reads) == 2
    assert all("LIMIT" not in query for query in source.reads)


@pytest.mark.parametrize(
    "label,key",
    [("Belief", "item"), ("Impact", "mark"), ("ChangeEntry", 1), ("HistoryValue", "value")],
)
def test_duplicate_catalogue_identity_refuses_instead_of_overwriting(label, key):
    rows = [node(label, key, extra="first"), node(label, key, extra="second")]
    with pytest.raises(ApiError) as caught:
        collect(Source(rows))
    assert caught.value.status == 503


@pytest.mark.parametrize(
    "row",
    [
        node("Item", world_id=OTHER),
        node("Item", id=None),
        node("ChangeEntry", True),
        node("ChangeEntry", 0),
        node("Request"),
        node("Item:PastValue"),
        node("Item:Source"),
        node("Item:Item"),
    ],
)
def test_foreign_missing_or_undeclared_node_identity_is_not_absence(row):
    with pytest.raises(ApiError) as caught:
        collect(Source([row]))
    assert caught.value.status == 503


def test_links_keep_complete_properties_and_validate_actual_endpoints():
    first, last = node("Item", "first"), node("Item", "last")
    link = relation(first, last, meaning="typed", end_class="corrected", original_phrase="words")
    result = collect(Source([first, last], [link]))
    assert result["links"] == [link["row"]]


@pytest.mark.parametrize("fault", ["foreign", "missing", "property", "label", "duplicate"])
def test_invalid_link_inventory_refuses_as_a_whole(fault):
    first, last = node("Item", "first"), node("Item", "last")
    nodes, links = [first, last], [relation(first, last)]
    if fault == "foreign":
        links[0]["to_node"] = {**last["row"], "world_id": OTHER}
    elif fault == "missing":
        nodes.pop()
    elif fault == "property":
        links[0]["row"]["to_item"] = "wrong"
    elif fault == "label":
        links[0]["to_labels"] = ["Source"]
    elif fault == "duplicate":
        links *= 2
    with pytest.raises(ApiError):
        collect(Source(nodes, links))


@pytest.mark.parametrize(
    "fault", ["foreign", "missing", "wrong_index", "duplicate", "missing_past_label"]
)
def test_malformed_native_membership_cannot_become_a_successful_partial_capture(fault):
    row = node("HistoryValue:PastValue", "past", item_id="item")
    memberships = {index: [] for index in LEXICAL_INDEXES}
    memberships[LEXICAL_INDEXES[2]] = [match(row)]
    source = Source([row], memberships=memberships)
    if fault == "foreign":
        memberships[LEXICAL_INDEXES[2]][0]["node"] = {**row["row"], "world_id": OTHER}
    elif fault == "missing":
        source.nodes = []
    elif fault == "wrong_index":
        memberships[LEXICAL_INDEXES[0]] = memberships[LEXICAL_INDEXES[2]]
    elif fault == "duplicate":
        memberships[LEXICAL_INDEXES[2]] *= 2
    elif fault == "missing_past_label":
        memberships[LEXICAL_INDEXES[2]][0]["labels"] = ["HistoryValue"]
    with pytest.raises(ApiError):
        collect(source, "Cedar")


def test_fixed_inputs_are_copies_and_no_optional_or_platform_query_runs():
    source = Source()
    result = collect(source)
    result["fixed"]["meanings"][0]["name"] = "caller edit"
    assert PRIMITIVE_MEANINGS[MEANING_DESCRIBES]["name"] == "describes"
    assert collect(Source())["fixed"]["meanings"][0]["name"] == "describes"
    assert set(result["tables"]) == {
        "items",
        "kinds",
        "meanings",
        "property_names",
        "aliases",
        "capabilities",
        "evidence",
        "beliefs",
        "change_entries",
        "history_values",
        "connectors",
        "sources",
        "episodes",
        "areas",
        "impacts",
        "cascade_proposals",
        "contradictions",
        "verifications",
    }
    assert not source.searches
    for query in source.reads:
        assert not any(
            label in query for label in ("World", "User", "Request", "RunRecord", "ForgetLedger")
        )


def test_validation_runs_in_the_bounded_worker(monkeypatch):
    from grove.reads import inputs

    original, threads = inputs._record, []
    event_loop_thread = threading.get_ident()

    def record(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(inputs, "_record", record)
    collect(Source([node("Item")]))
    assert threads and all(thread != event_loop_thread for thread in threads)
