"""D2 counting: the earning bars count distinct episodes among active supporting rows (S42).

Spec, D2 line 766: "The earning bar for words (D8) and for beliefs (D9) counts distinct episode
ids among a target's active supporting rows, so a ten-month export earns as many episodes as it
has windows, and two rows drawn from one message count once." D2 "How to test it": "Counting: two
evidence rows from one message, or three restatements in one episode, count as one episode."
"""

from typing import Any

import pytest

from grove.store.evidence_counts import distinct_episodes
from grove.store.query import WorldQuery
from grove.writer import record_episode
from grove.writer.tx import WorldTx, WorldWriter
from tests.writer.helpers import (
    T0,
    NoStore,
    apply,
    at_minute,
    create_item,
    create_link,
    created_id,
    new_op_id,
    observation,
    read,
)

RECHECK = "2027-06-01T00:00:00+00:00"


def window(driver, world_id: str, chat: dict[str, str], first_key: str) -> dict[str, str]:
    """Another window cut from the same chat: the same source, a new episode."""
    episode_id = record_episode(
        driver,
        world_id,
        connector_id=chat["connector_id"],
        source_ids=[chat["source_id"]],
        first_message_key=first_key,
        last_message_key=f"{first_key}-last",
        start_time=T0,
        end_time=at_minute(40),
        cut_rule="chat_window_90m",
    )
    return {**chat, "episode_id": episode_id}


def attach(target: str, ev_id: str, polarity: str = "supports") -> dict[str, Any]:
    return {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": target,
        "ev_id": ev_id,
        "polarity": polarity,
    }


def rows_on(driver, world_id: str, target_id: str) -> list[dict[str, Any]]:
    rows = read(driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=target_id)
    return [row["e"] for row in rows]


def one_item(driver, world_id: str, chat: dict[str, str], title: str, line: int) -> str:
    ev = observation(chat, f"e{line}", line)
    return created_id(apply(driver, world_id, [create_item("person", title, ev["ev_id"])], [ev]))


def test_two_rows_from_one_message_count_once(driver, seeded, chat):
    w = seeded["world_id"]
    ops = [
        create_item("person", "Rowan Okonkwo", "e1"),
        create_item("place", "Okonkwo hall", "e1"),
    ]
    receipt = apply(driver, w, ops, [observation(chat, "e1", 1)])
    rowan, hall = created_id(receipt, nth=0), created_id(receipt, nth=1)
    rows = [*rows_on(driver, w, rowan), *rows_on(driver, w, hall)]
    assert len(rows) == 2 and len({row["message_key"] for row in rows}) == 1

    query = WorldQuery(driver, w)
    assert distinct_episodes(query, [rowan, hall]) == 1
    assert distinct_episodes(query, [rowan]) == distinct_episodes(query, [hall]) == 1


def test_three_restatements_in_one_episode_count_once(driver, seeded, chat):
    w = seeded["world_id"]
    ops = [
        create_item("person", "Rowan Okonkwo", "e1"),
        create_item("place", "Okonkwo hall", "e1"),
    ]
    receipt = apply(driver, w, ops, [observation(chat, "e1", 1)])
    rowan, hall = created_id(receipt, nth=0), created_id(receipt, nth=1)
    for line in (2, 3, 4):
        ev = observation(chat, f"r{line}", line)
        link = create_link(rowan, "lives in", hall, ev["ev_id"], recheck_by=RECHECK)
        apply(driver, w, [link], [ev])

    [link] = read(
        driver,
        w,
        "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.active_key IS NOT NULL RETURN l.id AS id",
    )
    assert len(rows_on(driver, w, link["id"])) == 3
    assert distinct_episodes(WorldQuery(driver, w), [link["id"]]) == 1


def test_an_ended_row_and_a_contradicting_row_do_not_count(driver, seeded, chat):
    w = seeded["world_id"]
    rowan = one_item(driver, w, chat, "Rowan Okonkwo", 1)
    second, third = window(driver, w, chat, "k-second"), window(driver, w, chat, "k-third")
    apply(driver, w, [attach(rowan, "s5")], [observation(second, "s5", 5)])
    query = WorldQuery(driver, w)
    assert distinct_episodes(query, [rowan]) == 2

    apply(driver, w, [attach(rowan, "t6", "contradicts")], [observation(third, "t6", 6)])
    assert distinct_episodes(query, [rowan]) == 2

    [ended] = [
        row for row in rows_on(driver, w, rowan) if row["episode_id"] == second["episode_id"]
    ]
    end = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": ended["id"],
        "end_reason": "correction",
    }
    apply(driver, w, [end], [])
    assert distinct_episodes(query, [rowan]) == 1


def test_one_count_runs_over_all_the_targets(driver, seeded, chat):
    # A word's bar counts across its links: an episode that supports two of them counts once.
    w = seeded["world_id"]
    rowan = one_item(driver, w, chat, "Rowan Okonkwo", 1)
    ilse = one_item(driver, w, chat, "Ilse Tamsin", 2)
    shared, own = window(driver, w, chat, "k-shared"), window(driver, w, chat, "k-own")
    apply(
        driver,
        w,
        [attach(rowan, "a7"), attach(ilse, "a7"), attach(ilse, "b8")],
        [observation(shared, "a7", 7), observation(own, "b8", 8)],
    )
    query = WorldQuery(driver, w)
    assert distinct_episodes(query, [rowan]) == 2
    assert distinct_episodes(query, [ilse]) == 3
    assert distinct_episodes(query, [rowan, ilse]) == 3


def test_no_targets_read_nothing():
    assert distinct_episodes(NoStore(), []) == 0
    with pytest.raises(TypeError):
        distinct_episodes(NoStore(), "a target id")


def test_a_transaction_counts_the_rows_it_wrote(driver, seeded, chat):
    w = seeded["world_id"]
    rowan = one_item(driver, w, chat, "Rowan Okonkwo", 1)
    second = window(driver, w, chat, "k-second")

    class RolledBack(Exception):
        pass

    seen: dict[str, int] = {}

    def work(tx: WorldTx) -> None:
        tx.run(
            "CREATE (e:Evidence) SET e += $row",
            row={
                "id": f"ev-{rowan[-8:]}",
                "episode_id": second["episode_id"],
                "source_id": second["source_id"],
                "message_key": "mk-planted-9",
                "target_type": "item",
                "target_id": rowan,
                "polarity": "supports",
                "active": True,
            },
        )
        seen["inside"] = distinct_episodes(tx, [rowan])
        seen["outside"] = distinct_episodes(WorldQuery(driver, w), [rowan])
        raise RolledBack

    with pytest.raises(RolledBack):
        WorldWriter(driver, w).transaction(work)
    assert seen == {"inside": 2, "outside": 1}
    assert distinct_episodes(WorldQuery(driver, w), [rowan]) == 1
