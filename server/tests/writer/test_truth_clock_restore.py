"""D3 rebuild and undo preserve D4 verification history and projection clocks."""

import pytest

from grove.store.d3_fingerprint import live_fingerprint
from grove.writer.restore import rebuild_in_place
from tests.writer import test_verify as verification
from tests.writer.helpers import PERSON, apply, observation
from tests.writer.test_contract_d2 import update_item
from tests.writer.test_contract_d3 import undo
from tests.writer.test_verify import (
    FRESH,
    NOW,
    RECHECK,
    VERIFY_AT,
    confirm_with_two_pages,
    item_row,
    judge,
    organisation,
    page_piece,
    piece,
    verify,
)

frozen = verification.frozen
w = verification.w
talk = verification.talk
pages = verification.pages


def defer(driver, w, frozen, pages, claim, **fields):
    frozen.set(VERIFY_AT)
    observed = page_piece(pages, "unrelated", 2, FRESH)
    judged = judge(driver, w, f"item:{claim}", observed, "not_about_claim")
    return apply(
        driver,
        w,
        [verify(claim, [piece("unrelated", judged)], "deferred", **fields)],
        [observed],
    )


@pytest.mark.parametrize("mode", ["confirmed", "deferred", "marker_only"])
def test_rebuild_preserves_a_verified_claim(driver, w, frozen, talk, pages, mode):
    fields = {"recheck_by": RECHECK} if mode == "marker_only" else {}
    claim = organisation(driver, w, talk, **fields)
    if mode == "confirmed":
        confirm_with_two_pages(driver, w, frozen, pages, claim, f"item:{claim}")
    else:
        defer(driver, w, frozen, pages, claim, recheck_by=RECHECK)
    before = live_fingerprint(driver, w)
    rebuild_in_place(driver, w)
    assert live_fingerprint(driver, w) == before


def test_marker_only_verification_does_not_block_undo(driver, w, frozen, talk, pages):
    claim = organisation(driver, w, talk, summary="The original hall summary", recheck_by=RECHECK)
    changed = apply(
        driver,
        w,
        [update_item(claim, 1, {"summary": "The revised hall summary"}, "rev")],
        [observation(talk, "rev", 3, at=NOW)],
        caller=PERSON,
    )
    defer(driver, w, frozen, pages, claim, recheck_by=RECHECK)
    apply(driver, w, [undo(changed.change_set_id)], [], caller=PERSON)
    assert item_row(driver, w, claim)["summary"] == "The original hall summary"
