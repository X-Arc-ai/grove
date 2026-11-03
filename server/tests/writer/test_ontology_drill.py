"""The Oct 2 drill (D8 "How to test it"): an agent attempts to write the registry or the primitives
outside the ontology operations, and is refused, because no path it holds can do it.

The agent's only path is a batch of B.2 operations under its own caller kind. Each attempt below is
one a real agent could send; each is refused, and the registry is byte-identical after it. Below the
writer, the agent's process holds no store credential, and the store refuses a connection without
one. The seed file is release input: an edited seed never reaches a world that exists.
"""

import json
from pathlib import Path

import pytest
import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ClientError

from grove.config import load_settings
from grove.store.primitives import KIND_BELIEF, MEANING_SUPERSEDED_BY, PRIMITIVE_MEANINGS
from grove.writer import BatchDeclined, worlds
from grove.writer.worlds import create_world, load_starter_set
from tests.writer.helpers import (
    CURATOR,
    LEARNER,
    Caller,
    apply,
    create_item,
    new_op_id,
    observation,
    raw,
    read,
)

CONNECTOR = Caller(
    kind="consolidation", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:connector"
)
AGENTS = {"learner": LEARNER, "curator": CURATOR, "connector": CONNECTOR}
COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "compose.yml"
REGISTRY_LABELS = ("Kind", "Meaning", "Alias", "PropertyName", "OntologyChange", "Capability")
# The word the agent minted on Oct 2 by editing its seed, spelled as a new word here.
HEADLINE = "superseded as headline by"


def registry_dump(driver, world_id: str) -> str:
    """Every registry row of the world and every primitive row, with every property. Only kinds and
    meanings have primitive rows; other lanes keep default capabilities in world ''."""
    rows = []
    for label in REGISTRY_LABELS:
        worlds_read = [world_id, ""] if label in ("Kind", "Meaning") else [world_id]
        found = raw(
            driver,
            f"MATCH (n:{label}) WHERE n.world_id IN $worlds RETURN properties(n) AS p",
            worlds=worlds_read,
        )
        rows += [json.dumps([label, row["p"]], sort_keys=True, default=str) for row in found]
    return "\n".join(sorted(rows))


def word(driver, world_id: str, label: str, name: str) -> dict:
    return read(driver, world_id, f"MATCH (n:{label} {{name: $n}}) RETURN n", n=name)[0]["n"]


def refused(driver, world_id, ops, evidence, caller) -> list:
    with pytest.raises(BatchDeclined) as declined:
        apply(driver, world_id, ops, evidence, caller=caller)
    return declined.value.refusals


def proposal(**over) -> dict:
    op = {
        "op": "propose_word",
        "op_id": new_op_id(),
        "word_type": "meaning",
        "name": HEADLINE,
        "description": "A newer figure replaces an older one in presentation.",
        "reason": "No meaning separates a presentation update from a correction.",
        "worked_example": {"ev_id": "e1"},
        "neighbours": [
            {"word": "related to", "distinction": "related to carries no replacement"},
            {"word": "depends on", "distinction": "depends on carries no order in time"},
        ],
        "declared": {
            "from_kinds": {"any": True},
            "to_kinds": {"any": True},
            "direction": "from_to",
            "cardinality": "many",
            "carries_consequences": False,
            "temporal_class": "permanent",
        },
        "judgment_ids": ["00000000-0000-7000-8000-00000000beef"],
    }
    op.update(over)
    return op


@pytest.mark.parametrize("agent", sorted(AGENTS))
@pytest.mark.parametrize(
    "op_name", ["create_meaning", "update_meaning", "edit_seed", "migrate", "write_registry"]
)
def test_an_agent_has_no_operation_that_writes_the_registry(driver, seeded, chat, agent, op_name):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    op = {"op": op_name, "op_id": new_op_id(), "name": HEADLINE, "table": "meanings"}
    refusals = refused(driver, world_id, [op], [observation(chat, "e1", 1)], AGENTS[agent])
    assert [(r.code, r.rule) for r in refusals] == [("invalid_arguments", "unknown_op")]
    assert registry_dump(driver, world_id) == before


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_an_item_operation_cannot_reach_a_registry_row(driver, seeded, chat, agent):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    meaning = word(driver, world_id, "Meaning", "depends on")
    update = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": meaning["id"],
        "version": meaning["version"],
        "fields": {"title": HEADLINE},
        "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
        "reason": "rename the word",
    }
    refusals = refused(driver, world_id, [update], [observation(chat, "e1", 1)], AGENTS[agent])
    assert refusals[0].code == "not_found"
    assert registry_dump(driver, world_id) == before


@pytest.mark.parametrize(
    "field, value",
    [
        ("status", "canonical"),
        ("origin", "starter"),
        ("primitive", True),
        ("generic", True),
        ("uses", 99),
        ("distinct_episodes", 3),
        ("minted_by", "seed"),
        ("version", 7),
    ],
)
def test_an_agent_cannot_declare_a_field_the_writer_sets(driver, seeded, chat, field, value):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    op = proposal()
    op["declared"] = {**op["declared"], field: value}
    refusals = refused(driver, world_id, [op], [observation(chat, "e1", 1)], LEARNER)
    assert (refusals[0].code, refusals[0].rule) == ("invalid_arguments", "writer_field")
    assert registry_dump(driver, world_id) == before


@pytest.mark.parametrize("name", ["superseded by", "Corrected_By", "describes", "belief"])
def test_an_agent_cannot_mint_a_primitive_word(driver, seeded, chat, name):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    word_type = "kind" if name == "belief" else "meaning"
    op = proposal(name=name, word_type=word_type)
    if word_type == "kind":
        op["declared"] = {"lifecycle_class": "lasting_thing", "status_vocabulary": []}
        op["neighbours"] = [
            {"word": "person", "distinction": "a person is not a belief"},
            {"word": "place", "distinction": "a place is not a belief"},
        ]
    refusals = refused(driver, world_id, [op], [observation(chat, "e1", 1)], LEARNER)
    assert (refusals[0].code, refusals[0].rule) == ("invalid_arguments", "primitive_word")
    assert registry_dump(driver, world_id) == before


@pytest.mark.parametrize(
    "target", [MEANING_SUPERSEDED_BY, str(PRIMITIVE_MEANINGS[MEANING_SUPERSEDED_BY]["name"])]
)
def test_no_operation_promotes_a_primitive_or_a_starter_row(driver, seeded, target):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    for word_ref in (target, "depends on"):
        op = {
            "op": "promote_word",
            "op_id": new_op_id(),
            "word_type": "meaning",
            "word": word_ref,
            "reason": "the agent wants it",
        }
        refusals = refused(driver, world_id, [op], [], CONNECTOR)
        assert (refusals[0].code, refusals[0].rule) == ("invalid_arguments", "primitive_word")
    assert registry_dump(driver, world_id) == before


def test_a_mint_with_unrecorded_verdicts_writes_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    ops = [create_item("person", "Lio Varn", "e1"), proposal()]
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)], LEARNER)
    assert ("unrecorded_verdict", "unrecorded_verdict") in [(r.code, r.rule) for r in refusals]
    assert registry_dump(driver, world_id) == before


def test_an_edited_seed_never_reaches_a_world_that_exists(driver, seeded, monkeypatch):
    world_id = seeded["world_id"]
    before = registry_dump(driver, world_id)
    edited = load_starter_set()
    added = dict(edited["meanings"][-1])
    added.update(name=HEADLINE, aliases=[])
    edited["meanings"] = [*edited["meanings"], added]
    monkeypatch.setattr(worlds, "load_starter_set", lambda: edited)

    create_world(driver, world_id, owner_user_id="u_t_owner")

    assert registry_dump(driver, world_id) == before
    assert not read(driver, world_id, "MATCH (m:Meaning {name: $n}) RETURN m", n=HEADLINE)
    assert not read(driver, world_id, "MATCH (k:Kind {id: $id}) RETURN k", id=KIND_BELIEF)


def test_the_store_refuses_a_process_that_holds_no_credential(driver):
    with GraphDatabase.driver(load_settings().neo4j_uri, auth=None) as anonymous:
        with pytest.raises((AuthError, ClientError)):
            anonymous.verify_connectivity()


def test_the_agent_runner_holds_no_store_credential_and_cannot_reach_the_store():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    runner = services["runner"]
    files = [
        entry["path"] if isinstance(entry, dict) else entry
        for entry in runner.get("env_file") or []
    ]
    assert files and all("neo4j" not in path and "api" not in Path(path).name for path in files)
    environment = runner.get("environment") or {}
    names = environment if isinstance(environment, dict) else [e.split("=")[0] for e in environment]
    assert "NEO4J_AUTH" not in names and "NEO4J_URI" not in names
    store_networks = set(services["neo4j"].get("networks") or [])
    assert store_networks and not store_networks & set(runner.get("networks") or [])
