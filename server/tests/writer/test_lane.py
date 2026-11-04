"""D4 lane tests: the verification lane's pool and its batch (grove.store.lane).

Spec, D4 "How to test it": "In a store smaller than one batch, a planted claim past its horizon is
offered within one pass. A property test over a large synthetic pool shows every claim offered
within a bounded number of passes, which is what the reserved slice is for. A revision-due head
goes first." The order is D4's "The verification lane" paragraph; the batch of 12 and its reserved
third are Appendix C. Every name below is invented.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from grove.store import lane
from grove.store.connection import DATABASE
from grove.store.lane import (
    ABOUT_NOW,
    BATCH_SIZE,
    FIGURE,
    HEAD,
    HORIZON_LAPSED,
    HORIZON_MISSING,
    REVISION_DUE,
    Due,
    batch,
    is_due,
    passes_bound,
    pool,
    select,
)
from grove.store.primitives import KIND_BELIEF
from grove.store.query import WorldQuery
from grove.writer.testing import plant_link, plant_node
from grove.writer.worlds import create_world

NOW = datetime(2027, 3, 10, 9, 0, tzinfo=UTC)
PAST = NOW - timedelta(days=2)
FUTURE = NOW + timedelta(days=20)
FAR = datetime(2031, 1, 1, tzinfo=UTC)
_keys = count(1)


def names(driver, world_id: str, label: str) -> dict[str, str]:
    rows = WorldQuery(driver, world_id).read(f"MATCH (n:{label}) RETURN n.id AS id, n.name AS name")
    return {row["name"]: row["id"] for row in rows}


def item(driver, world_id: str, kind: str, title: str, **fields: Any) -> str:
    row = {
        "kind": kind,
        "title": title,
        "properties": "{}",
        "became_true_precision": "unknown",
        "recorded": NOW - timedelta(days=30),
        "version": 1,
        **fields,
    }
    return plant_node(driver, world_id, "Item", **row)["id"]


def task(driver, world_id: str, kinds: dict, title: str, **fields: Any) -> str:
    fields = {"status": "open", "status_role": "open", **fields}
    return item(driver, world_id, kinds["task"], title, **fields)


def link(driver, world_id: str, a: str, meaning: str, b: str, **fields: Any) -> str:
    row = {
        "meaning": meaning,
        "active_key": f"{world_id}|{a}|{meaning}|{b}|{next(_keys)}",
        "became_true_precision": "unknown",
        "recorded": NOW - timedelta(days=30),
        "version": 1,
        **fields,
    }
    return plant_link(driver, world_id, a, b, **row)["id"]


def evidence(driver, world_id: str, target: str, recorded: datetime, **fields: Any) -> str:
    row = {
        "target_type": "item",
        "target_id": target,
        "polarity": "supports",
        "active": True,
        "recorded": recorded,
        **fields,
    }
    return plant_node(driver, world_id, "Evidence", **row)["id"]


def setup(driver, world_id: str) -> tuple[dict, dict]:
    return names(driver, world_id, "Kind"), names(driver, world_id, "Meaning")


def ids(claims: list[Due]) -> list[str]:
    return [claim.claim_id for claim in claims]


# A store smaller than one batch.


def test_a_planted_claim_past_its_horizon_is_offered_within_one_pass(driver, seeded):
    world = seeded["world_id"]
    kinds, meanings = setup(driver, world)
    lapsed = task(driver, world, kinds, "Repaint the Brannock shed", recheck_by=PAST)
    task(driver, world, kinds, "Order Quillfeather seed", recheck_by=FUTURE)
    task(
        driver,
        world,
        kinds,
        "Mend the Ostrander fence",
        status="done",
        status_role="done",
        recheck_by=PAST,
    )
    item(driver, world, KIND_BELIEF, "Tamsin waters the beds at dusk", recheck_by=PAST)
    item(driver, world, kinds["trip"], "The Velmora trip", recheck_by=PAST)
    person = item(driver, world, kinds["person"], "Tamsin Oyelaran")
    place = item(driver, world, kinds["place"], "Velmora")
    link(driver, world, person, meanings["part of"], place)

    offered = batch(WorldQuery(driver, world), at=NOW)

    # The done task, the belief, the trip that has not begun, the task not yet due and the
    # permanent link are not claims the lane owes.
    assert ids(offered) == [lapsed]
    assert offered[0].reasons == (HORIZON_LAPSED,)
    assert offered[0].claim_class == ABOUT_NOW


def test_every_field_based_case_enters_the_pool(driver, seeded):
    world = seeded["world_id"]
    kinds, meanings = setup(driver, world)
    plant_node(
        driver,
        world,
        "PropertyName",
        kind=kinds["observation"],
        name="tank level",
        value_type="number",
        temporal_class="state",
        status="canonical",
        origin="minted",
        version=1,
    )
    work = task(driver, world, kinds, "Clear the Hollin drain", recheck_by=PAST)
    period = item(
        driver,
        world,
        kinds["period"],
        "The Kestrel harvest season",
        became_true_earliest=NOW - timedelta(days=9),
        became_true_latest=NOW - timedelta(days=9),
        became_true_precision="day",
    )
    figure = item(
        driver,
        world,
        kinds["observation"],
        "Cistern reading",
        properties='{"tank level": 412}',
        recheck_by=PAST,
    )
    plain_record = item(driver, world, kinds["observation"], "Rain on the Ferrow road")
    person = item(driver, world, kinds["person"], "Odalys Pennick")
    place = item(driver, world, kinds["place"], "Ferrow")
    lives = link(driver, world, person, meanings["lives in"], place)

    found = {claim.claim_id: claim for claim in pool(WorldQuery(driver, world), at=NOW)}

    assert set(found) == {work, period, figure, lives}
    assert plain_record not in found
    assert found[period].reasons == (HORIZON_MISSING,)
    assert found[figure].claim_class == FIGURE
    assert (found[lives].claim_type, found[lives].reasons) == ("link", (HORIZON_MISSING,))


# The order.


def test_a_revision_due_head_goes_first(driver, seeded):
    world = seeded["world_id"]
    kinds, meanings = setup(driver, world)
    hub = item(driver, world, kinds["person"], "Ilse Varga")
    crowd = []
    for n in range(5):
        claim = task(driver, world, kinds, f"Load-bearing chore {n}", recheck_by=PAST)
        for _ in range(3):
            link(driver, world, hub, meanings["depends on"], claim, recheck_by=FAR)
        crowd.append(claim)
    confirmed_at = NOW - timedelta(days=5)
    head = task(
        driver,
        world,
        kinds,
        "Sell the Marrow cart",
        recheck_by=FUTURE,
        last_confirmed=NOW - timedelta(days=11),
    )
    verification = plant_node(
        driver, world, "Verification", claim_id=head, at=confirmed_at, disposition="confirmed"
    )["id"]
    # verify writes its own pieces as rows at the commit: they mark nothing revision due.
    evidence(driver, world, head, confirmed_at)
    query = WorldQuery(driver, world)
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (i:Item {world_id: $w, id: $id}) SET i.confirmed_against = $v",
            w=world,
            id=head,
            v=verification,
        ).consume()
    assert head not in ids(pool(query, at=NOW))

    evidence(driver, world, head, NOW - timedelta(days=1), polarity="contradicts")
    lasting = item(
        driver, world, kinds["place"], "Marrow yard", last_confirmed=NOW - timedelta(days=40)
    )
    evidence(driver, world, lasting, NOW - timedelta(days=3))

    offered = batch(query, at=NOW)

    # Both are revision due; the open task is a claim about now, so it ranks before the place.
    assert ids(offered)[:2] == [head, lasting]
    assert offered[0].reasons == (REVISION_DUE,) and offered[0].claim_class == ABOUT_NOW
    assert offered[1].reasons == (REVISION_DUE,) and offered[1].claim_class == HEAD
    assert set(ids(offered)[2:]) == set(crowd)
    assert all(claim.consequence_links_in == 3 for claim in offered[2:])


def test_claims_about_now_before_figures_lapsed_before_missing_load_bearing_before_leaves(
    driver, seeded
):
    world = seeded["world_id"]
    kinds, meanings = setup(driver, world)
    plant_node(
        driver,
        world,
        "PropertyName",
        kind=kinds["observation"],
        name="well depth",
        value_type="number",
        temporal_class="state",
        status="canonical",
        origin="minted",
        version=1,
    )
    figure = item(
        driver,
        world,
        kinds["observation"],
        "Well gauge",
        properties='{"well depth": 7}',
        recheck_by=NOW - timedelta(days=90),
    )
    missing = task(driver, world, kinds, "Ask Corwin about the pump")
    never = task(driver, world, kinds, "Plant the Dunmore hedge", recheck_by=PAST)
    older = task(
        driver,
        world,
        kinds,
        "Fix the Dunmore gate",
        recheck_by=PAST,
        last_confirmed=NOW - timedelta(days=60),
    )
    newer = task(
        driver,
        world,
        kinds,
        "Paint the Dunmore sign",
        recheck_by=PAST,
        last_confirmed=NOW - timedelta(days=6),
    )
    bearing = task(
        driver,
        world,
        kinds,
        "Book the Dunmore crane",
        recheck_by=PAST,
        last_confirmed=NOW - timedelta(days=1),
    )
    other = task(driver, world, kinds, "Brief the crane crew", recheck_by=FUTURE)
    link(driver, world, other, meanings["depends on"], bearing, recheck_by=FAR)
    link(driver, world, other, meanings["blocks"], bearing, recheck_by=FAR)
    # A link out of the claim is no consequence link into it.
    link(driver, world, newer, meanings["depends on"], other, recheck_by=FAR)

    offered = batch(WorldQuery(driver, world), at=NOW)

    assert ids(offered) == [bearing, never, older, newer, missing, figure]
    assert [claim.consequence_links_in for claim in offered] == [2, 0, 0, 0, 0, 0]
    assert offered[-1].claim_class == FIGURE
    assert offered[-2].reasons == (HORIZON_MISSING,)
    assert not any(claim.reserved for claim in offered)


def test_a_full_batch_keeps_a_third_for_the_claims_unconfirmed_longest():
    young = [
        due(f"bearing-{n:02d}", links_in=2, last_confirmed=NOW - timedelta(days=1))
        for n in range(15)
    ]
    old = [due(f"leaf-{n}", last_confirmed=NOW - timedelta(days=100 + n)) for n in range(5)]

    picked = select(young + old, size=BATCH_SIZE)

    assert len(picked) == BATCH_SIZE
    assert [claim.reserved for claim in picked] == [False] * 8 + [True] * 4
    assert ids(picked[8:]) == ["leaf-4", "leaf-3", "leaf-2", "leaf-1"]
    assert ids(select(young + old, size=BATCH_SIZE, reserved=0)) == ids(young[:12])


# The property test: nothing starves.


def due(
    claim_id: str,
    *,
    links_in: int = 0,
    last_confirmed: datetime | None = None,
    recheck_by: datetime | None = PAST,
    recorded: datetime | None = NOW - timedelta(days=30),
) -> Due:
    return Due(
        claim_id=claim_id,
        claim_type="item",
        reasons=reasons_at(recheck_by, NOW),
        claim_class=ABOUT_NOW,
        recheck_by=recheck_by,
        last_confirmed=last_confirmed,
        recorded=recorded,
        consequence_links_in=links_in,
    )


def reasons_at(recheck_by: datetime | None, at: datetime) -> tuple[str, ...]:
    return (HORIZON_MISSING,) if recheck_by is None else (HORIZON_LAPSED,)


def synthetic_pool(seed: int, size: int = 60) -> list[Due]:
    """A third load-bearing, the rest leaves; random ages, some never confirmed, some with no
    horizon."""
    rng = random.Random(seed)
    claims = []
    for n in range(size):
        confirmed = None if rng.random() < 0.2 else NOW - timedelta(days=rng.randint(1, 400))
        recheck = None if rng.random() < 0.2 else NOW - timedelta(days=rng.randint(0, 30))
        claims.append(
            due(
                f"c{n:02d}",
                links_in=rng.randint(1, 4) if n < size // 3 else 0,
                last_confirmed=confirmed,
                recheck_by=recheck,
                recorded=NOW - timedelta(days=rng.randint(400, 900)),
            )
        )
    return claims


def simulate(claims: list[Due], passes: int, reserved: int | None) -> dict[str, int]:
    """Each pass offers a batch. An offered load-bearing claim is confirmed and stays due every
    pass (its horizon is one day, a pass a day); an offered leaf is deferred, which moves its
    re-check by past the run. Returns the first pass that offered each claim."""
    state = {claim.claim_id: claim for claim in claims}
    first: dict[str, int] = {}
    at = NOW
    for number in range(1, passes + 1):
        owed = [
            replace(claim, reasons=reasons_at(claim.recheck_by, at))
            for claim in state.values()
            if is_due(claim.recheck_by, at)
        ]
        for claim in select(owed, size=BATCH_SIZE, reserved=reserved):
            first.setdefault(claim.claim_id, number)
            if claim.load_bearing:
                state[claim.claim_id] = replace(
                    claim, last_confirmed=at, recheck_by=at + timedelta(days=1)
                )
            else:
                state[claim.claim_id] = replace(claim, recheck_by=at + timedelta(days=90))
        at += timedelta(days=1)
    return first


def test_every_claim_of_a_large_pool_is_offered_within_a_bounded_number_of_passes():
    bound = passes_bound(60)
    assert bound == 15
    for seed in range(40):
        claims = synthetic_pool(seed)
        first = simulate(claims, passes=bound, reserved=None)
        assert set(first) == {claim.claim_id for claim in claims}, seed
        starved = simulate(claims, passes=3 * bound, reserved=0)
        leaves = {claim.claim_id for claim in claims if not claim.load_bearing}
        # Twenty always-due load-bearing claims fill every batch of twelve: no leaf comes up.
        assert not leaves & set(starved), seed


def test_the_bound_holds_on_the_store(driver, seeded, make_world):
    """The same run against planted rows: pool() and batch() read the store each pass, and the
    test writes each disposition's effect on the horizon and the confirmation."""
    second = make_world()
    create_world(driver, second, owner_user_id="u_t_owner")
    bound = passes_bound(60)
    with_slice = run_on_store(driver, seeded["world_id"], bound, reserved=None)
    without = run_on_store(driver, second, 2 * bound, reserved=0)

    assert set(with_slice["first"]) == with_slice["all"]
    assert max(with_slice["first"].values()) <= bound
    assert not with_slice["leaves"] & set(without["first"])


def run_on_store(driver, world: str, passes: int, reserved: int | None) -> dict[str, Any]:
    kinds, meanings = setup(driver, world)
    rng = random.Random(7)
    hub = item(driver, world, kinds["person"], "Bastian Quell")
    claims, leaves = set(), set()
    for n in range(60):
        confirmed = None if n % 5 == 0 else NOW - timedelta(days=rng.randint(1, 400))
        claim = task(
            driver,
            world,
            kinds,
            f"Synthetic chore {n}",
            recheck_by=None if n % 7 == 0 else NOW - timedelta(days=rng.randint(0, 30)),
            **({"last_confirmed": confirmed} if confirmed else {}),
        )
        claims.add(claim)
        if n < 20:
            for _ in range(rng.randint(1, 3)):
                link(driver, world, hub, meanings["depends on"], claim, recheck_by=FAR)
        else:
            leaves.add(claim)
    query = WorldQuery(driver, world)
    first: dict[str, int] = {}
    at = NOW
    with driver.session(database=DATABASE) as session:
        for number in range(1, passes + 1):
            for claim in batch(query, at=at, reserved=reserved):
                first.setdefault(claim.claim_id, number)
                if claim.load_bearing:
                    columns = {"last_confirmed": at, "recheck_by": at + timedelta(days=1)}
                else:
                    columns = {"recheck_by": at + timedelta(days=90)}
                session.run(
                    "CYPHER 25 MATCH (i:Item {world_id: $w, id: $id}) SET i += $columns",
                    w=world,
                    id=claim.claim_id,
                    columns=columns,
                ).consume()
            at += timedelta(days=1)
    return {"first": first, "all": claims, "leaves": leaves}


def test_select_refuses_a_batch_with_no_room():
    for size, reserved in ((0, None), (12, 13), (12, -1)):
        try:
            select([], size=size, reserved=reserved)
        except ValueError:
            continue
        raise AssertionError((size, reserved))
    assert lane.reserved_for(BATCH_SIZE) == 4
