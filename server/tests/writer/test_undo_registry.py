"""D3/O10 regressions for legacy logs and registry changes within an undo batch."""

import pytest

from grove.store.connection import DATABASE
from grove.store.d3_fingerprint import projection_rows
from grove.store.query import WorldQuery
from grove.store.replay import read_history
from grove.writer import BatchDeclined
from grove.writer.testing import refuse_real_world
from tests.writer.helpers import (
    LEARNER,
    PERSON,
    apply,
    create_item,
    create_link,
    make_source,
    observation,
)
from tests.writer.test_contract_d3 import undo
from tests.writer.test_d3_integration import day, earned_word, recorded_at
from tests.writer.test_ontology import GODPARENT, judged, meaning_row, mint_godparent, propose


def logs(driver, world):
    return {
        row["c"]["id"]: row["c"]
        for row in WorldQuery(driver, world).read("MATCH (c:OntologyChange) RETURN c")
    }


def state(driver, world):
    query = WorldQuery(driver, world)
    return projection_rows(query), read_history(query), logs(driver, world)


def make_logs_legacy(driver, world):
    """Use the exact log shape from before S46 added change_set_id."""
    refuse_real_world(world)
    with driver.session(database=DATABASE) as session:
        session.run(
            "MATCH (c:OntologyChange {world_id: $world}) REMOVE c.change_set_id",
            world=world,
        ).consume()


def later_ops(ev):
    return [
        create_item("person", "Mirela Stone", ev, temp_id="$m"),
        create_item("person", "Oskar Lind", ev, temp_id="$o"),
        create_link("$m", GODPARENT, "$o", ev),
    ]


@pytest.mark.parametrize("operation", ["propose", "promote"])
def test_legacy_log_tracks_repeated_undo_without_marking_another_operation(
    driver, seeded, chat, operation
):
    world = seeded["world_id"]
    # Equal instants must not conflate the mint with an automatic promotion. The latter
    # has no reason id; the former has a unique reason id shared with its entries.
    with recorded_at(day(25)):
        if operation == "promote":
            earned_word(driver, world, chat)
        else:
            mint_godparent(driver, world, chat)
        before = logs(driver, world)
        [target] = [row for row in before.values() if row["operation"] == operation]
        change_set = target["change_set_id"]
        make_logs_legacy(driver, world)
        untouched = {key: row for key, row in logs(driver, world).items() if key != target["id"]}

        for number in range(1, 4):
            receipt = apply(driver, world, [undo(change_set)], [], caller=PERSON)
            after = logs(driver, world)
            marked = after[target["id"]]
            assert marked.get("undone_by") == (receipt.change_set_id if number % 2 else None)
            assert marked["version"] == target["version"] + number
            assert marked["change_set_id"] == target["change_set_id"]
            assert {key: row for key, row in after.items() if key != target["id"]} == untouched
            change_set = receipt.change_set_id


def test_undo_refreshes_registry_before_later_word_use_in_the_same_batch(driver, seeded, chat):
    world = seeded["world_id"]
    mint, _ = mint_godparent(driver, world, chat)
    source = make_source(driver, world, "whatsapp", ["an invented later christening"])
    before = state(driver, world)

    with pytest.raises(BatchDeclined) as failed:
        apply(
            driver,
            world,
            [undo(mint.change_set_id), *later_ops("e2")],
            [observation(source, "e2", 1)],
            caller=PERSON,
        )

    assert [(row.code, row.rule) for row in failed.value.refusals] == [
        ("unknown_meaning", "unknown_meaning")
    ]
    assert state(driver, world) == before


@pytest.mark.parametrize("prior_undos", [0, 2])
def test_every_retiring_undo_refuses_a_later_active_word_use(driver, seeded, chat, prior_undos):
    world = seeded["world_id"]
    current, _ = mint_godparent(driver, world, chat)
    for _ in range(prior_undos):
        current = apply(driver, world, [undo(current.change_set_id)], [], caller=PERSON)
    source = make_source(driver, world, "whatsapp", ["another invented later christening"])
    apply(driver, world, later_ops("e2"), [observation(source, "e2", 1)], caller=LEARNER)
    before = state(driver, world)

    with pytest.raises(BatchDeclined) as failed:
        apply(driver, world, [undo(current.change_set_id)], [], caller=PERSON)

    assert [(row.code, row.rule) for row in failed.value.refusals] == [
        ("version_conflict", "word_in_use")
    ]
    assert meaning_row(driver, world, GODPARENT)["status"] == "provisional"
    assert state(driver, world) == before


def test_undo_of_two_undos_restores_both_ontology_log_rows(driver, seeded, chat):
    world = seeded["world_id"]
    first, _ = mint_godparent(driver, world, chat)
    other = "is mentor of"
    source = make_source(driver, world, "whatsapp", ["Mirela mentors Oskar in an invented club"])
    ids = judged(driver, world, name=other, fields={"temporal_class": ("permanent", "fits")})
    second = apply(
        driver,
        world,
        [
            *later_ops("e2")[:2],
            propose("e2", ids, name=other),
            create_link("$m", other, "$o", "e2"),
        ],
        [observation(source, "e2", 1)],
        caller=LEARNER,
    )
    both = apply(
        driver, world, [undo(first.change_set_id), undo(second.change_set_id)], [], caller=PERSON
    )
    assert all(row.get("undone_by") == both.change_set_id for row in logs(driver, world).values())

    apply(driver, world, [undo(both.change_set_id)], [], caller=PERSON)

    assert all(row.get("undone_by") is None for row in logs(driver, world).values())
    assert meaning_row(driver, world, GODPARENT)["status"] == "provisional"
    assert meaning_row(driver, world, other)["status"] == "provisional"


def test_ambiguous_legacy_promotions_refuse_without_a_partial_undo(driver, seeded, chat):
    world = seeded["world_id"]
    with recorded_at(day(25)):
        earned_word(driver, world, chat)
        [first] = [row for row in logs(driver, world).values() if row["operation"] == "promote"]
        apply(driver, world, [undo(first["change_set_id"])], [], caller=PERSON)
        source = make_source(driver, world, "whatsapp", ["an invented fourth christening chat"])
        second = apply(
            driver, world, later_ops("e4"), [observation(source, "e4", 1)], caller=LEARNER
        )
        assert (
            len([row for row in logs(driver, world).values() if row["operation"] == "promote"]) == 2
        )
        make_logs_legacy(driver, world)
        before = state(driver, world)

        with pytest.raises(BatchDeclined) as failed:
            apply(driver, world, [undo(second.change_set_id)], [], caller=PERSON)

        assert [(row.code, row.rule) for row in failed.value.refusals] == [
            ("version_conflict", "ontology_log_ambiguous")
        ]
        assert state(driver, world) == before
