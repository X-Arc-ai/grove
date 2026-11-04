"""The catalogue against B.1, the enums and the migrations. Needs no live store."""

import re
from pathlib import Path

import pytest

from grove.store.catalogue import (
    B1_TABLES,
    FULLTEXT_INDEXES,
    LATER_TABLES,
    PLATFORM_LABELS,
    RELATIONSHIP_TYPES,
    TABLES,
    TYPE_CLASSES,
    WORLD_LABELS,
    table,
)
from grove.store.enums import ENUMS
from grove.store.primitives import KIND_BELIEF, PRIMITIVE_IDS, PRIMITIVE_WORLD
from tests.writer.migration_forms import is_generation_initializer

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

B1_NAMES = [
    "items",
    "links",
    "kinds",
    "meanings",
    "property_names",
    "aliases",
    "ontology_change_log",
    "distinct_pairs",
    "areas",
    "connectors",
    "capabilities",
    "sources",
    "episodes",
    "evidence",
    "holds",
    "unresolved_mentions",
    "screening_receipts",
    "change_entries",
    "history_values",
    "requests",
    "declines",
    "jobs",
    "run_records",
    "model_calls",
    "judgments",
    "budgets",
    "verifications",
    "horizon_stats",
    "impacts",
    "cascade_proposals",
    "beliefs",
    "contradictions",
    "support_changed",
    "questions",
    "area_audits",
    "walk_rotation",
    "checks",
    "audit_runs",
    "audit_grades",
    "findings",
    "forget_ledger",
    "oauth_clients",
]

# Line 2561, the homes of free text, with the status words of S31.
TEXT_HOMES = {
    ("sources", "content"),
    ("items", "title"),
    ("items", "summary"),
    ("items", "body"),
    ("items", "properties"),
    ("items", "property_values"),
    ("items", "search_terms"),
    ("items", "status"),
    ("kinds", "status_vocabulary"),
    ("aliases", "phrase"),
    ("aliases", "normalised_phrase"),
    ("kinds", "name"),
    ("meanings", "name"),
    ("property_names", "name"),
    ("history_values", "value"),
    ("run_records", "proposed_operations"),
}

PLAIN_TYPES = {
    "id",
    "ids",
    "value_id",
    "value_ids",
    "ref",
    "text_home",
    "instant",
    "date",
    "int",
    "float",
    "bool",
    "json_ids",
    "json_home",
    "locator",
    "code",
    "config",
    "digest",
}

# Words per form, after the first word. Python strings here must stay clear of the writer lint.
STATEMENT_FORMS = {"CONSTRAINT": 1, "RANGE": 2, "TEXT": 2, "FULLTEXT": 2}


def props_of(t):
    return [p for c in t.columns for p in c.props]


def statements():
    found = []
    for path in sorted(MIGRATIONS.glob("*.cypher")):
        lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("//")]
        for part in "\n".join(lines).split(";"):
            if part.strip():
                found.append((path.name, " ".join(part.split())))
    return found


def catalogue_schema_names():
    names = {f.name for f in FULLTEXT_INDEXES}
    for t in TABLES:
        names |= {c.name for c in t.uniques} | {i.name for i in t.indexes}
    return names


def test_the_42_names_in_b1_order():
    assert list(B1_TABLES) == B1_NAMES
    assert len(B1_NAMES) == 42


def test_table_lookup_and_platform_rows_follow_b1():
    assert table("items").label == "Item"
    assert [t.name for t in TABLES[:42]] == B1_NAMES
    platform = TABLES[42 : len(TABLES) - len(LATER_TABLES)]
    assert {t.name for t in platform} == {
        "worlds",
        "users",
        "sessions",
        "oauth_tokens",
        "authorization_codes",
        "email_codes",
        "upload_tokens",
        "migrations",
    }
    assert all(t.scope == "platform" and t.group == "platform" for t in platform)


def test_tables_of_later_lanes_come_last():
    assert TABLES[len(TABLES) - len(LATER_TABLES) :] == LATER_TABLES
    positions = table("map_positions")
    assert positions in LATER_TABLES
    assert (positions.label, positions.scope, positions.group) == (
        "MapPosition",
        "world",
        "snapshot",
    )
    assert positions.owner == "views-a"
    assert [c.props for c in positions.uniques] == [("id",), ("world_id", "item_id")]
    assert {c.origin for c in positions.columns} == {"convention"}
    assert "MapPosition" in WORLD_LABELS


def test_labels_follow_the_scopes():
    assert "PastValue" in WORLD_LABELS
    assert {"Item", "Kind", "Evidence", "ForgetLedger"} <= WORLD_LABELS
    assert {"World", "User", "OAuthClient", "OAuthToken", "Migration"} <= PLATFORM_LABELS
    assert not WORLD_LABELS & PLATFORM_LABELS
    assert RELATIONSHIP_TYPES == {"LINK"}
    assert table("links").element == "relationship"


def test_restore_groups_match_lines_806_to_808():
    projection = {t.name for t in TABLES if t.group == "projection"}
    assert projection == {
        "items",
        "links",
        "evidence",
        "kinds",
        "meanings",
        "property_names",
        "aliases",
        "capabilities",
        "beliefs",
    }
    assert {t.name for t in TABLES if t.group == "ledger"} == {
        "forget_ledger",
        "authority_projections",
        "authority_operations",
        "authority_approval_uses",
    }
    assert len([t for t in TABLES[:42] if t.group == "snapshot"]) == 32


@pytest.mark.parametrize("t", TABLES, ids=lambda t: t.name)
def test_each_table_has_an_id_or_key(t):
    assert t.uniques, t.name


@pytest.mark.parametrize("t", TABLES, ids=lambda t: t.name)
def test_property_names_are_unique_and_snake_case(t):
    props = props_of(t)
    assert len(props) == len(set(props)), t.name
    for prop in props:
        assert re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)*", prop), prop


@pytest.mark.parametrize("t", TABLES[:42], ids=lambda t: t.name)
def test_world_id_is_on_every_b1_row(t):
    assert "world_id" in props_of(t), t.name


@pytest.mark.parametrize("t", TABLES, ids=lambda t: t.name)
def test_every_type_and_origin_is_known(t):
    for c in t.columns:
        assert c.origin in {"b1", "convention", "ruling"}
        assert c.type in PLAIN_TYPES or c.type.startswith("enum:"), c
        if c.origin == "b1":
            assert c.spec, c
        if not c.spec:
            assert c.origin != "b1", c


def test_every_enum_exists_and_is_closed():
    used = {
        c.type.split(":", 1)[1] for t in TABLES for c in t.columns if c.type.startswith("enum:")
    }
    assert used <= set(ENUMS), used - set(ENUMS)
    for name, codes in ENUMS.items():
        assert codes and len(codes) == len(set(codes)), name
    for name in (
        "lifecycle_class",
        "status_role",
        "end_class",
        "source_type",
        "statement_class",
        "derivation",
        "speaker_kind",
        "modality",
        "polarity",
        "verdict",
        "disposition",
        "temporal_class",
        "precision",
        "capability",
        "cardinality",
        "evidence_end_reason",
        "belief_state",
        "belief_end_class",
        "request_status",
        "job_state",
        "job_lane",
        "budget_state",
        "connector_type",
        "proposal_type",
    ):
        assert name in ENUMS


def test_enum_values_against_the_spec():
    assert ENUMS["request_status"] == ("queued", "applying", "saved", "declined", "failed")
    assert ENUMS["end_class"] == (
        "ended_in_the_world",
        "refuted",
        "superseded",
        "retired",
        "spent",
    )
    assert ENUMS["lifecycle_class"] == (
        "lasting_thing",
        "work_with_state",
        "record",
        "bounded_period",
    )
    assert ENUMS["proposal_type"] == ("cascade", "cascade_failed", "wide_impact", "reopen")


def test_schema_names_are_unique():
    names = [f.name for f in FULLTEXT_INDEXES]
    for t in TABLES:
        names += [c.name for c in t.uniques] + [i.name for i in t.indexes]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("t", TABLES, ids=lambda t: t.name)
def test_constraint_and_index_props_are_column_props(t):
    props = set(props_of(t))
    for item in (*t.uniques, *t.indexes):
        assert set(item.props) <= props, item


def test_no_two_schema_rows_share_one_schema():
    """IF NOT EXISTS no-ops on the same schema under another name, and the name goes missing."""
    seen = set()
    for t in TABLES:
        for item in t.uniques:
            key = (t.label, item.props, "range")
            assert key not in seen, item
            seen.add(key)
        for item in t.indexes:
            key = (t.label, item.props, item.kind)
            assert key not in seen, item
            seen.add(key)


def test_fulltext_props_exist_on_a_labelled_table():
    by_label = {t.label: t for t in TABLES}
    by_label["PastValue"] = table("history_values")
    for f in FULLTEXT_INDEXES:
        props = set()
        for label in f.labels:
            props |= set(props_of(by_label[label]))
        assert set(f.props) <= props, f.name
        assert f.analyzer == "standard-folding"
        assert f.props[0] == "world_id"


def test_text_columns_stay_in_their_homes():
    found = set()
    for t in TABLES:
        for c in t.columns:
            if c.type in {"text_home", "json_home"}:
                found |= {(t.name, p) for p in c.props}
    assert found == TEXT_HOMES


def test_rulings_reach_the_columns():
    links = set(props_of(table("links")))
    assert {"active_key", "original_phrase", "touched_at", "became_true_precision"} <= links
    assert {"status_role", "property_values", "became_true_words"} <= set(props_of(table("items")))
    assert {"source_offset_minutes", "person_timezone"} <= set(props_of(table("evidence")))
    assert ("world_id", "id") in [c.props for c in table("requests").uniques]
    assert ("world_id", "sequence") in [c.props for c in table("change_entries").uniques]
    for name in (
        "links",
        "kinds",
        "meanings",
        "property_names",
        "aliases",
        "capabilities",
        "evidence",
        "beliefs",
        "holds",
        "unresolved_mentions",
        "contradictions",
        "questions",
        "impacts",
        "cascade_proposals",
        "connectors",
        "areas",
        "findings",
        "episodes",
        "sources",
    ):
        assert {"version", "touched_at"} <= set(props_of(table(name))), name


def column(table_name, prop):
    return next(c for c in table(table_name).columns if prop in c.props)


def test_the_type_classes_are_listed_in_the_catalogue():
    assert set(TYPE_CLASSES) == PLAIN_TYPES | {"enum:<name>"}
    assert all(TYPE_CLASSES.values())


def test_checks_rows_carry_the_real_world_id():
    """S35: a check's state and value measure one world."""
    world = column("checks", "world_id")
    assert "S35" in world.note
    assert "''" not in world.note and "store-wide" not in world.note
    assert table("checks").scope == "world"


def test_status_words_are_registry_names():
    """S31: an agent can mint a status word, so it lives in a text home."""
    assert column("items", "status").type == "text_home"
    assert column("kinds", "status_vocabulary").type == "json_home"


def test_subjects_are_references_and_client_ids_are_locators():
    """S32: a pair or a word with no row is not one UUIDv7 id."""
    assert column("judgments", "subject").type == "ref"
    assert column("audit_grades", "subject").type == "ref"
    assert column("findings", "subject").type == "id"
    assert column("oauth_clients", "client_id").type == "locator"
    assert column("oauth_tokens", "client_id").type == "locator"
    assert column("authorization_codes", "client_id").type == "locator"


def test_connector_outcome_and_verdict_codes():
    """Line 2391: a read that found items, and the two cases of could not read."""
    assert ENUMS["connector_outcome"] == ("took_in", "nothing_new", "denied", "failed", "kept_out")
    assert "not_supported" not in ENUMS["verdict"]  # S33
    assert ENUMS["verdict"][:3] == ("supports", "contradicts", "not_about_claim")


def test_a_hold_promotes_to_a_list():
    """Line 2191: promoted_to_refs[]."""
    assert column("holds", "promoted_to").type == "ids"


def test_scheduled_role_cites_its_spec_line():
    note = column("property_names", "scheduled_role").note
    assert "2067" in note and "2064" not in note


def test_no_owner_or_note_points_to_the_private_plan():
    for t in TABLES:
        texts = [t.owner, t.notes, *(c.note for c in t.columns)]
        assert not any("handoff" in s.lower() for s in texts), t.name
    assert table("worlds").owner == "platform; signin"


def test_the_jobs_note_names_the_locked_check():
    """A filter in MATCH or WHERE reads with no lock, and two runners claim one job."""
    notes = table("jobs").notes
    assert "reads with no lock" in notes
    assert "CASE" in notes and "WITH" in notes
    assert "claimed by a direct SET" not in notes


def test_verifications_hold_the_columns_of_line_985():
    props = set(props_of(table("verifications")))
    assert props == {
        "id",
        "world_id",
        "claim_id",
        "run_id",
        "at",
        "evidence_pieces",
        "second_judge_judgment_ids",
        "correlated",
        "agree",
        "disagree",
        "ratio",
        "band",
        "loadbearing",
        "disposition",
        "override_reason",
        "recheck_by_before",
        "recheck_by_after",
        "reason",
        "request_id",
        "commit_id",
    }


def schema_name(statement):
    """Check the form of one statement and return its schema name."""
    if is_generation_initializer(statement):
        return None
    words = statement.split()
    assert words[0] == "CREATE", statement
    size = STATEMENT_FORMS[words[1]]
    assert words[1 + size + 1 : 1 + size + 5] == ["IF", "NOT", "EXISTS", "FOR"], statement
    return words[1 + size]


def test_every_migration_statement_has_an_allowed_form():
    found = statements()
    assert found
    for _, statement in found:
        schema_name(statement)


def test_first_migration_creates_the_ledger_constraint():
    assert schema_name(statements()[0][1]) == "migrations_name_unique"


def test_fulltext_statements_set_the_analyzer():
    fulltext = [s for _, s in statements() if s.split()[1] == "FULLTEXT"]
    assert len(fulltext) == len(FULLTEXT_INDEXES)
    for statement in fulltext:
        assert "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}" in statement


def test_relationship_statements_use_the_relationship_form():
    for _, statement in statements():
        if ":LINK" in statement:
            assert "FOR ()-[r:LINK]-()" in statement


def test_catalogue_names_equal_migration_names():
    in_files = [name for _, s in statements() if (name := schema_name(s)) is not None]
    assert len(in_files) == len(set(in_files))
    assert set(in_files) == catalogue_schema_names()


def assert_migration_names(names):
    assert names
    numbers = []
    for name in names:
        match = re.fullmatch(r"([0-9]{4})_[a-z][a-z0-9_]*\.cypher", name)
        assert match is not None, name
        number = int(match[1])
        assert number > 0, name
        numbers.append(number)
    assert numbers == sorted(set(numbers)), "migration versions must be unique and ordered"


def test_migration_files_have_unique_positive_versions_and_valid_order():
    # Independent additive migrations can merge in a different order from their reservations.
    # Their full names and checksums remain the migration ledger's identities.
    assert_migration_names([p.name for p in sorted(MIGRATIONS.glob("*.cypher"))])


def test_migration_names_allow_reserved_number_gaps():
    assert_migration_names(["0001_ledger.cypher", "0013_source_connections.cypher"])


@pytest.mark.parametrize(
    "names",
    [
        [],
        ["0000_invalid.cypher"],
        ["001x_invalid.cypher"],
        ["0011invalid.cypher"],
        ["0011_Invalid.cypher"],
        ["0011_one.cypher", "0011_two.cypher"],
        ["0013_later.cypher", "0011_earlier.cypher"],
    ],
)
def test_migration_name_guard_rejects_invalid_duplicate_or_unordered_versions(names):
    with pytest.raises(AssertionError):
        assert_migration_names(names)


def test_primitive_ids_are_fixed():
    assert PRIMITIVE_WORLD == ""
    assert len(set(PRIMITIVE_IDS)) == 5
    assert KIND_BELIEF == "00000000-0000-7000-8000-000000000005"


def test_material_revisions_are_snapshot_metadata_without_text():
    material = table("material_revisions")
    assert material.group == "snapshot" and material.scope == "world"
    assert not any(c.type in {"text_home", "json_home"} for c in material.columns)
    assert {"title_source_id", "source_ids", "request_id"} <= set(props_of(material))
