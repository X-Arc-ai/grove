"""apply_batch: the one door into storage (D2).

The order: the caller's scopes (a caller with no write scope is refused before the store is
reached), the ids, a read of the request row with no lock, the batch's form and the caller kind
each operation lists, then one write transaction that takes the world's lock first and commits the
batch and the request's `saved` together.

A failed commit raises WriteFailed and never returns a receipt; a receipt is built only after the
commit, from a fresh read of what committed. A connection lost during the commit raises
WriteOutcomeUnknown, and a receipt that cannot be read after a commit raises ReceiptUnavailable.
Neither says "nothing written", and a replay with the same request id and the same batch is safe
after both, because the request row says whether it committed. Every decline is logged, apart from
the refusals in README.md that never reached the request.
"""

from typing import Any

from neo4j import Driver
from neo4j.exceptions import DriverError, IncompleteCommit, Neo4jError

from grove import clock
from grove.ids import new_id
from grove.store.query import WorldQuery
from grove.store.scope import check_world_id
from grove.writer import apply as apply_module
from grove.writer.caller import Caller
from grove.writer.errors import (
    BatchDeclined,
    ReceiptUnavailable,
    Refusal,
    Refused,
    WriteFailed,
    WriteOutcomeUnknown,
)
from grove.writer.history import Stamp
from grove.writer.message_binding import check_batch_sources
from grove.writer.ops import OPS, batch_ops, op_label, shape_refusals
from grove.writer.receipts import Receipt, read_receipt
from grove.writer.requests import (
    APPLY,
    KEY_REUSED,
    REPAIR_SPENT,
    REPLAY_DECLINE,
    attach_receipt,
    batch_fingerprint,
    check_request_id,
    key_reused,
    log_decline,
    mark_saved,
    merge_row,
    note_unavailable,
    read_row,
    receipt_of,
    repair_spent,
    replay_rule,
    replayed_decline,
)
from grove.writer.tx import WorldTx, WorldWriter


class _Replay(Exception):
    """Raised inside the transaction when the replay rules end the batch under the lock. It rolls
    back, so a replay leaves the row and the world's lock writes as they were."""

    def __init__(self, row: dict[str, Any], answer: str) -> None:
        super().__init__(answer)
        self.row = row
        self.answer = answer


def _after_commit() -> None:
    """Runs right after the transaction returns, before the receipt read: a test replaces it to
    kill the process with the batch committed and no receipt attached."""


def _declined(refusals: list[Refusal], size: int, request_id: str | None) -> BatchDeclined:
    return BatchDeclined(refusals, batch_size=size, request_id=request_id)


def _everyone_refused(ops: list[Any], code: str, rule: str, message: str) -> list[Refusal]:
    if not ops:
        return [Refusal(None, None, code, rule, message, {})]
    return [Refusal(*op_label(op), code, rule, message, {}) for op in ops]


def _check_id(value: Any, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} is non-empty text or None")


def _answer_replay(
    driver: Driver, world_id: str, row: dict[str, Any], answer: str, size: int
) -> Receipt:
    """A receipt read fresh, or the refusal the replay rules name. Nothing here is logged."""
    request_id = row["id"]
    if answer == REPLAY_DECLINE:
        try:
            declined = replayed_decline(driver, world_id, row)
        except (Neo4jError, DriverError) as error:
            raise WriteFailed(
                f"the decline could not be read: {type(error).__name__}", request_id=request_id
            ) from error
        raise declined
    if answer == KEY_REUSED:
        raise key_reused(request_id, size)
    if answer == REPAIR_SPENT:
        raise repair_spent(request_id, size)
    try:
        receipt = receipt_of(driver, world_id, row)
    except (Neo4jError, DriverError) as error:
        raise ReceiptUnavailable(
            f"the batch committed; its receipt could not be read: {type(error).__name__}",
            commit_id=row["commit_id"],
            change_set_id=row["change_set_id"],
            request_id=request_id,
        ) from error
    if not row.get("receipt"):
        attach_receipt(driver, world_id, receipt)
    return receipt


def _log_decline(
    driver: Driver,
    world_id: str,
    caller: Caller,
    request_id: str,
    declined: BatchDeclined,
    fingerprint: str,
    run_id: str | None,
) -> None:
    """A log that fails must not mask the decline. A replay runs the rules and logs it again."""
    try:
        log_decline(
            driver, world_id, caller, request_id, declined, fingerprint=fingerprint, run_id=run_id
        )
    except (Neo4jError, DriverError):
        return


def apply_batch(
    driver: Driver,
    world_id: str,
    caller: Caller,
    batch: dict[str, Any],
    *,
    request_id: str | None = None,
    run_id: str | None = None,
) -> Receipt:
    if not isinstance(caller, Caller):
        raise TypeError("caller must be a Caller")
    ops = batch_ops(batch)
    if not caller.can_write:
        raise _declined(
            _everyone_refused(
                ops, "scope_denied", "write_scope", "the caller holds no write scope"
            ),
            len(ops),
            request_id,
        )
    check_world_id(world_id)
    check_request_id(request_id, required=False)
    _check_id(run_id, "run_id")
    fingerprint = batch_fingerprint(batch)

    def check_sources(reader, row=None):
        try:
            check_batch_sources(reader, batch, row)
        except Refused as refused:
            raise _declined([refused.refusal(None, None)], len(ops), request_id) from refused

    # Saved is terminal, so this read with no lock is safe, and a replay never waits for the world.
    if request_id is not None:
        try:
            row = read_row(driver, world_id, request_id)
        except (Neo4jError, DriverError) as error:
            raise WriteFailed(
                f"the request could not be read: {type(error).__name__}", request_id=request_id
            ) from error
        answer = replay_rule(row, fingerprint)
        if answer != APPLY:
            check_sources(WorldQuery(driver, world_id), row)
            return _answer_replay(driver, world_id, row, answer, len(ops))
    key = request_id if request_id is not None else new_id(world_id)

    faults, by_index = shape_refusals(batch)
    pre: dict[int, Refusal] = {}
    if not faults:
        for index, op in enumerate(ops):
            name = op.get("op") if isinstance(op, dict) else None
            if name in OPS and caller.kind not in OPS[name].callers:
                pre[index] = Refusal(
                    *op_label(op),
                    "scope_denied",
                    "caller_kind",
                    f"{name} does not accept a caller of kind {caller.kind}",
                    {"caller_kind": caller.kind},
                )
            elif index in by_index:
                pre[index] = by_index[index]
    if faults or len(pre) == len(ops):
        refusals = faults or [pre[index] for index in sorted(pre)]
        declined = _declined(refusals, len(ops), key)
        _log_decline(driver, world_id, caller, key, declined, fingerprint, run_id)
        raise declined

    observations = {
        observation["ev_id"]: observation for observation in (batch.get("evidence") or [])
    }
    stamp = Stamp(
        world_id=world_id,
        at=clock.now(world_id),
        actor=caller.actor,
        commit_id=new_id(world_id),
        change_set_id=new_id(world_id),
        request_id=key,
        run_id=run_id,
        caller_kind=caller.kind,
    )

    def work(tx: WorldTx) -> None:
        tx.lock_world()
        row, _ = merge_row(tx, caller, key, stamp.at)
        check_sources(tx, row)
        answer = replay_rule(row, fingerprint)
        if answer != APPLY:
            raise _Replay(row, answer)
        apply_module.apply_ops(
            tx,
            stamp=stamp,
            ops=ops,
            observations=observations,
            pre_refused=pre,
            before_commit=False,
            caller_kind=caller.kind,
        )
        check_sources(tx, row)
        mark_saved(tx, row, stamp, fingerprint, run_id)
        apply_module._before_commit()

    # Only a store or driver failure becomes WriteFailed (retryable). A fault in the writer's own
    # code (a ScopeError, a WorldLeak, a TypeError) propagates as itself, so a retry loop never
    # hides it. Each still leaves nothing written: the transaction rolls back on any exception.
    ids = {
        "commit_id": stamp.commit_id,
        "change_set_id": stamp.change_set_id,
        "request_id": key,
    }
    try:
        WorldWriter(driver, world_id).transaction(work)
    except _Replay as replay:
        return _answer_replay(driver, world_id, replay.row, replay.answer, len(ops))
    except BatchDeclined as declined:
        _log_decline(driver, world_id, caller, key, declined, fingerprint, run_id)
        raise
    except IncompleteCommit as error:
        # The driver lost the connection waiting for the commit's reply: it may have committed.
        raise WriteOutcomeUnknown(
            "the connection dropped during the commit: replay with the same request id and batch",
            **ids,
        ) from error
    except (Neo4jError, DriverError) as error:
        note_unavailable(driver, world_id, key, run_id)
        raise WriteFailed(
            f"the batch did not commit: {type(error).__name__}", request_id=key
        ) from error
    _after_commit()
    try:
        receipt = read_receipt(driver, world_id, **ids)
    except (Neo4jError, DriverError) as error:
        raise ReceiptUnavailable(
            f"the batch committed; its receipt could not be read: {type(error).__name__}", **ids
        ) from error
    attach_receipt(driver, world_id, receipt)
    return receipt
