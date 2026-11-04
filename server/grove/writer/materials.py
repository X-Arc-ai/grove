"""Atomic material revision, screened Source and ordinary note Request writes."""

import re
from dataclasses import dataclass

from grove import clock
from grove.demo_policy import DemoDenied, designation_in_tx, has_designation
from grove.ids import new_id
from grove.ingest.materials import TITLE_PARSER, prepare, uuid_key
from grove.store.query import CYPHER_PREFIX, collect
from grove.views.session import _FIND
from grove.writer.caller import Caller
from grove.writer.forget_ledger import checked_under_lock, locked_ledger
from grove.writer.forget_sources import refuse_forgotten_source
from grove.writer.requests import write_request_row
from grove.writer.sources import write_screened_source
from grove.writer.tx import WorldWriter


@dataclass
class MaterialConflict(Exception):
    code: str = "material_conflict"


class MaterialMissing(LookupError):
    pass


class MaterialSessionEnded(Exception):
    pass


def check_session(tx, session, session_hash):
    rows = collect(
        tx.unscoped().run(CYPHER_PREFIX + _FIND, token_hash=session_hash, now=clock.now()), None
    )
    if not rows or rows[0]["user_id"] != session.user_id or rows[0]["world_id"] != session.world_id:
        raise MaterialSessionEnded()
    if has_designation(designation_in_tx(tx) or {}):
        raise DemoDenied()


def latest(reader, material_id):
    uuid_key(material_id)
    run = getattr(reader, "run", None) or reader.read
    rows = run(
        "MATCH (m:MaterialRevision {material_id: $id}) RETURN m ORDER BY m.revision DESC LIMIT 1",
        id=material_id,
    )
    if not rows or rows[0]["m"]["state"] == "deleted":
        raise MaterialMissing()
    return rows[0]["m"]


def check_revision(tx, target):
    row = latest(tx, target["material_id"])
    if row["revision"] != target["revision"]:
        raise MaterialConflict("stale_revision")
    return row


def save(
    driver,
    session,
    session_hash,
    *,
    title,
    body,
    request_key_hash,
    input_digest,
    input_bytes,
    material_id=None,
    expected_revision=None,
    ledger_dir=None,
):
    if any(
        type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in (request_key_hash, input_digest)
    ):
        raise ValueError("material fingerprints must be SHA-256")
    if type(input_bytes) is not int or not 0 < input_bytes <= 16 * 1024 * 1024:
        raise ValueError("material input size is invalid")
    if expected_revision is not None and (
        type(expected_revision) is not int or not 1 <= expected_revision < 2**31 - 1
    ):
        raise MaterialConflict("stale_revision")
    material_id = material_id or new_id(session.world_id)
    uuid_key(material_id)
    revision = (expected_revision + 1) if expected_revision is not None else 1
    if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
        raise MaterialConflict("stale_revision")
    at = clock.now(session.world_id)
    prepared = prepare(session.world_id, material_id, revision, title, body, at)
    with locked_ledger(ledger_dir) as root:

        def work(tx):
            check_session(tx, session, session_hash)
            ledger = [r["f"] for r in tx.run("MATCH (f:ForgetLedger) RETURN f")]
            checked_under_lock(root, tx.world_id, ledger)
            prior = tx.run(
                "MATCH (m:MaterialRevision {request_key_hash: $key}) RETURN m", key=request_key_hash
            )
            if prior:
                row = prior[0]["m"]
                if row["input_digest"] != input_digest or row["state"] == "deleted":
                    raise MaterialConflict("idempotency_conflict")
                return {"id": row["material_id"], "fresh": False}
            refuse_forgotten_source(tx, "material:" + material_id)
            if expected_revision is not None:
                old = check_revision(
                    tx, {"material_id": material_id, "revision": expected_revision}
                )
                if old["kind"] != "note":
                    raise MaterialConflict()
                tx.run(
                    "MATCH (m:MaterialRevision {id: $id}) SET m.state = 'superseded', "
                    "m.version = m.version + 1, m.touched_at = $at",
                    id=old["id"],
                    at=at,
                )
            connector_id = new_id(tx.world_id)
            tx.run(
                "CREATE (c:Connector) SET c += $row",
                row={
                    "id": connector_id,
                    "type": "person_in_views"
                    if prepared.kind == "note"
                    else "notes_files_releases",
                    "provider": "material-note" if prepared.kind == "note" else "document",
                    "version": 1,
                    "touched_at": at,
                },
            )
            title_id = new_id(tx.world_id)
            write_screened_source(
                tx,
                prepared.title,
                source_id=title_id,
                connector_id=connector_id,
                parser_version=TITLE_PARSER,
            )
            source_ids = []
            characters = 0
            kept = 0
            for screened in prepared.bodies:
                source_id = new_id(tx.world_id)
                write_screened_source(
                    tx,
                    screened,
                    source_id=source_id,
                    connector_id=connector_id,
                    parser_version=prepared.parser,
                    document=prepared.kind == "file",
                )
                source_ids.append(source_id)
                characters += sum(len(m.text) for m in screened.messages)
                kept += len(screened.messages)
            episode_id = request_id = None
            if prepared.kind == "note" and kept:
                episode_id, request_id = new_id(tx.world_id), new_id(tx.world_id)
                tx.run(
                    "CREATE (e:Episode) SET e += $row",
                    row={
                        "id": episode_id,
                        "connector_id": connector_id,
                        "source_ids": source_ids,
                        "first_message_key": "note",
                        "last_message_key": "note",
                        "start_time": at,
                        "end_time": at,
                        "read_at": at,
                        "cut_rule": "one_request",
                        "version": 1,
                        "touched_at": at,
                    },
                )
                write_request_row(
                    tx,
                    Caller("person", {"grove.write"}, session.user_id),
                    request_id,
                    source_id=source_ids[0],
                    said_at=at,
                    relay="remember",
                )
            row = {
                "id": new_id(tx.world_id),
                "material_id": material_id,
                "revision": revision,
                "kind": prepared.kind,
                "format": prepared.format,
                "state": "current",
                "title_source_id": title_id,
                "source_ids": source_ids,
                "request_key_hash": request_key_hash,
                "input_digest": input_digest,
                "input_bytes": input_bytes,
                "retained_characters": characters,
                "part_count": kept,
                "held_count": kept if prepared.kind == "file" else 0,
                "created_at": at,
                "version": 1,
                "touched_at": at,
            }
            if request_id:
                row.update(request_id=request_id, episode_id=episode_id)
            tx.run("CREATE (m:MaterialRevision) SET m += $row", row=row)
            return {"id": material_id, "fresh": True}

        return WorldWriter(driver, session.world_id).transaction(work)
