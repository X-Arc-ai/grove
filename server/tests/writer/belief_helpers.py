"""Test-only mutation helpers for belief guard controls; raw writes stay in writer tests."""

from grove.store.connection import DATABASE


def set_belief_fields(driver, world_id, belief_id, **columns):
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (b:Belief {world_id:$w,item_id:$id}) SET b += $columns",
            w=world_id,
            id=belief_id,
            columns=columns,
        ).consume()


def set_judgment_verdict(driver, world_id, judgment_ids, verdict):
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (j:Judgment {world_id:$w}) WHERE j.id IN $ids SET j.verdict=$verdict",
            w=world_id,
            ids=judgment_ids,
            verdict=verdict,
        ).consume()


def set_confirmation(driver, world_id, item_id, at):
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (i:Item {world_id:$w,id:$id}) "
            "SET i.last_confirmed=$at, i.recheck_by=$at",
            w=world_id,
            id=item_id,
            at=at,
        ).consume()
