"""One-use, target-bound person approval against the real world transaction lock."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from grove import clock
from grove.ids import new_id
from grove.store.query import WorldQuery
from grove.writer.errors import InvalidRecord
from grove.writer.forget_approvals import (
    consume_approval,
    mint_approval,
    target_digest,
    validate_target,
)
from grove.writer.testing import plant_session
from grove.writer.tx import WorldWriter


@pytest.fixture
def owner(driver, world):
    user = new_id()
    plant_session(driver, world, user_id=user)
    return user


def consume(driver, world, owner, token, target):
    return WorldWriter(driver, world).transaction(
        lambda tx: consume_approval(tx, token, target_digest(target), owner)
    )


def test_approval_holds_no_target_text_or_raw_token(driver, world, owner):
    target = {"type": "named_value", "value": "Juniper rehearsal phrase"}
    made = mint_approval(driver, world, owner, target)
    row = WorldQuery(driver, world).read("MATCH (a:ForgetApproval) RETURN a")[0]["a"]
    assert target["value"] not in str(row)
    assert made.token not in str(row)
    assert made.token not in repr(made)
    assert consume(driver, world, owner, made.token, target) == made.id
    with pytest.raises(InvalidRecord, match="approval_required"):
        consume(driver, world, owner, made.token, target)


def test_approval_is_bound_to_the_exact_target(driver, world, owner):
    target = {"type": "named_value", "value": "Juniper phrase"}
    made = mint_approval(driver, world, owner, target)
    with pytest.raises(InvalidRecord, match="approval_required"):
        consume(driver, world, owner, made.token, {**target, "value": "Cedar phrase"})
    assert consume(driver, world, owner, made.token, target) == made.id


def test_approval_is_bound_to_world_and_owner(driver, world, owner, make_world):
    target = {"type": "item", "item_id": new_id(world)}
    made = mint_approval(driver, world, owner, target)
    other = make_world()
    other_owner = new_id()
    plant_session(driver, other, user_id=other_owner)
    with pytest.raises(InvalidRecord, match="approval_required"):
        consume(driver, other, other_owner, made.token, target)
    with pytest.raises(InvalidRecord, match="approval_required"):
        consume(driver, world, other_owner, made.token, target)
    with pytest.raises(InvalidRecord, match="approval_required"):
        mint_approval(driver, world, other_owner, target)
    assert consume(driver, world, owner, made.token, target) == made.id


def test_expired_approval_is_refused_at_its_exact_deadline(driver, world, owner):
    held = clock.TestClock()
    held.set(datetime(2026, 11, 4, tzinfo=UTC))
    target = {"type": "named_value", "value": "Juniper phrase"}
    with clock.using(held):
        made = mint_approval(driver, world, owner, target)
        held.set(made.expires_at)
        with pytest.raises(InvalidRecord, match="approval_required"):
            consume(driver, world, owner, made.token, target)


def test_rollback_does_not_spend_the_approval(driver, world, owner):
    target = {"type": "item", "item_id": new_id(world)}
    made = mint_approval(driver, world, owner, target)

    def failed(tx):
        consume_approval(tx, made.token, target_digest(target), owner)
        raise RuntimeError("synthetic cascade refusal")

    with pytest.raises(RuntimeError, match="synthetic cascade refusal"):
        WorldWriter(driver, world).transaction(failed)
    assert consume(driver, world, owner, made.token, target) == made.id


def test_two_concurrent_consumers_cannot_spend_one_approval_twice(driver, world, owner):
    target = {"type": "named_value", "value": "Juniper phrase"}
    made = mint_approval(driver, world, owner, target)
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            consume(driver, world, owner, made.token, target)
            return "consumed"
        except InvalidRecord:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        assert sorted(f.result(timeout=30) for f in futures) == ["consumed", "refused"]


@pytest.mark.parametrize(
    "target",
    [
        None,
        {},
        {"type": "item", "item_id": "a title"},
        {"type": "named_value", "value": ""},
        {"type": "named_value", "value": "x" * 513},
        {"type": "named_value", "value": "a\nb"},
        {"type": "named_value", "value": "\ud800"},
        {
            "type": "source_span",
            "source_id": "00000000-0000-7000-8000-000000000001",
            "locator": "\udfff",
        },
        {"type": "named_value", "value": "x", "approved": True},
        {"type": "source_span", "source_id": "not-an-id"},
        {"type": "field_value", "item_id": "00000000-0000-7000-8000-000000000001", "field": "id"},
    ],
)
def test_target_shape_is_closed(target):
    with pytest.raises(InvalidRecord, match="invalid_arguments"):
        validate_target(target)


def test_target_digest_uses_canonical_key_order():
    assert target_digest({"type": "named_value", "value": "Juniper"}) == target_digest(
        {"value": "Juniper", "type": "named_value"}
    )
