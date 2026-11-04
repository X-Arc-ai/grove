"""Exact inactive authority schema and mutation controls, without a live store."""

from pathlib import Path

import pytest

from grove.lints.schema_rule import (
    AUTHORITY_DIGEST_COLUMNS,
    CATALOGUE,
    NON_TEXT_TYPES,
    check,
)
from grove.lints.schema_rule import (
    ENUMS as ENUM_PATH,
)
from grove.store.catalogue import AUTHORITY_TABLES, TABLES, TYPE_CLASSES
from grove.store.enums import ENUMS
from grove.store.storemap import DOC, render
from grove.writer.forget_plan import _PROTECTED, PLAN_KEYS, validate_plan

ROOT = Path(__file__).resolve().parents[3]
NAMES = {"authority_projections", "authority_operations", "authority_approval_uses"}
DIGESTS = {
    "authority_projections": {
        "profile_digest",
        "head_digest",
        "state_digest",
        "ledger_file_digest",
        "ledger_rows_digest",
    },
    "authority_operations": {
        "profile_digest",
        "intent_digest",
        "prepare_head_digest",
        "base_head_digest",
        "base_state_digest",
        "expected_result_digest",
        "choice_digest",
        "result_state_digest",
        "result_ledger_file_digest",
        "result_ledger_rows_digest",
    },
    "authority_approval_uses": {"choice_digest", "display_digest"},
}
TYPES = {
    "authority_projections": {
        "id": "world_id owner_id installation_id active_intent_id",
        "code": "profile_code ledger_format",
        "int": "installation_epoch authority_epoch erasure_revision sequence ledger_count fence",
        "bool": "ready",
    },
    "authority_operations": {
        "id": (
            "id idempotency_key world_id owner_id installation_id prepare_event_id "
            "original_approval_id attempt_id operation_id barrier_id renewed_approval_id"
        ),
        "code": "mode result_ledger_format",
        "int": (
            "prepare_sequence base_sequence installation_epoch authority_epoch erasure_revision "
            "attempt_fence revoked_through barrier_fence result_installation_epoch "
            "result_authority_epoch result_erasure_revision result_ledger_count "
            "d3_sequence_before d3_sequence_after"
        ),
        "instant": "registered_at terminal_at",
        "enum:authority_phase": "phase",
        "enum:authority_outcome": "outcome",
        "enum:authority_resolution": "resolution",
    },
    "authority_approval_uses": {
        "id": "id approval_id world_id user_id installation_id intent_id",
        "int": "installation_epoch authority_epoch",
        "instant": "used_at",
        "enum:authority_use_kind": "use_kind",
        "enum:authority_use_role": "role",
    },
}
CONSTRAINTS = {
    "authority_projection_world_unique": ("AuthorityProjection", ("world_id",)),
    "authority_operation_id_unique": ("AuthorityOperation", ("id",)),
    "authority_operation_key_unique": ("AuthorityOperation", ("world_id", "idempotency_key")),
    "authority_approval_use_id_unique": ("AuthorityApprovalUse", ("id",)),
    "authority_approval_use_world_unique": ("AuthorityApprovalUse", ("world_id", "approval_id")),
}


def test_exact_authority_tables_columns_and_constraints():
    assert {t.name for t in AUTHORITY_TABLES} == NAMES
    assert [t for t in TABLES if t.name in NAMES] == list(AUTHORITY_TABLES)
    assert {t.name for t in TABLES if t.group == "ledger"} == NAMES | {"forget_ledger"}
    found = {}
    for table in AUTHORITY_TABLES:
        assert (table.scope, table.group, table.element, table.owner) == (
            "world",
            "ledger",
            "node",
            "forget",
        )
        assert not table.indexes
        expected = {
            prop: kind for kind, props in TYPES[table.name].items() for prop in props.split()
        }
        expected.update(dict.fromkeys(DIGESTS[table.name], "digest"))
        actual = {prop: col.type for col in table.columns for prop in col.props}
        assert sum(len(col.props) for col in table.columns) == len(actual)
        assert actual == expected
        found.update({c.name: (table.label, c.props) for c in table.uniques})
    assert found == CONSTRAINTS


def test_exact_digest_boundary_and_closed_codes():
    pairs = {(table, prop) for table, props in DIGESTS.items() for prop in props}
    assert len(pairs) == 17
    assert AUTHORITY_DIGEST_COLUMNS == pairs
    assert "digest" in TYPE_CLASSES
    assert "digest" not in NON_TEXT_TYPES
    assert {key: value for key, value in ENUMS.items() if key.startswith("authority_")} == {
        "authority_phase": ("active", "fenced", "applied", "cancelled"),
        "authority_outcome": ("COMMITTED_EXACT", "CANCELLED_EXACT"),
        "authority_resolution": (
            "apply",
            "complete_restriction",
            "complete_reapproved",
            "cancel_unapplied_expansion",
        ),
        "authority_use_kind": ("applied", "cancelled"),
        "authority_use_role": ("original", "renewed"),
    }


@pytest.fixture
def schema_root(tmp_path):
    for relative in (CATALOGUE, ENUM_PATH):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    assert check(tmp_path) == []
    return tmp_path


def append_property(root, table, prop, kind):
    with (root / CATALOGUE).open("a") as output:
        output.write(
            "\nfrom dataclasses import replace\n"
            "TABLES = tuple(replace(t, columns=(*t.columns, "
            f"Column('', ({prop!r},), {kind!r}))) "
            f"if t.name == {table!r} else t for t in TABLES)\n"
        )


@pytest.mark.parametrize(
    "table,prop",
    [
        ("items", "profile_digest"),
        ("authority_operations", "private_note"),
        ("authority_approval_uses", "head_digest"),
    ],
)
def test_digest_cannot_escape_its_exact_property_allowlist(schema_root, table, prop):
    append_property(schema_root, table, prop, "digest")
    assert [finding.rule for finding in check(schema_root)] == ["schema/digest-outside-authority"]


@pytest.mark.parametrize("table", sorted(NAMES))
@pytest.mark.parametrize("kind", ["text_home", "json_home"])
def test_authority_rows_cannot_add_a_private_text_home(schema_root, table, kind):
    append_property(schema_root, table, "private_note", kind)
    assert [finding.rule for finding in check(schema_root)] == ["schema/text-outside-home"]


def test_every_digest_property_in_a_group_must_be_allowed(schema_root):
    with (schema_root / CATALOGUE).open("a") as output:
        output.write(
            "\nfrom dataclasses import replace\n"
            "TABLES = tuple(replace(t, columns=tuple("
            "replace(c, props=(*c.props, 'private_note')) "
            "if c.props == ('head_digest',) else c for c in t.columns)) "
            "if t.name == 'authority_projections' else t for t in TABLES)\n"
        )
    assert [finding.rule for finding in check(schema_root)] == ["schema/digest-outside-authority"]


@pytest.mark.parametrize("table", ["authority_operations", "authority_approval_uses"])
def test_ordinary_forget_plan_refuses_protected_authority_rows(table):
    source = {key: 2 if key == "version" else [] for key in PLAN_KEYS}
    source["delete"] = [{"table": "items", "key": {"id": "synthetic-id"}}]
    validate_plan(source)
    source["delete"][0]["table"] = table
    with pytest.raises(ValueError, match="^invalid forget deletion$"):
        validate_plan(source)


def test_all_authority_facts_are_protected_and_documented():
    assert NAMES <= _PROTECTED
    current = DOC.read_text()
    assert render(current) == current
