"""Shared builders for the writer's contract tests: a seeded world, a source to cite, batches.

Every name here is invented. Tests in this folder may plant rows with raw Cypher; the builders
below go through the writer's own functions so a test reads like a caller.
"""

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any
from uuid import uuid4

import pytest

from grove.store.connection import DATABASE
from grove.store.query import WorldQuery
from grove.writer import Caller, apply_batch, record_connector, record_episode, record_source
from grove.writer.declines import stored_op_id
from grove.writer.worlds import create_world

PERSON = Caller(kind="person", scopes=frozenset({"grove.read", "grove.write"}), actor="person:t")
LEARNER = Caller(
    kind="update", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:learner"
)
CURATOR = Caller(
    kind="cleanup", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:curator"
)
HOST = Caller(kind="ai_host", scopes=frozenset({"grove.read", "grove.write"}), actor="host:t")
READ_ONLY = Caller(kind="person", scopes=frozenset({"grove.read"}), actor="person:reader")

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
CHAT_LINES = [
    f"line {n}: an invented message about the venue for Rowan and Ilse" for n in range(40)
]

_counter = count(1)


def iso(at: datetime) -> str:
    return at.isoformat()


def at_minute(offset_minutes: int) -> datetime:
    return T0 + timedelta(minutes=offset_minutes)


def clock_at(at: datetime, precision: str = "minute", words: str | None = None) -> dict:
    clock = {"earliest": iso(at), "latest": iso(at), "precision": precision}
    if words is not None:
        clock["words"] = words
    return clock


@pytest.fixture
def seeded(driver, world):
    """A world with its :World row, the primitive rows and the starter set."""
    made = create_world(driver, world, owner_user_id="u_t_owner")
    return {"world_id": world, **made}


@pytest.fixture
def chat(driver, seeded):
    """One WhatsApp-like source with 40 lines and one episode read from it."""
    return make_source(driver, seeded["world_id"], "whatsapp", CHAT_LINES)


# The episode window of a test source: wide, so a test may date its rows freely. A row may not be
# dated outside its episode (S51); a test of that rule passes its own window.
EPISODE_FROM = datetime(2000, 1, 1, tzinfo=UTC)
EPISODE_TO = datetime(2100, 1, 1, tzinfo=UTC)


def make_source(
    driver,
    world_id: str,
    connector_type: str,
    lines: list[str],
    *,
    episode_start: datetime = EPISODE_FROM,
    episode_end: datetime = EPISODE_TO,
    **connector: Any,
) -> dict[str, str]:
    connector_id = record_connector(driver, world_id, connector_type, **connector)
    source_id = record_source(
        driver,
        world_id,
        connector_id=connector_id,
        external_locator=f"export-{next(_counter)}.txt",
        content="\n".join(lines),
        parser_version="test-1",
    )
    episode_id = record_episode(
        driver,
        world_id,
        connector_id=connector_id,
        source_ids=[source_id],
        first_message_key=f"k-first-{next(_counter)}",
        last_message_key=f"k-last-{next(_counter)}",
        start_time=episode_start,
        end_time=episode_end,
        cut_rule="chat_window_90m",
    )
    return {"connector_id": connector_id, "source_id": source_id, "episode_id": episode_id}


def observation(
    src: dict[str, str],
    ev_id: str,
    line: int,
    *,
    at: datetime | None = None,
    statement_class: str = "report_state",
    speaker_kind: str = "person",
    derivation: str = "observed",
    message_key: str | None = None,
    locator: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """An observation of one line of `src`. Line numbers are 1-based."""
    return {
        "ev_id": ev_id,
        "episode_id": src["episode_id"],
        "source_id": src["source_id"],
        "locator": locator if locator is not None else f"line:{line}",
        "message_key": message_key or f"mk-{src['source_id'][-6:]}-{line}",
        "source_time": iso(at or at_minute(line)),
        "statement_class": statement_class,
        "derivation": derivation,
        "speaker_kind": speaker_kind,
        "modality": "text",
        **extra,
    }


def supports(*ev_ids: str) -> list[dict[str, str]]:
    return [{"ev_id": ev_id, "polarity": "supports"} for ev_id in ev_ids]


def new_op_id() -> str:
    return f"o{next(_counter)}"


def create_item(kind: str, title: str, ev_id: str, **fields: Any) -> dict[str, Any]:
    op = {
        "op": "create_item",
        "op_id": new_op_id(),
        "kind": kind,
        "title": title,
        "became_true": fields.pop("became_true", "unknown"),
        "evidence_refs": fields.pop("evidence_refs", supports(ev_id)),
        "reason": fields.pop("reason", "the message states it"),
    }
    op.update(fields)
    return op


def create_link(src: str, meaning: str, dst: str, ev_id: str, **fields: Any) -> dict[str, Any]:
    op = {
        "op": "create_link",
        "op_id": new_op_id(),
        "from": src,
        "meaning": meaning,
        "to": dst,
        "became_true": fields.pop("became_true", "unknown"),
        "evidence_refs": fields.pop("evidence_refs", supports(ev_id)),
        "reason": fields.pop("reason", "the message states it"),
    }
    op.update(fields)
    return op


def batch(ops: list[dict], evidence: list[dict] | None = None) -> dict[str, Any]:
    return {"evidence": evidence or [], "ops": ops}


def apply(driver, world_id: str, ops: list[dict], evidence: list[dict], caller=PERSON, **kw):
    return apply_batch(driver, world_id, caller, batch(ops, evidence), **kw)


def created_id(receipt, symbol: str = "+", entity_type: str = "item", nth: int = 0) -> str:
    """The id of the nth line of a receipt with this symbol and entity type."""
    lines = [
        line for line in receipt.lines if line.symbol == symbol and line.entity_type == entity_type
    ]
    return lines[nth].entity_id


def read(driver, world_id: str, cypher: str, **params: Any) -> list[dict]:
    return WorldQuery(driver, world_id).read(cypher, **params)


def raw(driver, cypher: str, **params: Any) -> list[dict]:
    """A raw read across worlds, for counting rows a test must not see change."""
    with driver.session(database=DATABASE) as session:
        return [dict(record.items()) for record in session.run(cypher, **params)]


def count_rows(driver, world_id: str) -> dict[str, int]:
    """Every node label and LINK count of one world: the before/after of an atomicity test."""
    rows = raw(
        driver,
        "MATCH (n {world_id: $w}) UNWIND labels(n) AS label "
        "RETURN label, count(*) AS c ORDER BY label",
        w=world_id,
    )
    counts = {row["label"]: row["c"] for row in rows}
    links = raw(driver, "MATCH ()-[l:LINK {world_id: $w}]->() RETURN count(l) AS c", w=world_id)
    counts["LINK"] = links[0]["c"]
    return counts


class NoStore:
    """A driver or a transaction that fails the test the moment anything asks it for the store."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the store was reached before the check: {name}")


def request_key() -> str:
    return f"req-{uuid4().hex[:12]}"


def request_row(driver, world_id: str, request_id: str) -> dict[str, Any] | None:
    rows = read(driver, world_id, "MATCH (r:Request {id: $id}) RETURN r", id=request_id)
    return rows[0]["r"] if rows else None


def world_dump(driver, world_id: str) -> str:
    """Every row of the world with every property, and the :World row: the proof of no write."""
    nodes = raw(
        driver,
        "MATCH (n {world_id: $w}) RETURN labels(n) AS labels, properties(n) AS p",
        w=world_id,
    )
    links = raw(
        driver, "MATCH ()-[l:LINK {world_id: $w}]->() RETURN properties(l) AS p", w=world_id
    )
    worlds = raw(driver, "MATCH (w:World {id: $w}) RETURN properties(w) AS p", w=world_id)
    rows = sorted(json.dumps(row, sort_keys=True, default=str) for row in [*nodes, *links, *worlds])
    return "\n".join(rows)


def unknown_kind(chat: dict[str, str], line: int = 1, kind: str = "spaceship") -> dict[str, Any]:
    """A batch the writer refuses with unknown_kind."""
    ev = f"e{line}"
    return batch([create_item(kind, "The silver hull", ev)], [observation(chat, ev, line)])


def stale_update(chat: dict[str, str], item_id: str, line: int = 5) -> dict[str, Any]:
    """An update at version 1: refused with version_conflict once the item has moved on."""
    ev = f"e{line}"
    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item_id,
        "version": 1,
        "fields": {"summary": f"an invented summary from line {line}"},
        "evidence_refs": supports(ev),
        "reason": "the message states it",
    }
    return batch([op], [observation(chat, ev, line)])


def assert_only_decline_log(driver, world_id: str, before: dict[str, int], refusals: list) -> None:
    """A decline wrote nothing but its log: every other label is equal, at most one new request
    row, one :Decline per refused op key with its rule and code, and no message or details stored.
    A caller with no write scope never reaches the log, so for it nothing changes at all."""
    after = count_rows(driver, world_id)
    if all(refusal.rule == "write_scope" for refusal in refusals):
        assert after == before
        return
    logged = ("Request", "Decline")
    assert {k: v for k, v in after.items() if k not in logged} == {
        k: v for k, v in before.items() if k not in logged
    }
    assert 0 <= after.get("Request", 0) - before.get("Request", 0) <= 1
    expected: dict[str, set[tuple[str, str]]] = {}
    for refusal in refusals:
        expected.setdefault(stored_op_id(refusal.op_id), set()).add((refusal.rule, refusal.code))
    assert after.get("Decline", 0) - before.get("Decline", 0) == len(expected)
    rows = raw(driver, "MATCH (d:Decline {world_id: $w}) RETURN properties(d) AS d", w=world_id)
    found = [row["d"] for row in rows]
    for op_id, codes in expected.items():
        assert any(
            row["op_id"] == op_id and (row["rule"], row["code"]) in codes for row in found
        ), (op_id, codes, found)
    requests = raw(driver, "MATCH (r:Request {world_id: $w}) RETURN properties(r) AS r", w=world_id)
    for row in [*found, *(entry["r"] for entry in requests)]:
        assert "message" not in row and "details" not in row
        text = row.get("receipt") or ""
        assert '"message"' not in text and '"details"' not in text
