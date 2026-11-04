"""New worlds: the :World row, the primitive rows and the starter set of B.4.

The one place that creates :World rows. Nothing here names a starter word: the names live in
seed/starter_set.json, and the rows they become are data the writer reads.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

from neo4j import Driver, ManagedTransaction, unit_of_work

from grove import clock
from grove.ids import new_id
from grove.store import capabilities
from grove.store.connection import DATABASE
from grove.store.normalise import normalise_phrase
from grove.store.primitives import (
    KIND_BELIEF,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANINGS,
    PRIMITIVE_WORLD,
)
from grove.store.query import CYPHER_PREFIX
from grove.store.scope import WORLD_PARAM, check_world_id
from grove.writer.tx import WorldTx

_ORIGIN = "starter"
_STATUS = "canonical"
_ACTOR = "seed"
_REASON = "starter set"
_ENTITY_TYPE = "registry_row"
_FIELD = "row"
_TARGET_TYPE = "meaning"
_FIRST_VERSION = 1
_NO_FIELDS = "[]"
# One transaction holds several statements per row.
_SEED_TIMEOUT = 120.0

# The primitive rows carry no time: S22 fixes them in code, S12 asks a time only of a row the writer
# updates, and no operation updates these. The SET runs on create and on match, so a field added
# in code reaches a store that holds an older row (S30); a second run writes the same values. A
# MERGE on the unique id alone is what stays safe when two callers race.
_ENSURE_PRIMITIVE_MEANING = CYPHER_PREFIX + "MERGE (m:Meaning {id: $id}) SET m += $row"
_ENSURE_PRIMITIVE_KIND = CYPHER_PREFIX + "MERGE (k:Kind {id: $id}) SET k += $row"
# MERGE on the unique id makes a second caller wait for the first to commit, and then see its seed.
_ENSURE_WORLD = CYPHER_PREFIX + (
    "MERGE (w:World {id: $world_id}) ON CREATE SET w.owner_user_id = $owner_user_id, "
    "w.created_at = $at, w.next_sequence = 0, w.read_generation = 0 "
    "RETURN w.owner_user_id AS owner_user_id, w.seed_version AS seed_version"
)
_MARK_SEEDED = CYPHER_PREFIX + (
    "MATCH (w:World {id: $world_id}) SET w.seed_version = $seed_version"
)

_ADD_ROW = {
    "Kind": "CREATE (n:Kind) SET n += $row",
    "Meaning": "CREATE (n:Meaning) SET n += $row",
    "Alias": "CREATE (n:Alias) SET n += $row",
    "PropertyName": "CREATE (n:PropertyName) SET n += $row",
}
_STARTER_PROPERTY_NAMES = (
    "MATCH (p:PropertyName {origin: $origin}) RETURN p.id AS id, p.kind AS kind, p.name AS name"
)
# The default capabilities of D5 (S54): store-wide rows with fixed ids, like the primitive rows.
_ENSURE_DEFAULT_CAPABILITY = CYPHER_PREFIX + "MERGE (c:Capability {id: $id}) SET c += $row"
_ADD_VALUE = "CREATE (v:HistoryValue {id: $id, value: $value})"
_ADD_ENTRY = "CREATE (e:ChangeEntry) SET e += $entry"
_STARTER_KINDS = "MATCH (k:Kind {origin: $origin}) RETURN k.id AS id, k.name AS key"
_STARTER_MEANINGS = "MATCH (m:Meaning {origin: $origin}) RETURN m.id AS id, m.name AS key"
_STARTER_ALIASES = "MATCH (a:Alias {origin: $origin}) RETURN a.id AS id, a.normalised_phrase AS key"


@dataclass(frozen=True)
class _SeedRun:
    """What every row of one create_world shares."""

    world_id: str
    at: datetime
    reason: str
    commit_id: str
    change_set_id: str


_TABLES = {
    "Kind": "kinds",
    "Meaning": "meanings",
    "Alias": "aliases",
    "PropertyName": "property_names",
}


def load_starter_set() -> dict[str, Any]:
    text = resources.files("grove.seed").joinpath("starter_set.json").read_text(encoding="utf-8")
    return json.loads(text)


def _ensure_primitives(tx: ManagedTransaction) -> None:
    fixed = {WORLD_PARAM: PRIMITIVE_WORLD, "primitive": True, "version": _FIRST_VERSION}
    for row_id, fields in PRIMITIVE_MEANINGS.items():
        tx.run(_ENSURE_PRIMITIVE_MEANING, id=row_id, row={**fields, **fixed}).consume()
    tx.run(_ENSURE_PRIMITIVE_KIND, id=KIND_BELIEF, row={**PRIMITIVE_KIND_BELIEF, **fixed}).consume()
    for default in capabilities.DEFAULTS:
        row = {
            WORLD_PARAM: PRIMITIVE_WORLD,
            "selector": default.selector(),
            "capability": default.capability,
            "origin": "default",
            "version": _FIRST_VERSION,
        }
        tx.run(_ENSURE_DEFAULT_CAPABILITY, id=default.id, row=row).consume()


def seed_primitives(driver: Driver) -> None:
    """The one writer of world_id '': it takes no world id, and a WorldWriter refuses ''."""
    with driver.session(database=DATABASE) as session:
        session.execute_write(_ensure_primitives)


def _add_value(tx: WorldTx, world_id: str, value: str) -> str:
    value_id = new_id(world_id)
    tx.run(_ADD_VALUE, id=value_id, value=value)
    return value_id


def _row_as_json(table: str, row: dict[str, Any], world_id: str) -> str:
    """What a restore rebuilds the row from (S22); values by id, so a forget still reaches them.
    The table is named here because a change entry's entity type stops at "registry row"."""
    shown = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }
    return json.dumps(
        {"table": table, "row": {**shown, WORLD_PARAM: world_id}},
        sort_keys=True,
        ensure_ascii=False,
    )


def _add_row(tx: WorldTx, label: str, row: dict[str, Any], run: _SeedRun) -> None:
    tx.run(_ADD_ROW[label], row=row)
    tx.run(
        _ADD_ENTRY,
        entry={
            "sequence": tx.next_sequence(),
            "entity_type": _ENTITY_TYPE,
            "entity_id": row["id"],
            "field": _FIELD,
            "new_value_id": _add_value(
                tx, run.world_id, _row_as_json(_TABLES[label], row, run.world_id)
            ),
            "recorded_at": run.at,
            "world_time": run.at,
            "reason": run.reason,
            "actor": _ACTOR,
            "commit_id": run.commit_id,
            "change_set_id": run.change_set_id,
            "history_only": False,
        },
    )


def _governed(run: _SeedRun) -> dict[str, Any]:
    return {
        "id": new_id(run.world_id),
        "status": _STATUS,
        "origin": _ORIGIN,
        "reason": run.reason,
        "minted_at": run.at,
        "minted_by": _ACTOR,
        "version": _FIRST_VERSION,
        "touched_at": run.at,
    }


def _registry_row(tx: WorldTx, declared: dict[str, Any], run: _SeedRun) -> dict[str, Any]:
    return {
        **_governed(run),
        "name": declared["name"],
        "description": _add_value(tx, run.world_id, declared["description"]),
        "distinct_episodes": 0,
        "uses": 0,
        "primitive": False,
    }


def _kind_row(tx: WorldTx, declared: dict[str, Any], run: _SeedRun) -> dict[str, Any]:
    return {
        **_registry_row(tx, declared, run),
        "lifecycle_class": declared["lifecycle_class"],
        "status_vocabulary": json.dumps(declared["status_vocabulary"]),
        "required_fields": _NO_FIELDS,
        "required_links": _NO_FIELDS,
        "propagating_fields": _NO_FIELDS,
    }


def _endpoint(declared: dict[str, Any], kind_ids: dict[str, str]) -> str:
    if declared.get("any"):
        return json.dumps({"any": True})
    kinds = [kind_ids[name] for name in declared["kinds"]]
    return json.dumps({"kinds": kinds, "classes": declared["classes"]})


def _meaning_row(
    tx: WorldTx, declared: dict[str, Any], kind_ids: dict[str, str], run: _SeedRun
) -> dict[str, Any]:
    row = {
        **_registry_row(tx, declared, run),
        "from_kinds": _endpoint(declared["from_kinds"], kind_ids),
        "to_kinds": _endpoint(declared["to_kinds"], kind_ids),
        "direction": declared["direction"],
        "cardinality": declared["cardinality"],
        "carries_consequences": declared["carries_consequences"],
        "downstream_end": declared["downstream_end"],
        "decomposition": declared["decomposition"],
        "rolls_up_completion": declared["rolls_up_completion"],
        "temporal_class": declared["temporal_class"],
        "default_horizon": declared["default_horizon"],
        "generic": declared.get("generic", False),
    }
    # Null is no property value: a row in the history must match the live row key for key.
    return {key: value for key, value in row.items() if value is not None}


def _property_row(
    tx: WorldTx, declared: dict[str, Any], kind_ids: dict[str, str], run: _SeedRun
) -> dict[str, Any]:
    row = {
        **_governed(run),
        "kind": kind_ids[declared["kind"]],
        "name": declared["name"],
        "value_type": declared["value_type"],
        "temporal_class": declared["temporal_class"],
        "scheduled_role": declared.get("scheduled_role"),
        "default_horizon": declared.get("default_horizon"),
        "description": _add_value(tx, run.world_id, declared["description"]),
        "distinct_episodes": 0,
        "uses": 0,
    }
    return {key: value for key, value in row.items() if value is not None}


def _seed_property_names(
    tx: WorldTx, starter: dict[str, Any], kind_ids: dict[str, str], run: _SeedRun
) -> dict[str, str]:
    """The starter property names (S55): scheduled times only, keyed `<kind> <name>`."""
    known = {(row["kind"], row["name"]) for row in tx.run(_STARTER_PROPERTY_NAMES, origin=_ORIGIN)}
    ids = {}
    for declared in starter.get("property_names", []):
        kind_id = kind_ids.get(declared["kind"])
        if kind_id is None or (kind_id, declared["name"]) in known:
            continue
        row = _property_row(tx, declared, kind_ids, run)
        _add_row(tx, "PropertyName", row, run)
        ids[f"{declared['kind']} {declared['name']}"] = row["id"]
    return ids


def _alias_row(declared: dict[str, Any], meaning_id: str, run: _SeedRun) -> dict[str, Any]:
    return {
        **_governed(run),
        "phrase": declared["phrase"],
        "normalised_phrase": normalise_phrase(declared["phrase"]),
        "target_type": _TARGET_TYPE,
        "target_id": meaning_id,
        "reversed": declared["reversed"],
        "added_at": run.at,
    }


def _seed(tx: WorldTx, world_id: str, starter: dict[str, Any], at: datetime) -> dict[str, Any]:
    run = _SeedRun(
        world_id=world_id,
        at=at,
        reason=_add_value(tx, world_id, _REASON),
        commit_id=new_id(world_id),
        change_set_id=new_id(world_id),
    )
    ids: dict[str, dict[str, str]] = {"kinds": {}, "meanings": {}, "aliases": {}}
    for declared in starter["kinds"]:
        row = _kind_row(tx, declared, run)
        _add_row(tx, "Kind", row, run)
        ids["kinds"][row["name"]] = row["id"]
    for declared in starter["meanings"]:
        row = _meaning_row(tx, declared, ids["kinds"], run)
        _add_row(tx, "Meaning", row, run)
        ids["meanings"][row["name"]] = row["id"]
    for declared in starter["meanings"]:
        for alias in declared["aliases"]:
            row = _alias_row(alias, ids["meanings"][declared["name"]], run)
            _add_row(tx, "Alias", row, run)
            ids["aliases"][row["normalised_phrase"]] = row["id"]
    ids["property_names"] = _seed_property_names(tx, starter, ids["kinds"], run)
    return ids


def _seeded_ids(tx: WorldTx) -> dict[str, dict[str, str]]:
    def read(query: str) -> dict[str, str]:
        return {row["key"]: row["id"] for row in tx.run(query, origin=_ORIGIN)}

    kind_names = {kind_id: name for name, kind_id in read(_STARTER_KINDS).items()}
    return {
        "kinds": read(_STARTER_KINDS),
        "meanings": read(_STARTER_MEANINGS),
        "aliases": read(_STARTER_ALIASES),
        "property_names": {
            f"{kind_names.get(row['kind'], row['kind'])} {row['name']}": row["id"]
            for row in tx.run(_STARTER_PROPERTY_NAMES, origin=_ORIGIN)
        },
    }


def create_world(driver: Driver, world_id: str, *, owner_user_id: str) -> dict[str, Any]:
    """One transaction: all of the world or none of it. A second call changes nothing."""
    check_world_id(world_id)
    seed_primitives(driver)
    starter = load_starter_set()

    def work(tx: WorldTx) -> dict[str, Any]:
        at = clock.now(world_id)
        # WorldTx refuses the platform label :World by design, and the row must commit with the
        # seed. No other module writes it.
        managed = tx.unscoped()
        world = managed.run(
            _ENSURE_WORLD, world_id=world_id, owner_user_id=owner_user_id, at=at
        ).single()
        tx.lock_world()
        version = world["seed_version"]
        if version is not None and version != starter["seed_version"]:
            # A world seeded before the property names (S55) gets the rows it lacks, and no other.
            _seed_property_names(
                tx,
                starter,
                _seeded_ids(tx)["kinds"],
                _SeedRun(
                    world_id=world_id,
                    at=at,
                    reason=_add_value(tx, world_id, _REASON),
                    commit_id=new_id(world_id),
                    change_set_id=new_id(world_id),
                ),
            )
            version = starter["seed_version"]
            managed.run(_MARK_SEEDED, world_id=world_id, seed_version=version).consume()
            ids = _seeded_ids(tx)
        elif version is not None:
            ids = _seeded_ids(tx)
        else:
            ids = _seed(tx, world_id, starter, at)
            version = starter["seed_version"]
            managed.run(_MARK_SEEDED, world_id=world_id, seed_version=version).consume()
        return {
            "world_id": world_id,
            "owner_user_id": world["owner_user_id"],
            "seed_version": version,
            **ids,
        }

    # Only this fixed creation path can run before the World row exists. The initial
    # MERGE is tracked, then work acquires the same lock as an ordinary writer.
    @unit_of_work(timeout=_SEED_TIMEOUT)
    def unit(native: ManagedTransaction) -> dict[str, Any]:
        scoped = WorldTx(native, world_id)
        try:
            return scoped._finish(work(scoped))
        finally:
            scoped._closed = True

    with driver.session(database=DATABASE) as session:
        return session.execute_write(unit)
