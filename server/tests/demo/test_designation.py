"""Strict durable designation checks without external data."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from grove import demo_policy as policy

ROW = {
    "world_id": "world",
    "user_id": "owner",
    "self_item_id": "self",
    "email_hash": "a" * 64,
    "demo_version": "cedar-v1",
    "demo_world_id": "world",
    "designated_user_id": "owner",
    "claimed_demo_version": "cedar-v1",
}


def test_duplicate_designation_rows_refuse_before_any_mutation(monkeypatch):
    monkeypatch.setattr(policy, "collect", lambda *args: [deepcopy(ROW), deepcopy(ROW)])
    runner = SimpleNamespace(run=lambda *args, **kwargs: None)
    tx = SimpleNamespace(world_id="world", unscoped=lambda: runner)
    with pytest.raises(policy.DemoDenied):
        policy.designation_in_tx(tx)


@pytest.mark.parametrize(
    "field",
    [
        "email_hash",
        "demo_version",
        "demo_world_id",
        "user_id",
        "designated_user_id",
        "claimed_demo_version",
    ],
)
def test_partial_or_corrupt_designation_refuses(monkeypatch, field):
    row = deepcopy(ROW)
    row[field] = None
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", "a" * 64)
    monkeypatch.setattr(policy, "designation_in_tx", lambda tx: row)
    tx = SimpleNamespace(world_id="world")
    with pytest.raises(policy.DemoDenied):
        policy.require_designation_in_tx(tx)


def test_manifest_command_needs_no_account_or_secret(monkeypatch, capsys):
    from grove.demo import rich

    def forbidden():
        pytest.fail("manifest must not open account configuration")

    monkeypatch.setattr(rich, "private_email_hash", forbidden)
    monkeypatch.setattr(rich, "open_driver", forbidden)
    assert rich.main(["manifest"]) == 0
    assert '"items": 2080' in capsys.readouterr().out


def test_seed_refuses_missing_private_target_before_driver(monkeypatch, capsys):
    from grove.demo import rich

    def forbidden():
        pytest.fail("a refused target must not open the driver")

    monkeypatch.delenv("GROVE_DEMO_EMAIL_FILE", raising=False)
    monkeypatch.setattr(rich, "open_driver", forbidden)
    assert rich.main(["seed"]) == 1
    assert capsys.readouterr().out == (
        "synthetic demo operation refused or unavailable; inspect before retry\n"
    )
