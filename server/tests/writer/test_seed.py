"""create_world against the live store. The tables here are literal copies of B.4 on purpose: a
drift in the JSON must fail against a second transcription, not against itself."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import resources
from uuid import UUID

import pytest

from grove import clock
from grove.store.catalogue import table
from grove.store.connection import DATABASE
from grove.store.enums import ENUMS
from grove.store.errors import Neo4jError, ScopeError
from grove.store.normalise import normalise_phrase
from grove.store.primitives import (
    KIND_BELIEF,
    MEANING_AFFECTED_BY,
    MEANING_CORRECTED_BY,
    MEANING_DESCRIBES,
    MEANING_SUPERSEDED_BY,
    PRIMITIVE_IDS,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANING_IDS,
    PRIMITIVE_MEANINGS,
    PRIMITIVE_WORLD,
)
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer import worlds
from grove.writer.tx import WorldWriter
from grove.writer.worlds import create_world, load_starter_set, seed_primitives

AT = datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)
OWNER = "user-seed-test"

WORK = "work_with_state"
ANY = "any"

KINDS = {
    "person": ("lasting_thing", []),
    "organisation": ("lasting_thing", [("active", "active"), ("closed", "ended")]),
    "place": ("lasting_thing", []),
    "plan": (
        WORK,
        [
            ("proposed", "open"),
            ("active", "active"),
            ("waiting", "blocked"),
            ("fulfilled", "done"),
            ("dropped", "cancelled"),
        ],
    ),
    "task": (
        WORK,
        [
            ("open", "open"),
            ("in progress", "active"),
            ("blocked", "blocked"),
            ("done", "done"),
            ("cancelled", "cancelled"),
        ],
    ),
    "problem": (
        WORK,
        [
            ("open", "open"),
            ("being worked on", "active"),
            ("resolved", "done"),
            ("dismissed", "cancelled"),
        ],
    ),
    "assumption": (WORK, [("holds", "active"), ("dropped", "cancelled")]),
    "event": ("record", []),
    "observation": ("record", []),
    "decision": ("record", []),
    "trip": ("bounded_period", []),
    "period": ("bounded_period", []),
}

# name: from, to, cardinality, consequences, downstream end, decomposition, rolls up, temporal
# class, default horizon. An endpoint is ANY or a set of kind names and lifecycle classes.
MEANINGS = {
    "depends on": (ANY, ANY, "many", True, "from", False, False, "state", 30),
    "blocks": (ANY, {WORK, "bounded_period"}, "many", True, "to", False, False, "state", 30),
    "assigned to": ({WORK}, {"person"}, "one_per_source", True, "from", False, False, "state", 30),
    "owned by": (ANY, {"person", "organisation"}, "many", True, "from", False, False, "state", 180),
    "part of": (ANY, ANY, "many", False, None, True, False, "permanent", None),
    "step of": ({WORK}, {WORK}, "one_per_source", False, None, True, True, "permanent", None),
    "lives in": ({"person"}, {"place"}, "one_per_source", False, None, False, False, "state", 180),
    "works at": ({"person"}, {"organisation"}, "many", False, None, False, False, "state", 180),
    "member of": ({"person"}, {"organisation"}, "many", False, None, False, False, "state", 365),
    "involves": (
        {"record"},
        {"person", "organisation"},
        "many",
        False,
        None,
        False,
        False,
        "event",
        None,
    ),
    "related to": (ANY, ANY, "many", False, None, False, False, "state", 180),
}

# meaning: {alias: reversed}
ALIASES = {
    "depends on": {
        "gated on": False,
        "relies on": False,
        "requires": False,
        "needs": False,
        "needed by": True,
    },
    "blocks": {"holds up": False, "prevents": False, "blocked by": True},
    "assigned to": {"handled by": False, "responsible for": True},
    "owned by": {"owns": True},
    "part of": {"within": False, "includes": True, "contains": True},
    "step of": {"subtask of": False, "has step": True},
    "lives in": {"based in": False, "resides in": False},
    "works at": {"works for": False, "employed by": False},
    "member of": {},
    "involves": {"attended by": False, "attended": True, "took part in": True},
    "related to": {
        "relates to": False,
        "connected to": False,
        "connected with": False,
        "linked to": False,
    },
}
ALIAS_COUNT = 27
SEEDED_ROWS = 12 + 11 + ALIAS_COUNT
# One description per kind and meaning, one row JSON per seeded row, one "starter set" reason.
HISTORY_VALUES = 12 + 11 + SEEDED_ROWS + 1

# Columns the spec leaves empty when a row is born. Every other column must hold a value.
EMPTY_AT_BIRTH = {
    "kinds": {"worked_example", "nearest_neighbours_at_mint", "first_used", "last_used"},
    "meanings": {
        "worked_example",
        "nearest_neighbours_at_mint",
        "first_used",
        "last_used",
        "cardinality_group",
    },
    "aliases": {"valid_from", "valid_to", "worked_example"},
    "change_entries": {"old_value_id", "evidence_id", "request_id", "run_id", "inverse_of"},
    "history_values": {"forgotten_at", "item_id"},
}


def nodes(driver, world_id, label):
    return [row["n"] for row in WorldQuery(driver, world_id).read(f"MATCH (n:{label}) RETURN n")]


def count(driver, world_id, label):
    return len(nodes(driver, world_id, label))


def world_row(driver, world_id):
    rows = PlatformQuery(driver).read("MATCH (w:World {id: $id}) RETURN w", id=world_id)
    return rows[0]["w"] if rows else None


def everything_in(driver, world_id):
    with driver.session(database=DATABASE) as session:
        return session.run(
            "MATCH (n {world_id: $world_id}) RETURN count(n) AS c", world_id=world_id
        ).single()["c"]


def snapshot(driver, world_id):
    labels = ("Kind", "Meaning", "Alias", "ChangeEntry", "HistoryValue")
    return {
        label: sorted(
            json.dumps(row, sort_keys=True, default=str) for row in nodes(driver, world_id, label)
        )
        for label in labels
    }


def history_text(driver, world_id, value_id):
    rows = WorldQuery(driver, world_id).read(
        "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value", id=value_id
    )
    assert len(rows) == 1, value_id
    return rows[0]["value"]


def as_json_row(row):
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def uuid7_time(value):
    parsed = UUID(value)
    assert parsed.version == 7, value
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=parsed.int >> 80)


@pytest.fixture
def test_clock():
    return clock.TestClock()


@pytest.fixture
def seeded(driver, world, test_clock):
    test_clock.set(AT, world)
    with clock.using(test_clock):
        ids = create_world(driver, world, owner_user_id=OWNER)
    return world, ids


def test_the_data_file_ships_in_the_package():
    entry = resources.files("grove.seed").joinpath("starter_set.json")
    assert entry.is_file()
    assert json.loads(entry.read_text(encoding="utf-8")) == load_starter_set()


def test_the_data_file_holds_names_and_declared_fields_and_no_ids():
    starter = load_starter_set()
    assert len(starter["kinds"]) == 12
    assert len(starter["meanings"]) == 11
    columns = {
        name: {prop for column in table(name).columns for prop in column.props}
        for name in ("kinds", "meanings")
    }
    for name, rows in (("kinds", starter["kinds"]), ("meanings", starter["meanings"])):
        for row in rows:
            assert "id" not in row
            assert set(row) - {"aliases"} <= columns[name]
    aliases = [a for m in starter["meanings"] for a in m["aliases"]]
    assert all(set(alias) <= {"phrase", "reversed"} for alias in aliases)
    assert all(alias.get("reversed", True) is True for alias in aliases)


def test_counts_per_label(driver, seeded):
    world, _ = seeded
    assert count(driver, world, "Kind") == 12
    assert count(driver, world, "Meaning") == 11
    assert count(driver, world, "Alias") == ALIAS_COUNT
    assert count(driver, world, "ChangeEntry") == SEEDED_ROWS
    assert count(driver, world, "HistoryValue") == HISTORY_VALUES
    assert world_row(driver, world) is not None


def test_each_kind_has_its_class_and_vocabulary(driver, seeded):
    world, ids = seeded
    rows = {row["name"]: row for row in nodes(driver, world, "Kind")}
    assert set(rows) == set(KINDS)
    assert ids["kinds"] == {name: row["id"] for name, row in rows.items()}
    for name, (lifecycle_class, vocabulary) in KINDS.items():
        row = rows[name]
        assert row["lifecycle_class"] == lifecycle_class, name
        stated = json.loads(row["status_vocabulary"])
        assert [(entry["status"], entry["role"]) for entry in stated] == vocabulary, name
        assert all(entry["role"] in ENUMS["status_role"] for entry in stated), name
        assert all(entry["role"] != "reported_done" for entry in stated), name
        assert row["required_fields"] == "[]"
        assert row["required_links"] == "[]"
        assert row["propagating_fields"] == "[]"


def test_each_meaning_has_its_declared_fields(driver, seeded):
    world, ids = seeded
    kind_names = {kind_id: name for name, kind_id in ids["kinds"].items()}
    rows = {row["name"]: row for row in nodes(driver, world, "Meaning")}
    assert set(rows) == set(MEANINGS)
    assert ids["meanings"] == {name: row["id"] for name, row in rows.items()}

    def endpoint(text):
        stated = json.loads(text)
        if stated.get("any"):
            assert stated == {"any": True}
            return ANY
        return {kind_names[kind_id] for kind_id in stated["kinds"]} | set(stated["classes"])

    for name, expected in MEANINGS.items():
        row = rows[name]
        assert (
            endpoint(row["from_kinds"]),
            endpoint(row["to_kinds"]),
            row["cardinality"],
            row["carries_consequences"],
            row.get("downstream_end"),
            row["decomposition"],
            row["rolls_up_completion"],
            row["temporal_class"],
            row.get("default_horizon"),
        ) == expected, name
        assert row["direction"] == "from_to", name


def test_every_registry_row_is_a_canonical_starter_row(driver, seeded):
    world, _ = seeded
    reason = None
    for label in ("Kind", "Meaning", "Alias"):
        for row in nodes(driver, world, label):
            assert row["status"] == "canonical"
            assert row["origin"] == "starter"
            assert row["minted_by"] == "seed"
            assert row["version"] == 1
            reason = reason or row["reason"]
            assert row["reason"] == reason
    assert history_text(driver, world, reason) == "starter set"
    for label in ("Kind", "Meaning"):
        for row in nodes(driver, world, label):
            assert row["primitive"] is False
            assert row["uses"] == 0
            assert row["distinct_episodes"] == 0


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("Kind", "kinds"),
        ("Alias", "aliases"),
        ("ChangeEntry", "change_entries"),
        ("HistoryValue", "history_values"),
    ],
)
def test_every_column_holds_a_value_unless_the_spec_leaves_it_empty(driver, seeded, label, name):
    world, _ = seeded
    columns = {prop for column in table(name).columns for prop in column.props}
    for row in nodes(driver, world, label):
        assert set(row) == columns - EMPTY_AT_BIRTH[name], row.get("id")


def test_every_meaning_column_holds_a_value_unless_the_spec_leaves_it_empty(driver, seeded):
    world, _ = seeded
    columns = {prop for column in table("meanings").columns for prop in column.props}
    for row in nodes(driver, world, "Meaning"):
        name = row["name"]
        empty = set(EMPTY_AT_BIRTH["meanings"])
        if MEANINGS[name][4] is None:
            empty.add("downstream_end")
        if MEANINGS[name][8] is None:
            empty.add("default_horizon")
        assert set(row) == columns - empty, name


def test_the_world_row_holds_every_column(driver, seeded):
    world, _ = seeded
    row = world_row(driver, world)
    assert set(row) == {prop for column in table("worlds").columns for prop in column.props}
    assert row["id"] == world
    assert row["owner_user_id"] == OWNER
    assert row["seed_version"] == load_starter_set()["seed_version"]
    assert row["next_sequence"] == SEEDED_ROWS


def test_every_alias_resolves_to_its_meaning_with_its_flag(driver, seeded):
    world, ids = seeded
    query = WorldQuery(driver, world)
    assert sum(len(aliases) for aliases in ALIASES.values()) == ALIAS_COUNT
    for meaning, aliases in ALIASES.items():
        for phrase, reversed_flag in aliases.items():
            for spelling in (phrase, phrase.replace(" ", "_"), phrase.title().replace(" ", "-")):
                rows = query.read(
                    "MATCH (a:Alias {normalised_phrase: $phrase}) RETURN a",
                    phrase=normalise_phrase(spelling),
                )
                assert len(rows) == 1, spelling
                alias = rows[0]["a"]
                assert alias["phrase"] == phrase
                assert alias["target_type"] == "meaning"
                assert alias["target_id"] == ids["meanings"][meaning], spelling
                assert alias["reversed"] is reversed_flag, spelling


def test_alias_phrases_do_not_collide_with_each_other_or_with_a_name(driver, seeded):
    world, ids = seeded
    phrases = [row["normalised_phrase"] for row in nodes(driver, world, "Alias")]
    assert len(phrases) == len(set(phrases))
    assert not set(phrases) & {normalise_phrase(name) for name in (*KINDS, *MEANINGS)}
    assert set(ids["aliases"]) == set(phrases)


def test_beat_threes_path_is_canonical_before_any_import(driver, seeded):
    world, ids = seeded
    rows = WorldQuery(driver, world).read(
        "MATCH (a:Alias {normalised_phrase: $phrase}) "
        "MATCH (m:Meaning {id: a.target_id}) RETURN m, a.reversed AS flipped",
        phrase=normalise_phrase("Gated_On"),
    )
    assert len(rows) == 1
    meaning = rows[0]["m"]
    assert meaning["id"] == ids["meanings"]["depends on"]
    assert meaning["name"] == "depends on"
    assert meaning["status"] == "canonical"
    assert meaning["carries_consequences"] is True
    assert meaning["downstream_end"] == "from"
    assert rows[0]["flipped"] is False


def test_only_the_connection_meaning_is_generic(driver, seeded):
    world, _ = seeded
    flags = {row["name"]: row["generic"] for row in nodes(driver, world, "Meaning")}
    assert flags == {name: name == "related to" for name in MEANINGS}


def test_every_description_is_a_value_id_that_resolves(driver, seeded):
    world, _ = seeded
    starter = load_starter_set()
    stated = {row["name"]: row["description"] for row in (*starter["kinds"], *starter["meanings"])}
    rows = (*nodes(driver, world, "Kind"), *nodes(driver, world, "Meaning"))
    assert len(rows) == len(stated) == 23
    assert len({row["description"] for row in rows}) == 23
    for row in rows:
        assert uuid7_time(row["description"]) == AT
        assert history_text(driver, world, row["description"]) == stated[row["name"]]


def test_each_entry_rebuilds_its_row(driver, seeded):
    world, _ = seeded
    tables = {"Kind": "kinds", "Meaning": "meanings", "Alias": "aliases"}
    live = {
        row["id"]: (table, as_json_row(row))
        for label, table in tables.items()
        for row in nodes(driver, world, label)
    }
    rebuilt = {}
    for entry in nodes(driver, world, "ChangeEntry"):
        stored = json.loads(history_text(driver, world, entry["new_value_id"]))
        assert stored["row"]["id"] == entry["entity_id"]
        rebuilt[entry["entity_id"]] = (stored["table"], stored["row"])
    assert rebuilt == live


def test_each_entry_says_what_s22_asks(driver, seeded):
    world, _ = seeded
    entries = nodes(driver, world, "ChangeEntry")
    registry_ids = {
        row["id"] for label in ("Kind", "Meaning", "Alias") for row in nodes(driver, world, label)
    }
    assert {entry["entity_id"] for entry in entries} == registry_ids
    for entry in entries:
        assert entry["entity_type"] == "registry_row"
        assert entry["field"] == "row"
        assert entry["actor"] == "seed"
        assert entry["history_only"] is False
        assert entry["world_time"] == entry["recorded_at"] == AT
        assert history_text(driver, world, entry["reason"]) == "starter set"
    assert len({entry["reason"] for entry in entries}) == 1
    commits = {entry["commit_id"] for entry in entries}
    change_sets = {entry["change_set_id"] for entry in entries}
    assert len(commits) == len(change_sets) == 1
    assert commits != change_sets
    assert uuid7_time(commits.pop()) == uuid7_time(change_sets.pop()) == AT


def test_sequences_run_with_no_gap(driver, seeded):
    world, _ = seeded
    sequences = sorted(entry["sequence"] for entry in nodes(driver, world, "ChangeEntry"))
    assert sequences == list(range(1, SEEDED_ROWS + 1))


def test_seeded_rows_carry_the_test_clock_and_not_the_system_clock(driver, seeded):
    world, _ = seeded
    expected = {
        "Kind": ("minted_at", "touched_at"),
        "Meaning": ("minted_at", "touched_at"),
        "Alias": ("minted_at", "touched_at", "added_at"),
        "ChangeEntry": ("recorded_at", "world_time"),
    }
    for label, fields in expected.items():
        for row in nodes(driver, world, label):
            assert {row[field] for field in fields} == {AT}, label
    assert world_row(driver, world)["created_at"] == AT


def test_a_world_with_no_time_on_a_test_clock_is_refused(driver, world, test_clock):
    with clock.using(test_clock), pytest.raises(clock.ClockNotSet):
        create_world(driver, world, owner_user_id=OWNER)
    assert world_row(driver, world) is None


def test_ids_are_uuid7_from_the_clock_and_differ_from_the_primitive_ids(driver, seeded):
    world, ids = seeded
    seeded_ids = [
        row_id for group in ("kinds", "meanings", "aliases") for row_id in ids[group].values()
    ]
    assert len(seeded_ids) == len(set(seeded_ids)) == SEEDED_ROWS
    assert not set(seeded_ids) & set(PRIMITIVE_IDS)
    assert all(uuid7_time(row_id) == AT for row_id in seeded_ids)


def test_two_worlds_seed_with_no_conflict_and_different_ids(driver, make_world):
    first, second = make_world(), make_world()
    a = create_world(driver, first, owner_user_id=OWNER)
    b = create_world(driver, second, owner_user_id=OWNER)
    assert a["kinds"].keys() == b["kinds"].keys()
    assert a["meanings"].keys() == b["meanings"].keys()
    assert a["aliases"].keys() == b["aliases"].keys()
    for group in ("kinds", "meanings", "aliases"):
        assert not set(a[group].values()) & set(b[group].values())
    assert count(driver, first, "Kind") == count(driver, second, "Kind") == 12


def test_a_second_call_is_a_no_op(driver, seeded, test_clock):
    world, ids = seeded
    before = snapshot(driver, world)
    row_before = world_row(driver, world)

    test_clock.advance(world_id=world, days=3)
    with clock.using(test_clock):
        again = create_world(driver, world, owner_user_id=OWNER)

    assert again == ids
    assert snapshot(driver, world) == before
    assert world_row(driver, world) == row_before


def test_a_second_call_keeps_the_first_owner(driver, seeded):
    world, _ = seeded
    again = create_world(driver, world, owner_user_id="someone-else")
    assert again["owner_user_id"] == OWNER
    assert world_row(driver, world)["owner_user_id"] == OWNER


def test_two_callers_at_once_seed_one_world(driver, world):
    def call(_):
        return create_world(driver, world, owner_user_id=OWNER)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(call, range(2))

    assert first == second
    assert count(driver, world, "ChangeEntry") == SEEDED_ROWS
    assert count(driver, world, "Kind") == 12


def test_a_failure_on_the_last_row_rolls_the_whole_world_back(driver, world, monkeypatch):
    real = worlds._add_row
    calls = []

    def fail_on_last(*args):
        calls.append(args)
        if len(calls) == SEEDED_ROWS:
            raise RuntimeError("the last row fails")
        return real(*args)

    monkeypatch.setattr(worlds, "_add_row", fail_on_last)
    with pytest.raises(RuntimeError, match="the last row fails"):
        create_world(driver, world, owner_user_id=OWNER)

    assert len(calls) == SEEDED_ROWS
    assert everything_in(driver, world) == 0
    assert world_row(driver, world) is None

    monkeypatch.setattr(worlds, "_add_row", real)
    create_world(driver, world, owner_user_id=OWNER)
    sequences = sorted(entry["sequence"] for entry in nodes(driver, world, "ChangeEntry"))
    assert sequences == list(range(1, SEEDED_ROWS + 1))


def test_primitive_rows_exist_once_across_worlds(driver, make_world):
    create_world(driver, make_world(), owner_user_id=OWNER)
    create_world(driver, make_world(), owner_user_id=OWNER)
    seed_primitives(driver)

    query = WorldQuery.primitives(driver)
    for meaning_id in PRIMITIVE_MEANING_IDS:
        rows = query.read("MATCH (m:Meaning {id: $id}) RETURN m", id=meaning_id)
        assert len(rows) == 1
        row = rows[0]["m"]
        assert row["world_id"] == PRIMITIVE_WORLD
        assert row["primitive"] is True
        assert row["carries_consequences"] is False
        assert row["name"] == PRIMITIVE_MEANINGS[meaning_id]["name"]

    rows = query.read("MATCH (k:Kind {id: $id}) RETURN k", id=KIND_BELIEF)
    assert len(rows) == 1
    assert rows[0]["k"]["world_id"] == PRIMITIVE_WORLD
    assert rows[0]["k"]["primitive"] is True
    assert rows[0]["k"]["name"] == PRIMITIVE_KIND_BELIEF["name"]
    assert rows[0]["k"]["lifecycle_class"] == PRIMITIVE_KIND_BELIEF["lifecycle_class"]


def test_the_fixed_ids_are_the_five_named_rows():
    assert set(PRIMITIVE_MEANINGS) == {
        MEANING_DESCRIBES,
        MEANING_SUPERSEDED_BY,
        MEANING_CORRECTED_BY,
        MEANING_AFFECTED_BY,
    }


def test_primitive_rows_are_outside_every_world_scope(driver, seeded):
    world, _ = seeded
    query = WorldQuery(driver, world)
    assert query.read("MATCH (m:Meaning {id: $id}) RETURN m", id=MEANING_DESCRIBES) == []
    assert query.read("MATCH (k:Kind {id: $id}) RETURN k", id=KIND_BELIEF) == []
    assert not {row["id"] for row in nodes(driver, world, "Meaning")} & set(PRIMITIVE_IDS)


def test_seed_primitives_changes_nothing_on_a_second_run(driver, migrated):
    seed_primitives(driver)
    query = WorldQuery.primitives(driver)
    read = "MATCH (m:Meaning {id: $id}) RETURN m"
    before = query.read(read, id=MEANING_DESCRIBES)
    seed_primitives(driver)
    assert query.read(read, id=MEANING_DESCRIBES) == before


def test_a_world_writer_cannot_be_opened_on_the_primitive_world(driver):
    with pytest.raises(ScopeError):
        WorldWriter(driver, PRIMITIVE_WORLD)


def test_create_world_refuses_the_primitive_world_and_a_bad_id(driver):
    for world_id in (PRIMITIVE_WORLD, "no spaces", "x" * 81):
        with pytest.raises(ScopeError):
            create_world(driver, world_id, owner_user_id=OWNER)


def test_a_world_writer_cannot_reach_a_primitive_row(driver, seeded):
    world, _ = seeded
    query = WorldQuery.primitives(driver)
    read = "MATCH (m:Meaning {id: $id}) RETURN m"
    before = query.read(read, id=MEANING_DESCRIBES)
    writer = WorldWriter(driver, world)

    touched = writer.transaction(
        lambda tx: tx.run(
            "MATCH (m:Meaning {id: $id}) SET m.name = $name RETURN m",
            id=MEANING_DESCRIBES,
            name="renamed",
        )
    )

    assert touched == []
    assert query.read(read, id=MEANING_DESCRIBES) == before


def test_a_world_writer_cannot_mint_a_row_with_a_primitive_id(driver, seeded):
    world, _ = seeded
    writer = WorldWriter(driver, world)
    with pytest.raises(Neo4jError):
        writer.transaction(lambda tx: tx.run("CREATE (m:Meaning {id: $id})", id=MEANING_DESCRIBES))
    assert count(driver, world, "Meaning") == 11


def test_a_world_writer_cannot_stamp_the_primitive_world(driver, seeded):
    world, _ = seeded
    writer = WorldWriter(driver, world)
    with pytest.raises(ScopeError):
        writer.transaction(
            lambda tx: tx.run("CREATE (m:Meaning) SET m += $row", row={"world_id": PRIMITIVE_WORLD})
        )
    with pytest.raises(ScopeError):
        writer.transaction(lambda tx: tx.run("MATCH (m:Meaning) SET m.world_id = ''"))
