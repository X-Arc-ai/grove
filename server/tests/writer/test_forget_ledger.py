"""External ledger failure controls. No store or physical backup is touched."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from grove.writer.forget import validate_ledger_rows
from grove.writer.forget_ledger import LedgerUnavailable, append_intent, locked_ledger, read_ledger


@pytest.fixture
def world_id():
    return f"t_forget_{uuid4()}"


@pytest.fixture
def row(world_id):
    return {
        "id": str(uuid4()),
        "world_id": world_id,
        "target_type": "item",
        "internal_ids": [str(uuid4())],
        "locators": [],
        "approved_at": datetime(2026, 11, 4, tzinfo=UTC),
    }


def test_intent_round_trip_is_private_and_idempotent(tmp_path, world_id, row):
    with locked_ledger(tmp_path) as root:
        append_intent(root, world_id, row, [])
        append_intent(root, world_id, row, [row])
    rows = read_ledger(tmp_path, world_id)
    assert len(rows) == 1 and rows[0]["id"] == row["id"]
    assert all(path.stat().st_mode & 0o077 == 0 for path in tmp_path.iterdir())


def test_changed_retry_intent_refuses(tmp_path, world_id, row):
    with locked_ledger(tmp_path) as root:
        append_intent(root, world_id, row, [])
        with pytest.raises(LedgerUnavailable, match="changed during retry"):
            append_intent(root, world_id, {**row, "internal_ids": []}, [row])


def test_external_file_must_cover_all_committed_rows(tmp_path, world_id, row):
    with locked_ledger(tmp_path) as root:
        append_intent(root, world_id, row, [])
        missing = {**row, "id": str(uuid4())}
        with pytest.raises(LedgerUnavailable, match="does not cover"):
            append_intent(root, world_id, {**row, "id": str(uuid4())}, [row, missing])


@pytest.mark.parametrize("field", ["sha256", "count", "world_id", "format", "rows"])
def test_corrupt_envelope_refuses(tmp_path, world_id, row, field):
    with locked_ledger(tmp_path) as root:
        append_intent(root, world_id, row, [])
    path = next(tmp_path.glob("*.json"))
    data = json.loads(path.read_text())
    data[field] = [] if field == "rows" else "invalid"
    path.write_text(json.dumps(data))
    with pytest.raises(LedgerUnavailable):
        read_ledger(tmp_path, world_id)


def test_missing_latest_ledger_refuses(tmp_path, world_id):
    with pytest.raises(LedgerUnavailable, match="absent"):
        read_ledger(tmp_path, world_id)


def test_linked_ledger_directory_refuses(tmp_path, world_id):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(LedgerUnavailable, match="not private"):
        read_ledger(link, world_id)


def test_non_private_directory_refuses(tmp_path, world_id):
    tmp_path.chmod(0o755)
    with pytest.raises(LedgerUnavailable, match="not private"):
        read_ledger(tmp_path, world_id)


def test_legacy_sparse_replay_rows_remain_supported(world_id, row):
    sparse = {key: row[key] for key in ("target_type", "internal_ids", "approved_at")}
    assert validate_ledger_rows(world_id, [sparse]) == [sparse]


def test_planned_rows_require_identity_before_replay(world_id, row):
    invalid = {k: v for k, v in row.items() if k != "id"}
    invalid["plan"] = "{}"
    with pytest.raises(ValueError, match="identity"):
        validate_ledger_rows(world_id, [invalid])
