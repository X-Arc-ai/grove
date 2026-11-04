"""Atomic legacy import admission and bounded runner selection.

No protected Learner protocol is activated here. Holds use existing Request
status/error fields; content remains solely in the existing screened Source.
"""

from datetime import timedelta

from grove import clock
from grove.demo_policy import designation_in_tx, has_designation
from grove.ingest.import_parts import IMPORT_PROVIDERS, input_status
from grove.learner_input import select_input
from grove.store.query import WorldQuery
from grove.writer.caller import Caller
from grove.writer.forget_ledger import material_request_guard
from grove.writer.requests import _DUE, _claim_one, _need_worker, write_request_row
from grove.writer.tx import WorldWriter

PREFIX = "import:"
HOLD_PREFIX = "import_hold:"
READ_PAGE = 128


def request_id_for(source_id: str) -> str:
    return PREFIX + source_id


def _read(reader, cypher, **params):
    return (getattr(reader, "run", None) or reader.read)(cypher, **params)


def enqueue_import_parts_in_tx(tx, source_ids: list[str], provider: str) -> dict[str, int]:
    """Called only inside the screened chat transaction; holds are never due."""
    counts = {"requests_added": 0, "held_sources": 0}
    if provider not in IMPORT_PROVIDERS:
        return counts
    caller = Caller("writer", {"grove.write"}, PREFIX + provider)
    for source_id in source_ids:
        source = tx.run("MATCH (s:Source {id: $id}) RETURN s", id=source_id)[0]["s"]
        connector = tx.run(
            "MATCH (c:Connector {id: $id}) RETURN c", id=source["connector_id"]
        )[0]["c"]
        if connector.get("provider") != provider or connector.get("type") != "past_ai_conversations":
            raise ValueError("import source does not match its connector")
        reason = input_status(source, connector)
        if reason == "kept_out":
            continue
        request_id = request_id_for(source_id)
        selected = select_input(source, connector, {})
        # Existing Request metadata records the earliest actual message time.
        # Past-conversation provenance still uses each message's own timestamp.
        said_at = selected.messages[0]["at"] if selected.complete and selected.messages else None
        _, fresh = write_request_row(tx, caller, request_id, source_id=source_id, said_at=said_at)
        if fresh:
            counts["requests_added"] += 1
            if reason:
                tx.run(
                    "MATCH (r:Request {id: $id}) "
                    "SET r.status = 'failed', r.last_error_code = $code, "
                    "r.version = r.version + 1",
                    id=request_id, code=HOLD_PREFIX + reason,
                )
                counts["held_sources"] += 1
    return counts


def _source(reader, request):
    rows = _read(reader, "MATCH (s:Source {id: $id}) RETURN s", id=request["source_id"])
    if not rows:
        return None, None
    source = rows[0]["s"]
    rows = _read(reader, "MATCH (c:Connector {id: $id}) RETURN c", id=source["connector_id"])
    return source, rows[0]["c"] if rows else None


def readiness_reason(reader, request) -> str | None:
    """Closed diagnostic code for the same admission checks; never source text."""
    source, connector = _source(reader, request)
    if source is None or connector is None:
        return "source_unavailable"
    if connector.get("provider") not in IMPORT_PROVIDERS:
        return "provider_mismatch"
    if (
        request["id"] != request_id_for(source["id"])
        or request.get("caller") != PREFIX + connector["provider"]
    ):
        return "request_identity"
    reason = input_status(source, connector, request)
    if reason is not None:
        return reason
    # A later upload can insert an older part inside an existing episode range.
    # Episode start alone must not let an earlier pending part be overtaken.
    if request.get("said_at") is not None:
        earlier = _read(
            reader,
            "MATCH (current:Request {id: $request_id}) "
            "MATCH (s:Source {connector_id: $connector, external_locator: $locator}) "
            "MATCH (r:Request {source_id: s.id, caller: $caller}) "
            "WHERE r.id STARTS WITH $prefix AND r.protocol IS NULL AND r.status <> 'saved' "
            "AND r.id <> current.id "
            "AND (r.said_at.epochSeconds < current.said_at.epochSeconds "
            "OR (r.said_at.epochSeconds = current.said_at.epochSeconds "
            "AND r.said_at.nanosecond < current.said_at.nanosecond)) "
            "RETURN r.id AS id LIMIT 1",
            connector=source["connector_id"], locator=source["external_locator"],
            caller=request["caller"], prefix=PREFIX, request_id=request["id"],
        )
        if earlier:
            return "earlier_pending_time"
    # Episode source_ids retain complete-message order even under a frozen clock.
    # Across upload deltas, real episode time and read time give a stable order.
    offset = 0
    while True:
        episodes = _read(
            reader,
            "MATCH (e:Episode {connector_id: $connector}) "
            "MATCH (s:Source {connector_id: $connector, external_locator: $locator}) "
            "WHERE s.id IN e.source_ids "
            "RETURN DISTINCT e.id AS id, e.start_time AS start, e.read_at AS read, "
            "e.source_ids AS sources ORDER BY start, read, id SKIP $offset LIMIT $limit",
            connector=source["connector_id"], locator=source["external_locator"],
            offset=offset, limit=READ_PAGE,
        )
        if not episodes:
            return "episode_unavailable"
        for episode in episodes:
            ids = episode["sources"]
            found = source["id"] in ids
            prior = ids[:ids.index(source["id"])] if found else ids
            # Historical sources without an import Request never become work.
            # Page history rather than permanently blocking after 512 old parts.
            for start in range(0, len(prior), READ_PAGE):
                unresolved = _read(
                    reader,
                    "MATCH (current:Request {id: $request_id}) "
                    "MATCH (r:Request) WHERE r.id IN $ids AND r.status <> 'saved' "
                    "AND (current.said_at IS NULL OR r.said_at IS NULL "
                    "OR r.said_at.epochSeconds < current.said_at.epochSeconds "
                    "OR (r.said_at.epochSeconds = current.said_at.epochSeconds "
                    "AND r.said_at.nanosecond <= current.said_at.nanosecond)) "
                    "RETURN r.id AS id LIMIT 1",
                    ids=[request_id_for(key) for key in prior[start:start + READ_PAGE]],
                    request_id=request["id"],
                )
                if unresolved:
                    return "earlier_pending_episode"
            if found:
                return None
        offset += len(episodes)


def _ready(reader, request) -> bool:
    return readiness_reason(reader, request) is None


def _import_candidates(reader, now):
    last = _read(
        reader,
        "MATCH (run:RunRecord {agent: 'learner'}) MATCH (r:Request) "
        "WHERE r.id IN run.input_ids AND r.id STARTS WITH $prefix "
        "RETURN r.caller AS caller, max(run.started) AS last",
        prefix=PREFIX,
    )
    touched = {row["caller"]: row["last"] for row in last}
    # Each provider gets a turn, even when another has over one page of work.
    providers = sorted(IMPORT_PROVIDERS, key=lambda provider: (
        touched.get(PREFIX + provider) is not None,
        touched.get(PREFIX + provider) or now, provider,
    ))
    for provider in providers:
        cursor = ""
        order_seconds = order_nanos = created_seconds = created_nanos = 0
        while True:
            # Native datetime detachment can change timezone representation and
            # loses sub-microsecond precision. Keep the cursor as exact integers.
            rows = _read(
                reader,
                "MATCH (r:Request {caller: $caller}) WHERE " + _DUE +
                " AND r.id STARTS WITH $prefix "
                "WITH r, coalesce(r.said_at, r.created_at) AS order_at "
                "WITH r, order_at.epochSeconds AS order_seconds, "
                "order_at.nanosecond AS order_nanos, "
                "r.created_at.epochSeconds AS created_seconds, "
                "r.created_at.nanosecond AS created_nanos "
                "WHERE $cursor = '' OR order_seconds > $order_seconds "
                "OR (order_seconds = $order_seconds AND order_nanos > $order_nanos) "
                "OR (order_seconds = $order_seconds AND order_nanos = $order_nanos "
                "AND (created_seconds > $created_seconds "
                "OR (created_seconds = $created_seconds AND created_nanos > $created_nanos) "
                "OR (created_seconds = $created_seconds AND created_nanos = $created_nanos "
                "AND r.id > $cursor))) "
                "RETURN r, order_seconds, order_nanos, created_seconds, created_nanos "
                "ORDER BY order_seconds, order_nanos, created_seconds, created_nanos, r.id "
                "LIMIT $limit",
                now=now, prefix=PREFIX, caller=PREFIX + provider,
                cursor=cursor, order_seconds=order_seconds, order_nanos=order_nanos,
                created_seconds=created_seconds, created_nanos=created_nanos, limit=READ_PAGE,
            )
            if not rows:
                break
            yield from rows
            last = rows[-1]
            cursor = last["r"]["id"]
            order_seconds, order_nanos = last["order_seconds"], last["order_nanos"]
            created_seconds, created_nanos = last["created_seconds"], last["created_nanos"]


def has_due_import(query, now) -> bool:
    return any(_ready(query, row["r"]) for row in _import_candidates(query, now))


def claim_import_request(driver, world_id, caller, *, worker, lease_seconds):
    _need_worker(caller, None)
    now = clock.now(world_id)
    query = WorldQuery(driver, world_id)
    for candidate in _import_candidates(query, now):
        def work(tx):
            if has_designation(designation_in_tx(tx) or {}):
                return None
            rows = tx.run("MATCH (r:Request {id: $id}) RETURN r", id=candidate["r"]["id"])
            if not rows or not _ready(tx, rows[0]["r"]):
                return None
            return _claim_one(tx, rows[0]["r"]["id"], worker, now, timedelta(seconds=lease_seconds))
        claimed = WorldWriter(driver, world_id).transaction(work)
        if claimed is not None:
            return claimed
    return None


def claim_interactive_request(driver, world_id, caller, *, worker, lease_seconds):
    _need_worker(caller, None)
    now = clock.now(world_id)
    rows = WorldQuery(driver, world_id).read(
        "MATCH (r:Request) WHERE " + _DUE + " AND NOT r.id STARTS WITH $prefix "
        "RETURN r.id AS id ORDER BY r.created_at, r.id LIMIT $limit",
        now=now, prefix=PREFIX, limit=20,
    )
    for row in rows:
        # A note is material: its claim holds the forget ledger lock, as claim_next_request does.
        with material_request_guard(driver, world_id, [row["id"]]):
            claimed = WorldWriter(driver, world_id).transaction(
                lambda tx, key=row["id"]: _claim_one(
                    tx, key, worker, now, timedelta(seconds=lease_seconds)
                )
            )
        if claimed is not None:
            return claimed
    return None
