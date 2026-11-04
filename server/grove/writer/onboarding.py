"""The sole write path for bounded export reminders, under the existing World lock."""

import json

from grove import clock
from grove.demo_policy import designation_in_tx, has_designation
from grove.ids import new_id
from grove.store.query import CYPHER_PREFIX, collect
from grove.views.onboarding import (
    MAX_REVISION,
    READ_STATE,
    InvalidPendingExports,
    PendingExportCapture,
    PendingExportDenied,
    PendingExportSessionEnded,
    require_provider,
    snapshot,
    valid_generation,
)
from grove.views.session import _FIND, ViewSession
from grove.writer.tx import WorldTx, WorldWriter

_SAVE = (
    "MATCH (w:World {id: $world_id}) "
    "SET w.pending_export_requests = $requests, w.pending_export_revision = $revision"
)


def _state(tx: WorldTx, account: str) -> dict:
    rows = collect(tx.unscoped().run(CYPHER_PREFIX + READ_STATE, world_id=tx.world_id), None)
    if len(rows) != 1 or rows[0].get("account") != account:
        raise PendingExportDenied("export owner changed")
    if has_designation(designation_in_tx(tx) or {}):
        raise PendingExportDenied("export reminders are unavailable for this demo")
    return snapshot(rows[0])


def _save(tx: WorldTx, state: dict) -> dict:
    if state["revision"] >= MAX_REVISION:
        raise InvalidPendingExports("export revision is exhausted")
    state["revision"] += 1
    tx.unscoped().run(
        CYPHER_PREFIX + _SAVE,
        world_id=tx.world_id,
        requests=json.dumps(state["pending_exports"], sort_keys=True, separators=(",", ":")),
        revision=state["revision"],
    ).consume()
    return state


def request_export(driver, session: ViewSession, session_hash: str, provider: str) -> dict:
    require_provider(provider)

    def work(tx: WorldTx) -> dict:
        # Reuse the actual session resolver, including oldest-world selection, inside
        # the WorldWriter transaction. A stale API dependency cannot authorize a write.
        rows = collect(
            tx.unscoped().run(CYPHER_PREFIX + _FIND, token_hash=session_hash, now=clock.now()),
            None,
        )
        if (
            not rows
            or rows[0]["user_id"] != session.user_id
            or rows[0]["world_id"] != session.world_id
        ):
            raise PendingExportSessionEnded("export session ended")
        state = _state(tx, session.user_id)
        state["pending_exports"][provider] = new_id(session.world_id)
        return _save(tx, state)

    return WorldWriter(driver, session.world_id).transaction(work)


def complete_export(driver, captured: PendingExportCapture) -> dict:
    require_provider(captured.provider)
    if captured.generation is not None and not valid_generation(captured.generation):
        raise InvalidPendingExports("invalid captured generation")

    def work(tx: WorldTx) -> dict:
        state = _state(tx, captured.account)
        if (
            captured.generation is None
            or state["pending_exports"].get(captured.provider) != captured.generation
        ):
            return state
        del state["pending_exports"][captured.provider]
        return _save(tx, state)

    return WorldWriter(driver, captured.world_id).transaction(work)
