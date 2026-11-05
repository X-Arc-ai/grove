"""D14 restoration preserves D4 markers and D3 undo on the same retained claim."""

import json

import pytest

from grove.store.d3_fingerprint import live_fingerprint
from grove.writer.forget import inventory_fingerprint
from grove.writer.forget_ledger import read_ledger
from grove.writer.restore import restore, take_snapshot
from tests.writer import test_verify as verification
from tests.writer.helpers import PERSON, apply, observation
from tests.writer.test_contract_d2 import update_item
from tests.writer.test_contract_d3 import undo
from tests.writer.test_forget import erase, inventory
from tests.writer.test_restore import purge_world_rows
from tests.writer.test_truth_clock_restore import defer
from tests.writer.test_verify import (
    NOW,
    RECHECK,
    confirm_with_two_pages,
    item_row,
    organisation,
)

frozen = verification.frozen
w = verification.w
talk = verification.talk
pages = verification.pages


@pytest.mark.parametrize("mode", ["confirmed", "deferred", "marker_only"])
def test_forget_restore_preserves_verification_and_marker_only_undo(
    driver, w, frozen, talk, pages, tmp_path, mode
):
    selected = "Private cobalt orchard summary"
    fields = {"recheck_by": RECHECK} if mode == "marker_only" else {}
    claim = organisation(driver, w, talk, summary=selected, **fields)
    changed = None
    if mode == "marker_only":
        changed = apply(
            driver,
            w,
            [update_item(claim, 1, {"title": "Revised hall title"}, "rev")],
            [observation(talk, "rev", 3, at=NOW)],
            caller=PERSON,
        )
    if mode == "confirmed":
        confirm_with_two_pages(driver, w, frozen, pages, claim, f"item:{claim}")
    else:
        defer(driver, w, frozen, pages, claim, recheck_by=RECHECK)
    entries = inventory(driver, w)["change_entries"]
    assert any(row["field"] == "verified" and row["entity_id"] == claim for row in entries)
    snapshot = take_snapshot(driver, w)
    erase(driver, w, {"type": "field_value", "item_id": claim, "field": "summary"}, tmp_path)
    ledger = read_ledger(tmp_path, w)
    expected_inventory = inventory(driver, w)
    # The approval was created after the old snapshot and must not reappear on restore.
    assert len(expected_inventory["forget_approvals"]) == 1
    expected_inventory["forget_approvals"] = []
    expected_fingerprint = inventory_fingerprint(expected_inventory)
    expected_claim = item_row(driver, w, claim)
    purge_world_rows(driver, w)
    restore(driver, w, snapshot, ledger, ledger_dir=tmp_path)
    restored = inventory(driver, w)
    assert restored["forget_approvals"] == []
    assert sorted(restored["change_entries"], key=lambda row: row["sequence"]) == sorted(
        entries, key=lambda row: row["sequence"]
    )
    assert selected not in json.dumps(restored, default=str)
    assert item_row(driver, w, claim) == expected_claim
    assert live_fingerprint(driver, w) == expected_fingerprint
    if changed is not None:
        apply(driver, w, [undo(changed.change_set_id)], [], caller=PERSON)
        final = item_row(driver, w, claim)
        assert final["title"] == "Okonkwo Hall"
        assert final["summary"] == "[forgotten]"
