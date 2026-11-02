"""Shared builders for the writer's contract tests: a seeded world, a source to cite, batches.

Every name here is invented. Tests in this folder may plant rows with raw Cypher; the builders
below go through the writer's own functions so a test reads like a caller.
"""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from grove.store.connection import DATABASE
from grove.store.query import WorldQuery
from grove.writer import Caller, apply_batch, record_connector, record_episode, record_source
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


def make_source(
    driver, world_id: str, connector_type: str, lines: list[str], **connector: Any
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
        start_time=T0,
        end_time=at_minute(len(lines)),
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
