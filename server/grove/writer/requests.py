"""The durable queue (D2): the request row, its state machine and the replay rules.

A request commits exactly once, in one batch, and `saved` moves in the same transaction as that
commit. A kill before the commit rolls everything back, so the next pass may extract again; a kill
after it leaves `saved`, so no pass runs again. Exactly one effect is a property of atomicity, not
of a model that re-reads what it did. A pass with more facts for the same words mints another id.

States: queued, applying, saved, declined, failed. `failed` with `retry_at` waits for a retry and
reads "saving"; `failed` with none is surfaced and reads "failed". Only a row enqueue_request wrote
(one with a `source_id`) can be claimed, so a direct apply_batch never leaves a claimable row.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError, TransientError

from grove import clock
from grove.store.enums import ENUMS
from grove.store.errors import ScopeError
from grove.store.query import PlatformQuery, WorldQuery
from grove.store.scope import check_world_id
from grove.writer.caller import Caller
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.declines import (
    candidates_by_op,
    declines_from,
    declines_of,
    rebuilt_decline,
    stored_decline,
    write_decline_rows,
)
from grove.writer.errors import (
    BatchDeclined,
    ReceiptUnavailable,
    Refusal,
    WriteFailed,
    WriteOutcomeUnknown,
)
from grove.writer.history import Stamp
from grove.writer.receipts import Receipt, read_receipt
from grove.writer.tx import WorldTx, WorldWriter

# S53: what a relayed request carries. An answer or a correction from a host needs the person's
# own words and the id of the host message that carried them (D5), or it is refused here.
RELAYS: tuple[str, ...] = ENUMS["relay"]
_ANSWER_RELAYS = frozenset({"answer", "correction"})


def _check_verbatim(
    caller: Caller, request_id: str, relay: str | None, verbatim: bool | None, message: Any
) -> None:
    if relay in _ANSWER_RELAYS and caller.kind not in ("person", "ai_host"):
        # Only the person, or the AI host relaying the person, answers or corrects (D5, S53).
        raise _refused(
            "scope_denied",
            "relay_caller",
            "an answer or a correction comes from the person or the AI host only",
            request_id,
        )
    if caller.kind != "ai_host" or relay not in _ANSWER_RELAYS:
        return
    if verbatim is not True or not isinstance(message, str) or not message.strip():
        raise _refused(
            "verbatim_required",
            "verbatim_required",
            "an answer or a correction from a host carries the person's own words "
            "(verbatim true) and the id of the host message",
            request_id,
        )


MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (30, 60, 120, 240)
LEASE_SECONDS = 300
QUEUE_WORKERS = frozenset({"update", "cleanup", "consolidation"})
WORKER_SCOPE = "grove.propose"

KEY_REUSED = "request_key_reused"
REPAIR_SPENT = "repair_spent"
LEASE_LOST = "lease_lost"
QUEUE_WORKER = "queue_worker"
UNAVAILABLE = "unavailable"
OUTCOME_UNKNOWN = "outcome_unknown"
RECEIPT_UNAVAILABLE = "receipt_unavailable"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
VERSION_CONFLICT = "version_conflict"

# What the replay rules answer.
APPLY = "apply"
REPLAY_SAVED = "replay_saved"
REPLAY_DECLINE = "replay_decline"

# A key lands on change entries and a receipt prints part of it, so it holds no text.
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_CLAIM_WINDOW = 20
_CLEAR_CLAIM = {"retry_at": None, "lease_until": None, "claimed_by": None}
_FAILABLE = ("queued", "applying", "failed")

_READ = "MATCH (r:Request {id: $id}) RETURN r"
# Every callback already holds the World lock, including unchanged request replays.
_LOCK = _READ
_CREATE = "CREATE (r:Request {id: $id}) SET r += $row RETURN r"
_UPDATE = "MATCH (r:Request {id: $id}) SET r += $columns, r.version = coalesce(r.version, 0) + 1"
_ATTACH = (
    "MATCH (r:Request {id: $id}) "
    "WITH r WHERE r.protocol IS NULL AND r.status = 'saved' AND r.receipt IS NULL "
    "SET r.receipt = $receipt, r.touched_at = $at, r.version = coalesce(r.version, 0) + 1"
)
# The one due predicate: claim, pending_requests and worlds_with_due_requests all read it.
_DUE = (
    "r.protocol IS NULL AND r.source_id IS NOT NULL AND ("
    "(r.status = 'queued' AND (r.retry_at IS NULL OR r.retry_at <= $now)) OR "
    "(r.status = 'failed' AND r.retry_at IS NOT NULL AND r.retry_at <= $now) OR "
    "(r.status = 'applying' AND (r.lease_until IS NULL OR r.lease_until <= $now)))"
)
_READ_DUE = "MATCH (r:Request) WHERE " + _DUE + " RETURN r ORDER BY r.created_at, r.id LIMIT $limit"
_LOCK_IF_DUE = "MATCH (r:Request {id: $id}) WITH r WHERE " + _DUE + " RETURN r"


# Checks that need no store -------------------------------------------------------------------


def check_request_id(value: Any, *, required: bool = True) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("a request id is 1 to 128 of A-Z a-z 0-9 _ . : -")


def _check_code(value: Any, name: str) -> None:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError(f"{name} is a code: 1 to 128 of A-Z a-z 0-9 _ . : -")


def _check_limit(limit: Any) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit is a positive whole number")


def _refused(
    code: str, rule: str, message: str, request_id: str | None, size: int = 0
) -> BatchDeclined:
    return BatchDeclined(
        [Refusal(None, None, code, rule, message, {})], batch_size=size, request_id=request_id
    )


def _need_write(caller: Caller, request_id: Any) -> None:
    if not isinstance(caller, Caller):
        raise TypeError("caller must be a Caller")
    if not caller.can_write:
        raise _refused(
            "scope_denied",
            "write_scope",
            "the caller holds no write scope",
            request_id if isinstance(request_id, str) else None,
        )


def _need_worker(caller: Caller, request_id: Any) -> None:
    if not isinstance(caller, Caller):
        raise TypeError("caller must be a Caller")
    if caller.kind not in QUEUE_WORKERS or WORKER_SCOPE not in caller.scopes:
        raise _refused(
            "scope_denied",
            QUEUE_WORKER,
            "only a queue worker with the scope grove.propose works the queue",
            request_id if isinstance(request_id, str) else None,
        )


def _unknown_request(request_id: str) -> BatchDeclined:
    return _refused("not_found", "request_not_found", "no such request", request_id)


def refuse_protected_request(row: dict | None) -> None:
    """A protected request can only use its typed writer, even for saved replay."""
    if row is not None and row.get("protocol") is not None:
        raise _refused(
            "scope_denied",
            "protected_request",
            "the request requires its protected writer",
            row.get("id"),
        )


def batch_fingerprint(batch: Any) -> str:
    """What a replay compares. A batch the shape check will refuse needs a fingerprint too."""
    try:
        text = json.dumps(batch, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(batch)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def next_retry(attempts: int, at: datetime) -> datetime:
    index = min(max(attempts, 1) - 1, len(BACKOFF_SECONDS) - 1)
    return at + timedelta(seconds=BACKOFF_SECONDS[index])


# The row --------------------------------------------------------------------------------------


def read_row(driver: Driver, world_id: str, request_id: str) -> dict[str, Any] | None:
    rows = WorldQuery(driver, world_id).read(_READ, id=request_id)
    return rows[0]["r"] if rows else None


def merge_row(
    tx: WorldTx, caller: Caller, request_id: str, at: datetime, **columns: Any
) -> tuple[dict[str, Any], bool]:
    """The row, under its lock, and whether this transaction created it. A new row's version is 0
    unless the caller says otherwise: its first status update makes it 1."""
    existing = tx.run(_READ, id=request_id)
    if existing:
        refuse_protected_request(existing[0]["r"])
        return existing[0]["r"], False
    row = {
        "caller": caller.actor,
        "status": "queued",
        "attempts": 0,
        "repairs": 0,
        "created_at": at,
        "version": 0,
        "touched_at": at,
        **{key: value for key, value in columns.items() if value is not None},
    }
    found = tx.run(_CREATE, id=request_id, row=row)[0]
    return found["r"], True


def _attempts_after(row: dict[str, Any]) -> int:
    """A claim counted its attempt already; anything else counts one here."""
    attempts = row.get("attempts") or 0
    return attempts if row.get("status") == "applying" else attempts + 1


def replay_rule(row: dict[str, Any] | None, fingerprint: str) -> str:
    """What a batch with this fingerprint may do to this row. Runs twice: with no lock before the
    world lock, so N replays of a saved request never queue behind it, and under the lock, because
    the first read can be stale."""
    refuse_protected_request(row)
    status = row.get("status") if row else None
    if status == "saved":
        return REPLAY_SAVED if row.get("fingerprint") == fingerprint else KEY_REUSED
    if status == "declined":
        if row.get("declined_fingerprint") == fingerprint:
            return REPLAY_DECLINE
        return APPLY if not row.get("repairs") else REPAIR_SPENT
    return APPLY


def key_reused(request_id: str, size: int) -> BatchDeclined:
    return _refused(
        "invalid_arguments",
        KEY_REUSED,
        "this request id already holds other content; a new batch needs a new id",
        request_id,
        size,
    )


def repair_spent(request_id: str, size: int) -> BatchDeclined:
    return _refused(
        "invalid_arguments",
        REPAIR_SPENT,
        "this request already used its one repair attempt",
        request_id,
        size,
    )


def replayed_decline(driver: Driver, world_id: str, row: dict[str, Any]) -> BatchDeclined:
    stored = json.loads(row["receipt"])
    return rebuilt_decline(stored, candidates_by_op(WorldQuery(driver, world_id), row["id"]))


def mark_saved(
    tx: WorldTx, row: dict[str, Any], stamp: Stamp, fingerprint: str, run_id: str | None
) -> None:
    """In the same transaction as the batch. The receipt attaches after it: a receipt is a fresh
    read of what committed, and a saved row with none is legal after a crash."""
    refuse_protected_request(row)
    columns = {
        "status": "saved",
        "commit_id": stamp.commit_id,
        "change_set_id": stamp.change_set_id,
        "fingerprint": fingerprint,
        "committed_at": stamp.at,
        "attempts": _attempts_after(row),
        "receipt": None,
        "declined_fingerprint": None,
        "last_error_code": None,
        "touched_at": stamp.at,
        **_CLEAR_CLAIM,
    }
    if run_id is not None:
        columns["run_id"] = run_id
    tx.run(_UPDATE, id=row["id"], columns=columns)


def attach_receipt(driver: Driver, world_id: str, receipt: Receipt) -> None:
    """Best effort: a saved row with no receipt reads back from its change set, so a failed attach
    never fails the call that saved."""
    text = json.dumps(receipt.to_json(), sort_keys=True)

    def work(tx: WorldTx) -> None:
        tx.run(_ATTACH, id=receipt.request_id, receipt=text, at=clock.now(world_id))

    try:
        WorldWriter(driver, world_id).transaction(work)
    except (Neo4jError, DriverError):
        return


def receipt_of(driver: Driver, world_id: str, row: dict[str, Any]) -> Receipt | None:
    """A saved row's receipt: the stored one if it is a receipt, or else a fresh read of the
    change set. The receipt column of a request that was declined holds a decline."""
    refuse_protected_request(row)
    stored = json.loads(row["receipt"]) if row.get("receipt") else None
    if stored is not None and stored.get("state") == "saved":
        return Receipt.from_json(stored)
    if not row.get("change_set_id"):
        return None
    return read_receipt(
        driver,
        world_id,
        commit_id=row["commit_id"],
        change_set_id=row["change_set_id"],
        request_id=row["id"],
    )


def log_decline(
    driver: Driver,
    world_id: str,
    caller: Caller,
    request_id: str,
    declined: BatchDeclined,
    *,
    fingerprint: str,
    run_id: str | None,
) -> None:
    """The one log transaction, under the row lock, after the batch rolled back. A stale version
    takes the row back to the queue if it was enqueued; any other decline moves it to declined. On
    a saved row it changes nothing, and a batch that was declined before logs nothing again."""
    at = clock.now(world_id)
    refusals = declined.refusals
    stored = json.dumps(stored_decline(declined), sort_keys=True)

    def conflict(tx: WorldTx) -> None:
        rows = tx.run(_LOCK, id=request_id, at=at)
        row = rows[0]["r"] if rows else None
        if row is None or not row.get("source_id"):
            write_decline_rows(tx, request_id, refusals, at)
            return
        if row.get("status") == "saved":
            return
        write_decline_rows(tx, request_id, refusals, at)
        attempts = _attempts_after(row)
        columns = {
            "status": "queued",
            "attempts": attempts,
            "last_error_code": VERSION_CONFLICT,
            # A conflict on a repair used that repair: the row must not earn a second one.
            "repairs": 1 if row.get("status") == "declined" else row.get("repairs") or 0,
            "receipt": None,
            "declined_fingerprint": None,
            "touched_at": at,
            **_CLEAR_CLAIM,
            "retry_at": next_retry(attempts, at),
        }
        if run_id is not None:
            columns["run_id"] = run_id
        tx.run(_UPDATE, id=request_id, columns=columns)

    def decline(tx: WorldTx) -> None:
        row, _ = merge_row(tx, caller, request_id, at)
        status = row.get("status")
        repairs = row.get("repairs") or 0
        if status == "saved":
            return
        if status == "declined":
            if row.get("declined_fingerprint") == fingerprint or repairs:
                return
            repairs = 1
        write_decline_rows(tx, request_id, refusals, at)
        columns = {
            "status": "declined",
            "declined_fingerprint": fingerprint,
            "receipt": stored,
            "last_error_code": refusals[0].code,
            "attempts": _attempts_after(row),
            "repairs": repairs,
            "touched_at": at,
            **_CLEAR_CLAIM,
        }
        if run_id is not None:
            columns["run_id"] = run_id
        tx.run(_UPDATE, id=request_id, columns=columns)

    WorldWriter(driver, world_id).transaction(conflict if declined.retryable else decline)


def note_unavailable(driver: Driver, world_id: str, request_id: str, run_id: str | None) -> None:
    """Best effort after a failed commit: an enqueued row waits for a retry. A failed write here is
    harmless, because the lease brings the row back. A direct call has no row and writes none."""
    at = clock.now(world_id)

    def work(tx: WorldTx) -> None:
        rows = tx.run(_LOCK, id=request_id, at=at)
        if not rows:
            return
        row = rows[0]["r"]
        refuse_protected_request(row)
        if not row.get("source_id") or row.get("status") not in _FAILABLE:
            return
        attempts = _attempts_after(row)
        columns = {
            "status": "failed",
            "attempts": attempts,
            "last_error_code": UNAVAILABLE,
            "touched_at": at,
            **_CLEAR_CLAIM,
            "retry_at": next_retry(attempts, at),
        }
        if run_id is not None:
            columns["run_id"] = run_id
        tx.run(_UPDATE, id=request_id, columns=columns)

    try:
        WorldWriter(driver, world_id).transaction(work)
    except (Neo4jError, DriverError):
        return


# States ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestState:
    request_id: str
    status: str
    receipt: Receipt | None = None
    declines: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    last_error_code: str | None = None
    source_id: str | None = None
    run_id: str | None = None
    retry_at: datetime | None = None

    @property
    def api_state(self) -> str:
        """Saved only with a receipt in hand (D2). `failed` with a retry_at still reads saving."""
        if self.status == "saved" and self.receipt is not None:
            return "saved"
        if self.status == "declined":
            return "declined"
        if self.status == "failed" and self.retry_at is None:
            return "failed"
        return "saving"

    def to_api(self) -> dict[str, Any]:
        """B.3's answer. hosts-b renders a receipt through render_receipt."""
        state = self.api_state
        answer: dict[str, Any] = {"state": state, "request_id": self.request_id}
        if state == "saved":
            answer["receipt"] = self.receipt.to_json()
        if state == "declined":
            answer["declines"] = list(self.declines)
        if self.last_error_code:
            answer["last_error_code"] = self.last_error_code
        return answer


@dataclass(frozen=True)
class ClaimedRequest:
    id: str
    source_id: str
    run_id: str | None
    attempts: int
    caller: str
    last_error_code: str | None = None
    verbatim: bool | None = None
    host_message_id: str | None = None
    said_at: datetime | None = None
    about: list[str] | None = None
    relay: str | None = None


def _state_of(row: dict[str, Any], **parts: Any) -> RequestState:
    return RequestState(
        request_id=row["id"],
        status=row["status"],
        attempts=row.get("attempts") or 0,
        last_error_code=row.get("last_error_code"),
        source_id=row.get("source_id"),
        run_id=row.get("run_id"),
        retry_at=row.get("retry_at"),
        **parts,
    )


def _full_state(driver: Driver, world_id: str, row: dict[str, Any]) -> RequestState:
    if row["status"] == "saved":
        return _state_of(row, receipt=receipt_of(driver, world_id, row))
    if row["status"] == "declined" and row.get("receipt"):
        candidates = candidates_by_op(WorldQuery(driver, world_id), row["id"])
        return _state_of(row, declines=declines_of(json.loads(row["receipt"]), candidates))
    return _state_of(row)


def request_state(driver: Driver, world_id: str, request_id: str) -> RequestState | None:
    """A pure read, for GET /v1/requests/{id} and for a host's wait."""
    check_world_id(world_id)
    check_request_id(request_id)
    row = read_row(driver, world_id, request_id)
    return None if row is None else _full_state(driver, world_id, row)


# Enqueue -------------------------------------------------------------------------------------


def _enqueue_columns(
    source_id: str | None,
    write_source: Callable[[WorldTx], str] | None,
    content_key: str | None,
    run_id: str | None,
    verbatim: bool | None,
    host_message_id: str | None,
    said_at: datetime | str | None,
    about: list[str] | None,
    relay: str | None = None,
) -> dict[str, Any]:
    if (source_id is None) == (write_source is None):
        raise ValueError("an enqueue takes source_id or write_source, exactly one")
    if source_id is not None and content_key is not None:
        raise ValueError("with source_id, the content key is the source id")
    key = source_id if source_id is not None else content_key
    _check_code(key, "content_key")
    if run_id is not None:
        _check_code(run_id, "run_id")
    if verbatim is not None and not isinstance(verbatim, bool):
        raise ValueError("verbatim is true or false")
    if host_message_id is not None and (
        not isinstance(host_message_id, str) or not host_message_id.strip()
    ):
        raise ValueError("host_message_id is non-empty text")
    when = None
    if said_at is not None:
        try:
            when = parse_instant(said_at)
        except ClockError as error:
            raise ValueError(f"said_at: {error}") from error
    if about is not None and (
        not isinstance(about, list) or not all(isinstance(entry, str) for entry in about)
    ):
        raise ValueError("about is a list of ids")
    if relay is not None and relay not in RELAYS:
        raise ValueError(f"relay is one of {list(RELAYS)}")
    return {
        "relay": relay,
        "content_key": key,
        "run_id": run_id,
        "verbatim": verbatim,
        "host_message_id": host_message_id,
        "said_at": when,
        "about": about or None,
    }


def write_request_row(
    tx: WorldTx,
    caller: Caller,
    request_id: str,
    *,
    source_id: str | None = None,
    write_source: Callable[[WorldTx], str] | None = None,
    content_key: str | None = None,
    run_id: str | None = None,
    verbatim: bool | None = None,
    host_message_id: str | None = None,
    said_at: datetime | str | None = None,
    about: list[str] | None = None,
    relay: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """The enqueue, for a caller that holds a transaction. `write_source` runs only when the row is
    new, so one key stores its words once. A second call with the same content writes nothing, and
    other content under the key raises BatchDeclined. Returns (row, fresh)."""
    _need_write(caller, request_id)
    check_request_id(request_id)
    tx.lock_world()
    columns = _enqueue_columns(
        source_id,
        write_source,
        content_key,
        run_id,
        verbatim,
        host_message_id,
        said_at,
        about,
        relay,
    )
    _check_verbatim(caller, request_id, relay, verbatim, host_message_id)
    row, fresh = merge_row(tx, caller, request_id, clock.now(tx.world_id), version=1, **columns)
    if not fresh:
        if row.get("content_key") != columns["content_key"]:
            raise key_reused(request_id, 0)
        return row, False
    if source_id is not None:
        if not tx.run("MATCH (s:Source {id: $id}) RETURN s.id AS id", id=source_id):
            raise _refused("not_found", "source_not_found", "no such source", request_id)
        written = source_id
    else:
        written = write_source(tx)
    tx.run("MATCH (r:Request {id: $id}) SET r.source_id = $source", id=request_id, source=written)
    return {**row, "source_id": written}, True


def enqueue_request(
    driver: Driver,
    world_id: str,
    caller: Caller,
    request_id: str,
    *,
    source_id: str | None = None,
    write_source: Callable[[WorldTx], str] | None = None,
    content_key: str | None = None,
    run_id: str | None = None,
    verbatim: bool | None = None,
    host_message_id: str | None = None,
    said_at: datetime | str | None = None,
    about: list[str] | None = None,
    relay: str | None = None,
) -> RequestState:
    """The request is durable once this returns. A retry with the same key is always safe, so a
    store failure raises WriteFailed, also when the connection dropped during the commit."""
    _need_write(caller, request_id)
    check_world_id(world_id)
    check_request_id(request_id)
    _enqueue_columns(
        source_id,
        write_source,
        content_key,
        run_id,
        verbatim,
        host_message_id,
        said_at,
        about,
        relay,
    )
    _check_verbatim(caller, request_id, relay, verbatim, host_message_id)

    def work(tx: WorldTx) -> dict[str, Any]:
        row, _ = write_request_row(
            tx,
            caller,
            request_id,
            source_id=source_id,
            write_source=write_source,
            content_key=content_key,
            run_id=run_id,
            verbatim=verbatim,
            host_message_id=host_message_id,
            said_at=said_at,
            about=about,
            relay=relay,
        )
        return row

    try:
        row = WorldWriter(driver, world_id).transaction(work)
        return _full_state(driver, world_id, row)
    except (Neo4jError, DriverError) as error:
        raise WriteFailed(
            f"the enqueue did not finish: {type(error).__name__}", request_id=request_id
        ) from error


# Claim, finish and fail ------------------------------------------------------------------------


def _claim_one(
    tx: WorldTx, request_id: str, worker: str, now: datetime, lease: timedelta
) -> ClaimedRequest | None:
    rows = tx.run(_LOCK_IF_DUE, id=request_id, now=now)
    if not rows:
        return None
    row = rows[0]["r"]
    refuse_protected_request(row)
    attempts = row.get("attempts") or 0
    if attempts >= MAX_ATTEMPTS:
        tx.run(
            _UPDATE,
            id=request_id,
            columns={
                "status": "failed",
                "last_error_code": ATTEMPTS_EXHAUSTED,
                "touched_at": now,
                **_CLEAR_CLAIM,
            },
        )
        return None
    tx.run(
        _UPDATE,
        id=request_id,
        columns={
            "status": "applying",
            "attempts": attempts + 1,
            "touched_at": now,
            **_CLEAR_CLAIM,
            "lease_until": now + lease,
            "claimed_by": worker,
        },
    )
    return ClaimedRequest(
        id=request_id,
        source_id=row["source_id"],
        run_id=row.get("run_id"),
        attempts=attempts + 1,
        caller=row.get("caller"),
        last_error_code=row.get("last_error_code"),
        verbatim=row.get("verbatim"),
        host_message_id=row.get("host_message_id"),
        said_at=row.get("said_at"),
        about=row.get("about"),
        relay=row.get("relay"),
    )


def claim_next_request(
    driver: Driver,
    world_id: str,
    caller: Caller,
    *,
    worker: str,
    lease_seconds: float = LEASE_SECONDS,
) -> ClaimedRequest | None:
    """The oldest due row. The candidates are read with no lock, so each claim checks due again
    under the row's lock, and the loser of a race moves on to the next row."""
    _need_worker(caller, None)
    check_world_id(world_id)
    _check_code(worker, "worker")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int | float):
        raise ValueError("lease_seconds is a number of seconds")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds is a number of seconds")
    now = clock.now(world_id)
    lease = timedelta(seconds=lease_seconds)
    try:
        candidates = WorldQuery(driver, world_id).read(_READ_DUE, now=now, limit=_CLAIM_WINDOW)
        writer = WorldWriter(driver, world_id)
        for candidate in candidates:
            request_id = candidate["r"]["id"]
            try:
                claimed = writer.transaction(
                    lambda tx, key=request_id: _claim_one(tx, key, worker, now, lease)
                )
            except Neo4jError as error:
                if _lock_wait_ended(error):
                    continue
                raise
            if claimed is not None:
                return claimed
    except (Neo4jError, DriverError) as error:
        raise WriteFailed(f"the claim did not finish: {type(error).__name__}") from error
    return None


def _lock_wait_ended(error: Neo4jError) -> bool:
    """Another transaction holds this row: a busy row is no failure of the queue."""
    return isinstance(error, TransientError) or ".Transaction." in (error.code or "")


def finish_request(
    driver: Driver, world_id: str, caller: Caller, request_id: str, *, worker: str
) -> RequestState:
    """A pass that found nothing to write: applying to saved, with no commit. Only the holder may
    do it, because the receipt says the pass ran. Any other status comes back unchanged."""
    _need_worker(caller, request_id)
    check_world_id(world_id)
    check_request_id(request_id)
    _check_code(worker, "worker")
    at = clock.now(world_id)

    def work(tx: WorldTx) -> dict[str, Any]:
        rows = tx.run(_LOCK, id=request_id, at=at)
        if not rows:
            raise _unknown_request(request_id)
        row = rows[0]["r"]
        refuse_protected_request(row)
        # A successful no-op must not discard source work withheld from the Learner.
        from grove.learner_input import select_input

        if row.get("source_id") is None and row.get("status") != "applying":
            return row
        sources = tx.run("MATCH (s:Source {id: $id}) RETURN s", id=row.get("source_id"))
        if not sources:
            raise _refused(
                "missing_provenance", "source_work_pending", "the source is unavailable", request_id
            )
        source = sources[0]["s"]
        connectors = tx.run("MATCH (c:Connector {id: $id}) RETURN c", id=source.get("connector_id"))
        if not connectors or not select_input(source, connectors[0]["c"], row).complete:
            raise _refused(
                "missing_provenance",
                "source_work_pending",
                "required source work is held or unavailable",
                request_id,
            )
        if row.get("status") != "applying":
            return row
        _holder(row, worker)
        receipt = json.dumps(
            Receipt("saved", None, None, request_id).to_json(), sort_keys=True, ensure_ascii=False
        )
        columns = {
            "status": "saved",
            "receipt": receipt,
            "declined_fingerprint": None,
            "last_error_code": None,
            "touched_at": at,
            **_CLEAR_CLAIM,
        }
        tx.run(_UPDATE, id=request_id, columns=columns)
        return {**row, **columns}

    return _full_state(driver, world_id, _worked(driver, world_id, request_id, work))


def fail_request(
    driver: Driver,
    world_id: str,
    caller: Caller,
    request_id: str,
    code: str,
    *,
    worker: str,
    retryable: bool = True,
) -> RequestState:
    """A pass that failed: applying to failed, by the holder only. With `retryable` and attempts
    left it waits for a retry, and otherwise it is surfaced. Any other status is left unchanged."""
    _need_worker(caller, request_id)
    check_world_id(world_id)
    check_request_id(request_id)
    _check_code(worker, "worker")
    _check_code(code, "code")
    at = clock.now(world_id)

    def work(tx: WorldTx) -> dict[str, Any]:
        rows = tx.run(_LOCK, id=request_id, at=at)
        if not rows:
            raise _unknown_request(request_id)
        row = rows[0]["r"]
        refuse_protected_request(row)
        if row.get("status") != "applying":
            return row
        _holder(row, worker)
        attempts = row.get("attempts") or 0
        columns = {
            "status": "failed",
            "last_error_code": code,
            "touched_at": at,
            **_CLEAR_CLAIM,
        }
        if retryable and attempts < MAX_ATTEMPTS:
            columns["retry_at"] = next_retry(attempts, at)
        tx.run(_UPDATE, id=request_id, columns=columns)
        return {**row, **columns}

    return _full_state(driver, world_id, _worked(driver, world_id, request_id, work))


def _holder(row: dict[str, Any], worker: str) -> None:
    if row.get("claimed_by") != worker:
        raise _refused(
            "invalid_arguments",
            LEASE_LOST,
            "another worker holds this request's lease",
            row["id"],
        )


def _worked(
    driver: Driver, world_id: str, request_id: str, work: Callable[[WorldTx], dict[str, Any]]
) -> dict[str, Any]:
    try:
        return WorldWriter(driver, world_id).transaction(work)
    except (Neo4jError, DriverError) as error:
        raise WriteFailed(
            f"the change did not finish: {type(error).__name__}", request_id=request_id
        ) from error


# Applying a batch under a request -------------------------------------------------------------


def _passes_through(declined: BatchDeclined) -> bool:
    return any(
        refusal.rule in (KEY_REUSED, REPAIR_SPENT) or refusal.code == "scope_denied"
        for refusal in declined.refusals
    )


def _state_after_failure(
    driver: Driver, world_id: str, before: dict[str, Any], code: str
) -> RequestState:
    """No receipt, whatever the row says, so this reads "saving": the fresh read behind a "saved"
    just failed. The store may be down still, so the row read before the batch is the fallback."""
    return replace(_state_of(_row_or(driver, world_id, before)), last_error_code=code)


def _state_after_decline(
    driver: Driver, world_id: str, before: dict[str, Any], declined: BatchDeclined
) -> RequestState:
    """The row as the log left it. A log failure never hides the decline from this answer."""
    state = _state_of(_row_or(driver, world_id, before))
    if declined.retryable:
        return replace(state, last_error_code=VERSION_CONFLICT)
    return replace(
        state,
        status="declined",
        declines=declines_from(declined),
        last_error_code=declined.refusals[0].code,
    )


def _row_or(driver: Driver, world_id: str, before: dict[str, Any]) -> dict[str, Any]:
    try:
        return read_row(driver, world_id, before["id"]) or before
    except (Neo4jError, DriverError):
        return before


def apply_request(
    driver: Driver,
    world_id: str,
    caller: Caller,
    request_id: str,
    batch: dict[str, Any],
    *,
    run_id: str | None = None,
) -> RequestState:
    """apply_batch under an enqueued request, with its outcomes as states: only an enqueue makes a
    request that can read "saving". A refusal of the key or of the caller propagates, and a writer
    fault propagates as itself."""
    # api.py imports this module for the row functions, so this one import comes late.
    from grove.writer.api import apply_batch

    _need_write(caller, request_id)
    check_world_id(world_id)
    check_request_id(request_id)
    try:
        before = read_row(driver, world_id, request_id)
    except (Neo4jError, DriverError) as error:
        raise WriteFailed(
            f"the request could not be read: {type(error).__name__}", request_id=request_id
        ) from error
    if before is None:
        raise _unknown_request(request_id)
    refuse_protected_request(before)
    try:
        receipt = apply_batch(driver, world_id, caller, batch, request_id=request_id, run_id=run_id)
    except BatchDeclined as declined:
        if _passes_through(declined):
            raise
        return _state_after_decline(driver, world_id, before, declined)
    except WriteFailed:
        return _state_after_failure(driver, world_id, before, UNAVAILABLE)
    except WriteOutcomeUnknown:
        return _state_after_failure(driver, world_id, before, OUTCOME_UNKNOWN)
    except ReceiptUnavailable:
        return _state_after_failure(driver, world_id, before, RECEIPT_UNAVAILABLE)
    return replace(_state_of(_row_or(driver, world_id, before)), status="saved", receipt=receipt)


# Reads for later lanes ----------------------------------------------------------------------


def committed_for_request(driver: Driver, world_id: str, request_id: str) -> list[dict[str, Any]]:
    """What this request committed: its change entries in order, ids and codes only."""
    check_world_id(world_id)
    check_request_id(request_id)
    return WorldQuery(driver, world_id).read(
        "MATCH (e:ChangeEntry {request_id: $id}) RETURN e.sequence AS sequence, "
        "e.entity_type AS entity_type, e.entity_id AS entity_id, e.field AS field, "
        "e.new_value_id AS new_value_id, e.evidence_id AS evidence_id, e.commit_id AS commit_id, "
        "e.change_set_id AS change_set_id ORDER BY e.sequence",
        id=request_id,
    )


def pending_requests(driver: Driver, world_id: str, *, limit: int = 100) -> list[RequestState]:
    """The rows a claim could take now, oldest first."""
    check_world_id(world_id)
    _check_limit(limit)
    rows = WorldQuery(driver, world_id).read(_READ_DUE, now=clock.now(world_id), limit=limit)
    return [_state_of(row["r"]) for row in rows]


def surfaced_requests(driver: Driver, world_id: str, *, limit: int = 100) -> list[RequestState]:
    """The requests whose attempts ran out, or that a worker failed for good, for health."""
    check_world_id(world_id)
    _check_limit(limit)
    rows = WorldQuery(driver, world_id).read(
        "MATCH (r:Request {status: 'failed'}) WHERE r.retry_at IS NULL "
        "RETURN r ORDER BY r.created_at, r.id LIMIT $limit",
        limit=limit,
    )
    return [_state_of(row["r"]) for row in rows]


def worlds_with_due_requests(driver: Driver, *, limit: int = 100) -> list[str]:
    """The runner's watcher: ids only. A pass still needs a credential for each world."""
    _check_limit(limit)
    worlds = PlatformQuery(driver).read("MATCH (w:World) RETURN w.id AS id ORDER BY w.id")
    due: list[str] = []
    for world in worlds:
        world_id = world["id"]
        try:
            query = WorldQuery(driver, world_id)
        except ScopeError:
            # No pass can open a world outside the id form of S36, and it must not stop the others.
            continue
        rows = query.read(
            "MATCH (r:Request) WHERE " + _DUE + " RETURN r.id AS id LIMIT 1",
            now=clock.now(world_id),
        )
        if rows:
            due.append(world_id)
            if len(due) == limit:
                break
    return due
