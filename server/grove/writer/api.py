"""apply_batch: the one door into storage (D2).

The order: the caller's scopes (a caller with no write scope is refused before the store is
reached), the caller kind each operation lists, the batch's form, then one write transaction.
A failed commit raises WriteFailed and never returns a receipt; a receipt is built only after the
commit, from a fresh read of what committed. A connection lost during the commit raises
WriteOutcomeUnknown, and a receipt that cannot be read after a commit raises ReceiptUnavailable:
neither says "nothing written", so a caller never retries either blind.
"""

from typing import Any

from neo4j import Driver
from neo4j.exceptions import DriverError, IncompleteCommit, Neo4jError

from grove import clock
from grove.ids import new_id
from grove.store.scope import check_world_id
from grove.writer import apply as apply_module
from grove.writer.caller import Caller
from grove.writer.errors import (
    BatchDeclined,
    ReceiptUnavailable,
    Refusal,
    WriteFailed,
    WriteOutcomeUnknown,
)
from grove.writer.history import Stamp
from grove.writer.ops import OPS, batch_ops, op_label, shape_refusals
from grove.writer.receipts import Receipt, read_receipt
from grove.writer.tx import WorldTx, WorldWriter


def _declined(refusals: list[Refusal], size: int, request_id: str | None) -> BatchDeclined:
    return BatchDeclined(refusals, batch_size=size, request_id=request_id)


def _everyone_refused(ops: list[Any], code: str, rule: str, message: str) -> list[Refusal]:
    if not ops:
        return [Refusal(None, None, code, rule, message, {})]
    return [Refusal(*op_label(op), code, rule, message, {}) for op in ops]


def _check_id(value: Any, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} is non-empty text or None")


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
    _check_id(request_id, "request_id")
    _check_id(run_id, "run_id")

    faults, by_index = shape_refusals(batch)
    if faults:
        raise _declined(faults, len(ops), request_id)
    pre: dict[int, Refusal] = {}
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
    if len(pre) == len(ops):
        raise _declined([pre[index] for index in sorted(pre)], len(ops), request_id)

    observations = {
        observation["ev_id"]: observation for observation in (batch.get("evidence") or [])
    }
    stamp = Stamp(
        world_id=world_id,
        at=clock.now(world_id),
        actor=caller.actor,
        commit_id=new_id(world_id),
        change_set_id=new_id(world_id),
        request_id=request_id,
        run_id=run_id,
    )

    def work(tx: WorldTx) -> None:
        apply_module.apply_ops(tx, stamp=stamp, ops=ops, observations=observations, pre_refused=pre)

    # Only a store or driver failure becomes WriteFailed (retryable). A fault in the writer's own
    # code (a ScopeError, a WorldLeak, a TypeError) propagates as itself, so a retry loop never
    # hides it. Each still leaves nothing written: the transaction rolls back on any exception.
    ids = {
        "commit_id": stamp.commit_id,
        "change_set_id": stamp.change_set_id,
        "request_id": request_id,
    }
    try:
        WorldWriter(driver, world_id).transaction(work)
    except BatchDeclined:
        raise
    except IncompleteCommit as error:
        # The driver lost the connection waiting for the commit's reply: it may have committed.
        raise WriteOutcomeUnknown(
            "the connection dropped during the commit: read the change set back before any retry",
            **ids,
        ) from error
    except (Neo4jError, DriverError) as error:
        raise WriteFailed(f"the batch did not commit: {type(error).__name__}") from error
    try:
        return read_receipt(driver, world_id, **ids)
    except (Neo4jError, DriverError) as error:
        raise ReceiptUnavailable(
            f"the batch committed; its receipt could not be read: {type(error).__name__}", **ids
        ) from error
