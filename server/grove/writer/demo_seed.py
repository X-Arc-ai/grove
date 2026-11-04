"""Add only the pinned invented corpus to an existing designated demo world.

This adapter has no arbitrary text, world, batch, or fixture argument. The ordinary
writer still checks every graph operation and builds all evidence and history.
"""

import json
from typing import Any

from grove import clock
from grove.demo import corpus
from grove.demo.corpus import content_identity, request_identity
from grove.demo.screening import screen_story
from grove.demo_policy import (
    DemoDenied,
    configured_email_hash,
    require_corpus_identity,
    require_designation,
    require_designation_in_tx,
)
from grove.ids import new_id
from grove.store.query import CYPHER_PREFIX, PlatformQuery, collect
from grove.writer.api import execute_prepared_in_tx, prepare_batch
from grove.writer.caller import Caller
from grove.writer.clocks import parse_instant
from grove.writer.forget_sources import refuse_forgotten_source
from grove.writer.history import Stamp
from grove.writer.message_binding import check_batch_sources
from grove.writer.neardup import threshold_for
from grove.writer.receipts import Receipt, read_receipt
from grove.writer.registry import Registry
from grove.writer.requests import batch_fingerprint, mark_saved, write_request_row
from grove.writer.resolver import title_scores
from grove.writer.tx import WorldTx, WorldWriter

_CALLER = Caller("update", {"grove.propose"}, corpus.ACTOR)
_RULE = "fixed_corpus_v1"
_LOCK_USER = (
    CYPHER_PREFIX + "MATCH (u:User {id: $id}) "
    "SET u.demo_seed_lock_marker = true REMOVE u.demo_seed_lock_marker RETURN u.id AS id"
)
_TARGET = "MATCH (u:User {email_hash: $hash}) RETURN u.id AS id, u.demo_world_id AS world"


class _ReadOnly(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


def _read(reader, query: str, **params) -> list[dict]:
    return (reader.run if isinstance(reader, WorldTx) else reader.read)(query, **params)


def _one(reader, label: str, row_id: str) -> dict:
    statements = {
        "Source": "MATCH (n:Source {id: $id}) RETURN n",
        "Connector": "MATCH (n:Connector {id: $id}) RETURN n",
        "Request": "MATCH (n:Request {id: $id}) RETURN n",
    }
    rows = _read(reader, statements[label], id=row_id)
    if len(rows) != 1:
        raise DemoDenied()
    return rows[0]["n"]


def _matches(row: dict, expected: dict) -> None:
    if any(
        row.get(key) != value
        or (type(value) in (bool, int) and type(row.get(key)) is not type(value))
        for key, value in expected.items()
    ):
        raise DemoDenied()


def resolve_target(driver, hashed_address: str) -> dict:
    """Resolve existing durable ownership only; never create an account or world."""
    require_corpus_identity()
    if not isinstance(hashed_address, str) or hashed_address != configured_email_hash():
        raise DemoDenied()
    rows = PlatformQuery(driver).read(_TARGET, hash=hashed_address)
    if len(rows) != 1 or not rows[0].get("id") or not rows[0].get("world"):
        raise DemoDenied()
    target = require_designation(driver, rows[0]["world"], rows[0]["id"])
    if target.get("email_hash") != hashed_address:
        raise DemoDenied()
    return target


def _guard(tx: WorldTx, target: dict, *, lock: bool = False) -> None:
    current = require_designation_in_tx(tx)
    _matches(current, target)
    if lock:
        rows = collect(tx.unscoped().run(_LOCK_USER, id=target["user_id"]), None)
        if len(rows) != 1 or rows[0]["id"] != target["user_id"]:
            raise DemoDenied()
        _matches(require_designation_in_tx(tx), target)


def _batch(
    reader,
    registry: Registry,
    story: corpus.Story,
    source_id: str,
    episode_id: str,
    known: dict[str, dict],
) -> dict:
    refs = {key: row["id"] for key, row in known.items()}
    seen = dict(known)
    observations, operations = [], []
    for index, item in enumerate(story.items):
        kind = registry.resolve_kind(item.kind)
        temp = f"$item{index}"
        titles = {row["id"]: row["title"] for row in seen.values() if row["kind"] == kind["id"]}
        candidates = title_scores(reader, item.title, titles)
        distinct = [
            {"item": row_id, "reason": "Separate records in the fixed synthetic demo corpus."}
            for row_id, score in sorted(candidates.items())
            if score >= threshold_for(kind["id"])
        ]
        operations.append(
            {
                "op": "create_item",
                "op_id": f"item{index}",
                "temp_id": temp,
                "kind": item.kind,
                "title": item.title,
                "summary": item.summary,
                "became_true": "unknown",
                "recheck_by": corpus.RECHECK_TIME,
                "evidence_refs": [{"ev_id": item.key, "polarity": "supports"}],
                "reason": "The fixed synthetic demo source defines this invented record.",
                **({"status": item.status} if item.status is not None else {}),
                **({"distinct_from": distinct} if distinct else {}),
            }
        )
        refs[item.key] = temp
        seen[item.key] = {"id": temp, "title": item.title, "kind": kind["id"]}
        observations.append(
            {
                "ev_id": item.key,
                "source_id": source_id,
                "episode_id": episode_id,
                "message_key": item.key,
                "locator": "whole",
                "source_time": corpus.SOURCE_TIME,
                "statement_class": "report_state",
                "derivation": "observed",
                "premises": [],
                "speaker_kind": "ai_host",
                "modality": "text",
            }
        )
    for index, link in enumerate(story.links):
        if link.source not in refs or link.target not in refs:
            raise DemoDenied()
        operations.append(
            {
                "op": "create_link",
                "op_id": f"link{index}",
                "from": refs[link.source],
                "to": refs[link.target],
                "meaning": link.meaning,
                "became_true": "unknown",
                "recheck_by": corpus.RECHECK_TIME,
                "evidence_refs": [{"ev_id": link.source, "polarity": "supports"}],
                "reason": "The fixed synthetic demo source states this invented relationship.",
            }
        )
    return {"evidence": observations, "ops": operations}


def _connector(tx: WorldTx, digest: str, at) -> str:
    expected = {
        "type": "ai_host",
        "provider": corpus.ACTOR,
        "auth_reference": "synthetic",
        "batch_rule": _RULE,
        "settings": json.dumps({"version": corpus.VERSION, "sha256": digest}, sort_keys=True),
        "version": 1,
    }
    rows = tx.run("MATCH (c:Connector {provider: $provider}) RETURN c", provider=corpus.ACTOR)
    if rows:
        if len(rows) != 1:
            raise DemoDenied()
        _matches(rows[0]["c"], expected)
        return rows[0]["c"]["id"]
    row = {"id": new_id(tx.world_id), **expected, "touched_at": at}
    tx.run("CREATE (c:Connector) SET c += $row", row=row)
    return row["id"]


def _source(tx, story, screened, source_id, episode_id, connector_id, request_id, at):
    refuse_forgotten_source(tx, "synthetic:" + story.key, (item.key for item in story.items))
    row = {
        "id": source_id,
        "connector_id": connector_id,
        "external_locator": "synthetic:" + story.key,
        "received_at": at,
        "content": screened.content(),
        "parser_version": corpus.PARSER,
        "untrusted": True,
        "forget_state": "kept",
        "version": 1,
        "touched_at": at,
        "demo_version": corpus.VERSION,
        "demo_request_id": request_id,
    }
    tx.run("CREATE (s:Source) SET s += $row", row=row)
    tx.run(
        "CREATE (r:ScreeningReceipt) SET r += $row",
        row={
            "source_id": source_id,
            "counts_per_class": json.dumps(dict(screened.counts), sort_keys=True),
            "screened_at": at,
            "screened_where": "server",
        },
    )
    tx.run(
        "CREATE (e:Episode) SET e += $row",
        row={
            "id": episode_id,
            "connector_id": connector_id,
            "source_ids": [source_id],
            "first_message_key": story.items[0].key,
            "last_message_key": story.items[-1].key,
            "start_time": parse_instant(corpus.SOURCE_TIME),
            "end_time": parse_instant(corpus.SOURCE_TIME),
            "cut_rule": _RULE,
            "read_at": at,
            "version": 1,
            "touched_at": at,
        },
    )
    return source_id


def _saved(reader, registry, story, digest, known, *, absent=False) -> dict | None:
    request_id = request_identity(reader.world_id, story.key)
    requests = _read(reader, "MATCH (r:Request {id: $id}) RETURN r", id=request_id)
    sources = _read(
        reader,
        "MATCH (s:Source {external_locator: $locator}) RETURN s",
        locator="synthetic:" + story.key,
    )
    if not requests:
        # An interrupted transaction cannot leave any of these records behind.
        evidence = _read(
            reader,
            "MATCH (e:Evidence) WHERE e.message_key IN $keys RETURN e.id AS id LIMIT 1",
            keys=[item.key for item in story.items],
        )
        episodes = _read(
            reader,
            "MATCH (e:Episode {first_message_key: $key}) RETURN e.id AS id LIMIT 1",
            key=story.items[0].key,
        )
        if absent and not sources and not evidence and not episodes:
            return None
        raise DemoDenied()
    if len(requests) != 1 or len(sources) != 1:
        raise DemoDenied()
    request, source = requests[0]["r"], sources[0]["s"]
    _matches(
        request,
        {
            "caller": corpus.ACTOR,
            "status": "saved",
            "source_id": source["id"],
            "content_key": content_identity(story.key),
            "verbatim": False,
            "host_message_id": None,
            "relay": None,
            "about": None,
            "run_id": None,
            "protocol": None,
            "said_at": parse_instant(corpus.SOURCE_TIME),
            "attempts": 1,
            "repairs": 0,
        },
    )
    if (
        not request.get("commit_id")
        or not request.get("change_set_id")
        or not request.get("committed_at")
    ):
        raise DemoDenied()
    _matches(
        source,
        {
            "parser_version": corpus.PARSER,
            "content": story.content(),
            "untrusted": True,
            "forget_state": "kept",
            "version": 1,
            "demo_version": corpus.VERSION,
            "demo_request_id": request_id,
            "speakers": None,
            "document": None,
            "source_facts": None,
            "content_pointer": None,
            "calendar_events": None,
            "received_at": request["committed_at"],
            "touched_at": request["committed_at"],
        },
    )
    connector = _one(reader, "Connector", source["connector_id"])
    _matches(
        connector,
        {
            "type": "ai_host",
            "provider": corpus.ACTOR,
            "auth_reference": "synthetic",
            "batch_rule": _RULE,
            "settings": json.dumps({"version": corpus.VERSION, "sha256": digest}, sort_keys=True),
            "version": 1,
        },
    )
    receipts = _read(
        reader, "MATCH (r:ScreeningReceipt {source_id: $id}) RETURN r", id=source["id"]
    )
    episodes = _read(
        reader, "MATCH (e:Episode) WHERE $id IN e.source_ids RETURN e", id=source["id"]
    )
    if len(receipts) != 1 or len(episodes) != 1:
        raise DemoDenied()
    _matches(
        receipts[0]["r"],
        {
            "counts_per_class": "{}",
            "screened_where": "server",
            "screened_at": source.get("received_at"),
        },
    )
    episode = episodes[0]["e"]
    _matches(
        episode,
        {
            "connector_id": source["connector_id"],
            "source_ids": [source["id"]],
            "first_message_key": story.items[0].key,
            "last_message_key": story.items[-1].key,
            "start_time": parse_instant(corpus.SOURCE_TIME),
            "end_time": parse_instant(corpus.SOURCE_TIME),
            "cut_rule": _RULE,
            "version": 1,
            "run_id": None,
            "read_at": source["received_at"],
            "touched_at": source["received_at"],
        },
    )
    batch = _batch(reader, registry, story, source["id"], episode["id"], known)
    _matches(request, {"fingerprint": batch_fingerprint(batch)})
    check_batch_sources(reader, batch, request)
    evidence = [
        row["e"]
        for row in _read(reader, "MATCH (e:Evidence {request_id: $id}) RETURN e", id=request_id)
    ]
    if len(evidence) != len(story.items) + len(story.links):
        raise DemoDenied()
    by_target = {}
    for row in evidence:
        _matches(
            row,
            {
                "source_id": source["id"],
                "episode_id": episode["id"],
                "active": True,
                "polarity": "supports",
                "source_time": parse_instant(corpus.SOURCE_TIME),
                "locator": "whole",
                "statement_class": "report_state",
                "source_type": "said",
                "speaker_kind": "ai_host",
                "derivation": "observed",
                "modality": "text",
                "version": 1,
            },
        )
        if row.get("premises") or row["target_id"] in by_target:
            raise DemoDenied()
        by_target[row["target_id"]] = row
    items = [
        row["i"]
        for row in _read(reader, "MATCH (i:Item) WHERE i.id IN $ids RETURN i", ids=list(by_target))
    ]
    item_defs = {item.key: item for item in story.items}
    added = {}
    for row in items:
        ev = by_target[row["id"]]
        item = item_defs.get(ev.get("message_key"))
        if item is None or item.key in added or ev.get("target_type") != "item":
            raise DemoDenied()
        kind = registry.resolve_kind(item.kind)
        role = registry.resolve_status(kind, item.status)[1] if item.status else None
        _matches(
            row,
            {
                "title": item.title,
                "summary": item.summary,
                "kind": kind["id"],
                "status": item.status,
                "status_role": role,
                "version": 1,
                "provenance_evidence_id": ev["id"],
                "derived_source_type": "said",
                "derivation": "observed",
                "speaker": "ai_host",
                "end_class": None,
                "body": None,
                "properties": None,
                "search_terms": None,
                "became_true_precision": "unknown",
                "became_true_earliest": None,
                "became_true_latest": None,
                "stopped_being_true_precision": None,
                "recheck_by": parse_instant(corpus.RECHECK_TIME),
            },
        )
        added[item.key] = row
    if set(added) != set(item_defs):
        raise DemoDenied()
    all_items = {**known, **added}
    expected_links = set()
    for link in story.links:
        meaning, reverse = registry.resolve_meaning(link.meaning)
        a, b = all_items[link.source]["id"], all_items[link.target]["id"]
        if reverse:
            a, b = b, a
        expected_links.add((a, meaning["id"], b, link.source))
    links = _read(
        reader,
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.id IN $ids RETURN l, a.id AS a, b.id AS b",
        ids=list(by_target),
    )
    actual_links = set()
    for found in links:
        row, a, b = found["l"], found["a"], found["b"]
        ev = by_target[row["id"]]
        _matches(
            row,
            {
                "from_item": a,
                "to_item": b,
                "version": 1,
                "end_class": None,
                "active_key": "|".join((reader.world_id, a, row["meaning"], b)),
                "provenance_evidence_id": ev["id"],
                "derived_source_type": "said",
                "derivation": "observed",
                "original_phrase": None,
                "became_true_precision": "unknown",
                "stopped_being_true_precision": None,
                "recheck_by": parse_instant(corpus.RECHECK_TIME),
            },
        )
        if ev.get("target_type") != "link":
            raise DemoDenied()
        actual_links.add((a, row["meaning"], b, ev["message_key"]))
    if len(links) != len(story.links) or actual_links != expected_links:
        raise DemoDenied()
    entries = [
        row["e"]
        for row in _read(reader, "MATCH (e:ChangeEntry {request_id: $id}) RETURN e", id=request_id)
    ]
    required = {("item", row["id"]) for row in items} | {("link", row["l"]["id"]) for row in links}
    required |= {("evidence", row["id"]) for row in evidence}
    creates = [(row["entity_type"], row["entity_id"]) for row in entries if row["field"] == "row"]
    if len(creates) != len(required) or set(creates) != required:
        raise DemoDenied()
    for row in entries:
        _matches(
            row,
            {
                "actor": corpus.ACTOR,
                "commit_id": request["commit_id"],
                "change_set_id": request["change_set_id"],
                "run_id": None,
            },
        )
    created = [row for row in entries if row["field"] == "row"]
    values = _read(
        reader,
        "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v",
        ids=[row["new_value_id"] for row in created],
    )
    values = {row["v"]["id"]: row["v"]["value"] for row in values}
    projections = {
        ("item", row["id"]): {
            key: value
            for key, value in row.items()
            if key not in {"derived_source_type", "derivation", "speaker", "modality"}
        }
        for row in items
    }
    projections.update(
        {
            ("link", row["l"]["id"]): {
                key: value
                for key, value in row["l"].items()
                if key not in {"derived_source_type", "derivation"}
            }
            for row in links
        }
    )
    projections.update({("evidence", row["id"]): row for row in evidence})
    for entry in created:
        kind = entry["entity_type"]
        expected = {
            "table": {"item": "items", "link": "links", "evidence": "evidence"}[kind],
            "row": projections[(kind, entry["entity_id"])],
        }
        try:
            original = json.loads(values[entry["new_value_id"]])
            encoded = json.loads(json.dumps(expected, default=lambda value: value.isoformat()))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DemoDenied() from None
        if original != encoded:
            raise DemoDenied()
    if request.get("receipt") is not None:
        try:
            receipt = Receipt.from_json(json.loads(request["receipt"]))
        except (TypeError, ValueError, KeyError):
            raise DemoDenied() from None
        _check_receipt(receipt, request, added, links)
    return {"request": request, "items": added, "links": links}


def _check_receipt(receipt, request, items, links):
    if (
        receipt.state != "saved"
        or receipt.request_id != request["id"]
        or receipt.commit_id != request["commit_id"]
        or receipt.change_set_id != request["change_set_id"]
    ):
        raise DemoDenied()
    wanted = {("+", "item", row["id"]) for row in items.values()}
    wanted |= {(">", "link", row["l"]["id"]) for row in links}
    actual = [(line.symbol, line.entity_type, line.entity_id) for line in receipt.lines]
    if len(actual) != len(wanted) or set(actual) != wanted:
        raise DemoDenied()


def seed_story(driver, hashed_address: str, story_key: str) -> dict:
    """Commit one fixed story, or verify its exact saved result without duplication."""
    digest = require_corpus_identity()
    stories = corpus.stories()
    selected = next((index for index, story in enumerate(stories) if story.key == story_key), None)
    if selected is None:
        raise DemoDenied()
    story = stories[selected]
    target = resolve_target(driver, hashed_address)
    screened = screen_story(story_key)
    request_id = request_identity(target["world_id"], story.key)

    def work(tx):
        _guard(tx, target, lock=True)
        registry, known = Registry.load(tx), {}
        for previous in stories[:selected]:
            prior = _saved(tx, registry, previous, digest, known)
            known.update(prior["items"])
        saved = _saved(tx, registry, story, digest, known, absent=True)
        if saved is not None:
            _guard(tx, target)
            raise _ReadOnly(saved)
        at = clock.now(tx.world_id)
        if at < parse_instant(corpus.SOURCE_TIME):
            raise DemoDenied()
        connector = _connector(tx, digest, at)
        source_id, episode_id = new_id(tx.world_id), new_id(tx.world_id)
        stamp = Stamp(
            tx.world_id,
            at,
            corpus.ACTOR,
            new_id(tx.world_id),
            new_id(tx.world_id),
            request_id=request_id,
            caller_kind=_CALLER.kind,
        )
        row, fresh = write_request_row(
            tx,
            _CALLER,
            request_id,
            write_source=lambda current: _source(
                current, story, screened, source_id, episode_id, connector, request_id, at
            ),
            content_key=content_identity(story.key),
            verbatim=False,
            said_at=corpus.SOURCE_TIME,
        )
        if not fresh:
            raise DemoDenied()
        batch = _batch(tx, registry, story, source_id, episode_id, known)
        execute_prepared_in_tx(tx, stamp, prepare_batch(_CALLER, batch, request_id=request_id), row)
        mark_saved(tx, row, stamp, batch_fingerprint(batch), None)
        _guard(tx, target)
        return _saved(tx, registry, story, digest, known)

    replay = False
    try:
        saved = WorldWriter(driver, target["world_id"], timeout=120.0).transaction(work)
    except _ReadOnly as unchanged:
        saved, replay = unchanged.value, True
    request = saved["request"]
    receipt = read_receipt(
        driver,
        target["world_id"],
        commit_id=request["commit_id"],
        change_set_id=request["change_set_id"],
        request_id=request_id,
    )
    _check_receipt(receipt, request, saved["items"], saved["links"])

    def attach(tx):
        _guard(tx, target, lock=True)
        registry, known = Registry.load(tx), {}
        for previous in stories[:selected]:
            prior = _saved(tx, registry, previous, digest, known)
            known.update(prior["items"])
        _saved(tx, registry, story, digest, known)
        current = _one(tx, "Request", request_id)
        _matches(
            current,
            {
                "status": "saved",
                "source_id": request["source_id"],
                "commit_id": request["commit_id"],
                "change_set_id": request["change_set_id"],
                "content_key": request["content_key"],
                "fingerprint": request["fingerprint"],
            },
        )
        text = json.dumps(receipt.to_json(), sort_keys=True)
        if current.get("receipt") is not None:
            if current["receipt"] != text:
                raise DemoDenied()
            raise _ReadOnly(None)
        tx.run(
            "MATCH (r:Request {id: $id}) SET r.receipt = $receipt, "
            "r.touched_at = $at, r.version = r.version + 1",
            id=request_id,
            receipt=text,
            at=clock.now(tx.world_id),
        )
        _guard(tx, target)

    if request.get("receipt") is None:
        try:
            WorldWriter(driver, target["world_id"], timeout=120.0).transaction(attach)
        except _ReadOnly:
            pass
    return {
        "state": "saved",
        "story": story_key,
        "items": len(story.items),
        "links": len(story.links),
        "replayed": replay,
    }


def inspect_seed(driver, hashed_address: str) -> dict:
    """Return bounded counts only after validation of every retained corpus record."""
    digest = require_corpus_identity()
    target = resolve_target(driver, hashed_address)

    def inspect(tx):
        _guard(tx, target, lock=True)
        registry, known, count, links, missing = Registry.load(tx), {}, 0, 0, False
        completed = 0
        source_ids, request_ids = set(), set()
        for story in corpus.stories():
            saved = _saved(tx, registry, story, digest, known, absent=True)
            if saved is None:
                missing = True
                continue
            if saved["request"].get("receipt") is None:
                missing = True
            elif not missing:
                completed += 1
            known.update(saved["items"])
            source_ids.add(saved["request"]["source_id"])
            request_ids.add(saved["request"]["id"])
            count += 1
            links += len(saved["links"])
        all_sources = tx.run(
            "MATCH (s:Source) WHERE s.parser_version = $parser OR s.demo_version = $version "
            "RETURN s.id AS id",
            parser=corpus.PARSER,
            version=corpus.VERSION,
        )
        all_requests = tx.run(
            "MATCH (r:Request {caller: $actor}) RETURN r.id AS id", actor=corpus.ACTOR
        )
        if {row["id"] for row in all_sources} != source_ids or {
            row["id"] for row in all_requests
        } != request_ids:
            raise DemoDenied()
        world_items = tx.run("MATCH (i:Item) RETURN count(i) AS n")[0]["n"]
        world_links = tx.run(
            "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.active_key IS NOT NULL RETURN count(l) AS n"
        )[0]["n"]
        _guard(tx, target)
        raise _ReadOnly(
            {
                "state": "complete" if completed == len(corpus.stories()) else "partial",
                "synthetic": True,
                "version": corpus.VERSION,
                "completed_stories": completed,
                "items": len(known),
                "links": links,
                "sources": count,
                "episodes": count,
                "screening_receipts": count,
                "saved_requests": count,
                "world_items": world_items,
                "world_active_links": world_links,
            }
        )

    try:
        WorldWriter(driver, target["world_id"], timeout=120.0).transaction(inspect)
    except _ReadOnly as result:
        return result.value
    raise DemoDenied()
