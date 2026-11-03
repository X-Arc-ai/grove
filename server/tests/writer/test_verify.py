"""D4's writer contract for verify and the score: the only operation that moves last confirmed.

Written from the spec's "How to test it" for D4 (the sentences about verify and the score, and the
lane tests that the writer can answer), B.2's verify row and Appendix C's numbers, not from the
implementation. Every verdict a test cites is a recorded judgment of a recorded model call; every
expected weight, ratio and band below is computed by hand in the test. All names are invented, and
every time comes from a TestClock.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.writer import BatchDeclined, render_receipt
from grove.writer.judgments import JudgmentRow, record_judgments
from grove.writer.testing import plant_node
from tests.writer.helpers import (
    LEARNER,
    apply,
    assert_only_decline_log,
    clock_at,
    count_rows,
    create_item,
    create_link,
    created_id,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)

# The clock of every test here: the claim is recorded at NOW, the judge reads at VERIFY_AT.
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
VERIFY_AT = NOW + timedelta(hours=1)
FRESH = NOW + timedelta(minutes=30)
FRESHER = NOW + timedelta(minutes=40)
RECHECK = "2027-03-15T09:00:00+00:00"
LATER_RECHECK = "2027-04-01T09:00:00+00:00"
PAGE_LIMIT_DAYS = 7  # Appendix C: a fetched page goes stale in 7 days.

CHAT_LINES = [f"line {n}: Rowan says the Okonkwo Hall venue list is in hand" for n in range(20)]
PAGE_LINES = [
    "Okonkwo Hall: the venue list for Rowan Okonkwo is published and final.",
    "Okonkwo Hall: bookings for the spring season are open to members.",
    "Okonkwo Hall: the venue list was withdrawn for corrections this week.",
    "Okonkwo Hall: the floor plan for the Ilse Varga dinner is attached.",
]
GIT_TOKEN = "c0ffee42"
GIT_LINES = [f"commit {GIT_TOKEN} Publish the Okonkwo Hall venue list"]
IDENTITY_LINES = ["profile: Ilse Varga, born in Velmora, member since 2019"]


# Fixtures and builders ------------------------------------------------------------------------


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(NOW)
    with clock.using(test_clock):
        yield test_clock


@pytest.fixture
def w(frozen, seeded):
    return seeded["world_id"]


@pytest.fixture
def talk(driver, w):
    return make_source(driver, w, "whatsapp", CHAT_LINES)


@pytest.fixture
def pages(driver, w):
    return make_source(driver, w, "web_pages", PAGE_LINES)


def page_piece(src: dict, ev_id: str, line: int, at: datetime, **extra: Any) -> dict[str, Any]:
    """An observation of one line of a fetched page, by its characters (web pages have no lines)."""
    start = sum(len(text) + 1 for text in PAGE_LINES[: line - 1])
    locator = f"char:{start}-{start + len(PAGE_LINES[line - 1])}"
    return observation(src, ev_id, line, at=at, locator=locator, **extra)


def call(driver, w: str, model: str = "judge-a", job: str = "verify", **extra: Any) -> str:
    """A recorded model call that answered."""
    return plant_node(driver, w, "ModelCall", model=model, job=job, outcome="ok", at=NOW, **extra)[
        "id"
    ]


def judge(
    driver,
    w: str,
    subject: str,
    obs: dict[str, Any],
    verdict: str = "supports",
    *,
    role: str = "first_judge",
    model: str = "judge-a",
    correlated: bool = False,
) -> str:
    """A recorded verdict on a fetch that has no evidence row yet: named by source and locator."""
    props: dict[str, Any] = {
        "role": role,
        "subject": subject,
        "verdict": verdict,
        "source_id": obs["source_id"],
        "locator": obs["locator"],
        "model_call_id": call(driver, w, model),
        "at": NOW,
    }
    if correlated:
        props["correlated"] = True
    return plant_node(driver, w, "Judgment", **props)["id"]


def verify(claim: str, pieces: list[dict], disposition: str = "confirmed", **fields) -> dict:
    op = {
        "op": "verify",
        "op_id": new_op_id(),
        "claim": claim,
        "pieces": pieces,
        "disposition": disposition,
        "reason": "the judge read the source again",
    }
    op.update(fields)
    return op


def piece(ev_id: str, judgment_id: str) -> dict[str, str]:
    return {"ev_id": ev_id, "judgment_id": judgment_id}


def refused(driver, w: str, ops: list[dict], evidence: list[dict], caller=LEARNER, **kw):
    """Apply a batch that must be declined; check that nothing but the decline log changed."""
    before = count_rows(driver, w)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, ops, evidence, caller=caller, **kw)
    assert_only_decline_log(driver, w, before, caught.value.refusals)
    return caught.value.refusals


def one(refusals, code: str, rule: str | None = None):
    found = [r for r in refusals if r.code == code and (rule is None or r.rule == rule)]
    assert found, f"no {code}/{rule} among {[(r.code, r.rule) for r in refusals]}"
    return found[0]


def item_row(driver, w: str, item_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


def link_row(driver, w: str, link_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=link_id)
    return row["l"]


def verifications(driver, w: str, claim_id: str) -> list[dict[str, Any]]:
    rows = read(
        driver,
        w,
        "MATCH (v:Verification {claim_id: $id}) RETURN v ORDER BY v.at, v.id",
        id=claim_id,
    )
    return [row["v"] for row in rows]


def evidence_on(driver, w: str, target_id: str) -> list[dict[str, Any]]:
    rows = read(driver, w, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=target_id)
    return [row["e"] for row in rows]


def utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC)


def organisation(driver, w: str, talk: dict, title: str = "Okonkwo Hall", **fields) -> str:
    """A lasting thing said in a chat at NOW: never confirmed, recorded at NOW."""
    ev = observation(talk, "said", 1, at=NOW - timedelta(minutes=5), statement_class="report_state")
    return created_id(
        apply(driver, w, [create_item("organisation", title, "said", **fields)], [ev])
    )


def task(driver, w: str, talk: dict, title: str, line: int, status: str = "open") -> str:
    ev = observation(talk, f"t{line}", line, at=NOW - timedelta(minutes=5))
    op = create_item("task", title, f"t{line}", status=status, recheck_by=RECHECK)
    return created_id(apply(driver, w, [op], [ev]))


def hand_weight(spoken_for: datetime, reference: datetime, now: datetime, limit_days: int) -> float:
    """Appendix C, by hand: 1.0 when newer than the reference; else 1.0 - 0.7 * age / limit within
    the limit, and 0 beyond it."""
    if spoken_for > reference:
        return 1.0
    age_days = (now - spoken_for).total_seconds() / 86400
    if age_days > limit_days:
        return 0.0
    return 1.0 - 0.7 * age_days / limit_days


def hand_band(agree: float, disagree: float) -> tuple[float | None, str]:
    if agree == 0 and disagree == 0:
        return None, "zero_signal"
    ratio = agree / (agree + disagree)
    if ratio >= 0.8:
        return ratio, "confirm" if agree >= 1.0 else "stale_support"
    if ratio >= 0.5:
        return ratio, "contradiction"
    return ratio, "against"


def assert_stored_band(
    row: dict[str, Any], *, reference: datetime, now: datetime, limit_days: int
) -> None:
    """The band the writer stored equals the one computed by hand from the same stored rows: the
    pieces' times and verdicts as the verification row lists them."""
    agree = disagree = 0.0
    for entry in json.loads(row["evidence_pieces"]):
        if entry["verdict"] not in ("supports", "contradicts"):
            continue
        by_hand = hand_weight(utc(entry["time_spoken_for"]), reference, now, limit_days)
        assert entry["weight"] == pytest.approx(by_hand, abs=1e-6)
        if entry["verdict"] == "contradicts":
            disagree += by_hand
        elif entry["admitted"]:
            agree += by_hand
    ratio, band = hand_band(round(agree, 9), round(disagree, 9))
    assert row["band"] == band
    assert row["agree"] == pytest.approx(agree, abs=1e-6)
    assert row["disagree"] == pytest.approx(disagree, abs=1e-6)
    if ratio is None:
        assert row.get("ratio") is None
    else:
        assert row["ratio"] == pytest.approx(ratio, abs=1e-6)


def confirm_with_two_pages(driver, w: str, frozen, pages: dict, claim: str, subject: str):
    """Two fresh supporting pieces from a fetched page, read at VERIFY_AT, confirmed."""
    frozen.set(VERIFY_AT)
    p1 = page_piece(pages, "p1", 1, FRESH)
    p2 = page_piece(pages, "p2", 2, FRESHER)
    ops = [
        verify(
            claim,
            [
                piece("p1", judge(driver, w, subject, p1)),
                piece("p2", judge(driver, w, subject, p2)),
            ],
            recheck_by=RECHECK,
        )
    ]
    return apply(driver, w, ops, [p1, p2], caller=LEARNER)


# A verify moves the truth clock, and only it ---------------------------------------------------


def test_a_verify_moves_last_confirmed_and_confirmed_against_and_nothing_of_the_claim(
    driver, w, frozen, talk, pages
):
    claim = organisation(
        driver, w, talk, summary="A hall the venue list names", became_true=clock_at(NOW, "day")
    )
    before = item_row(driver, w, claim)
    assert before.get("last_confirmed") is None and before.get("confirmed_against") is None

    confirm_with_two_pages(driver, w, frozen, pages, claim, f"item:{claim}")

    after = item_row(driver, w, claim)
    [row] = verifications(driver, w, claim)
    assert utc(after["last_confirmed"]) == FRESHER  # the newest admitted piece's time
    assert after["confirmed_against"] == row["id"]
    unchanged = (
        "title",
        "summary",
        "body",
        "kind",
        "properties",
        "recorded",
        "became_true_earliest",
        "became_true_latest",
        "became_true_precision",
        "stopped_being_true_precision",
    )
    assert {key: after.get(key) for key in unchanged} == {key: before.get(key) for key in unchanged}


def test_two_fresh_supporting_pieces_confirm(driver, w, frozen, talk, pages):
    # Hand computed: both pieces are newer than the claim's recorded time (NOW), so each weighs
    # 1.0; agree = 2.0, disagree = 0, ratio = 1.0 >= 0.8 and agree >= 1.0: band confirm.
    claim = organisation(driver, w, talk)
    confirm_with_two_pages(driver, w, frozen, pages, claim, f"item:{claim}")
    [row] = verifications(driver, w, claim)
    assert (row["agree"], row["disagree"], row["ratio"], row["band"]) == (2.0, 0.0, 1.0, "confirm")
    assert row["disposition"] == "confirmed"
    assert row["loadbearing"] is False
    pieces = json.loads(row["evidence_pieces"])
    assert [(p["weight"], p["admitted"], p["capability"]) for p in pieces] == [
        (1.0, True, "confirms"),
        (1.0, True, "confirms"),
    ]
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    # Each piece that supports became an evidence row on the claim.
    page_rows = [e for e in evidence_on(driver, w, claim) if e["source_id"] == pages["source_id"]]
    assert sorted(e["polarity"] for e in page_rows) == ["supports", "supports"]


def test_the_receipt_line_names_the_time_the_source_and_the_next_recheck(
    driver, w, frozen, talk, pages
):
    claim = organisation(driver, w, talk)
    receipt = confirm_with_two_pages(driver, w, frozen, pages, claim, f"item:{claim}")
    [line] = [line for line in receipt.lines if line.symbol == "v"]
    assert line.entity_id == claim
    text = render_receipt(driver, w, receipt)
    short = claim.replace("-", "")[-8:]
    assert f"v item {short}: confirmed 03-01 09:40 against a fetched page; re-check by 03-15" in (
        text
    )


def test_a_piece_cited_by_its_evidence_row_and_a_judgment_from_record_judgments_confirms(
    driver, w, frozen, talk, pages
):
    claim = organisation(driver, w, talk)
    frozen.set(FRESH)
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": claim,
        "ev_id": "a1",
        "polarity": "supports",
    }
    apply(driver, w, [attach], [page_piece(pages, "a1", 1, FRESH)], caller=LEARNER)
    [row] = [e for e in evidence_on(driver, w, claim) if e["source_id"] == pages["source_id"]]
    model_call = call(driver, w, "judge-a")
    [judgment] = record_judgments(
        driver,
        w,
        model_call,
        [JudgmentRow("first_judge", f"item:{claim}", "supports", NOW, evidence_id=row["id"])],
    )
    frozen.set(VERIFY_AT)
    ops = [verify(claim, [{"evidence_id": row["id"], "judgment_id": judgment}])]
    apply(driver, w, ops, [], caller=LEARNER)
    found = item_row(driver, w, claim)
    assert utc(found["last_confirmed"]) == FRESH
    [stored] = verifications(driver, w, claim)
    assert (stored["agree"], stored["band"]) == (1.0, "confirm")
    assert_stored_band(stored, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)


# Refusals ------------------------------------------------------------------------------------


def test_a_verify_without_a_resolving_locator_is_refused(driver, w, frozen, talk, pages):
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    bad = observation(pages, "p1", 1, at=FRESH, locator="char:0-99999")
    ops = [verify(claim, [piece("p1", judge(driver, w, f"item:{claim}", bad))])]
    one(refused(driver, w, ops, [bad]), "unresolvable_locator")
    assert item_row(driver, w, claim).get("last_confirmed") is None


def test_a_verify_from_an_unconfirming_source_type_is_refused(driver, w, frozen, talk):
    # A chat line reporting a state is reported_done: it never confirms (condition 1).
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    said = observation(talk, "c1", 3, at=FRESH, statement_class="report_state")
    ops = [verify(claim, [piece("c1", judge(driver, w, f"item:{claim}", said))])]
    one(refused(driver, w, ops, [said]), "not_confirmable", "source_type")


@pytest.mark.parametrize("verdict", ["contradicts", "not_about_claim"])
def test_a_confirmation_with_a_verdict_other_than_supports_is_refused(
    driver, w, frozen, talk, pages, verdict
):
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    p1 = page_piece(pages, "p1", 3, FRESH)
    ops = [verify(claim, [piece("p1", judge(driver, w, f"item:{claim}", p1, verdict))])]
    one(refused(driver, w, ops, [p1]), "judge_not_supports")


def test_a_verify_whose_only_evidence_is_inferred_is_refused(driver, w, frozen, talk, pages):
    claim = organisation(driver, w, talk)
    # A confirmed premise: a page row on another claim.
    other = organisation(driver, w, talk, title="Varga Gallery")
    frozen.set(FRESH)
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": other,
        "ev_id": "a1",
        "polarity": "supports",
    }
    apply(driver, w, [attach], [page_piece(pages, "a1", 4, FRESH)], caller=LEARNER)
    [premise] = [e for e in evidence_on(driver, w, other) if e["source_type"] == "confirmed"]
    frozen.set(VERIFY_AT)
    inferred = page_piece(pages, "i1", 1, FRESHER, derivation="inferred", premises=[premise["id"]])
    ops = [verify(claim, [piece("i1", judge(driver, w, f"item:{claim}", inferred))])]
    one(refused(driver, w, ops, [inferred]), "inferred_evidence", "inferred_piece")


def test_a_verify_for_a_claim_no_capability_covers_is_refused(driver, w, frozen, talk):
    # Git confirms records and work items, never a lasting thing (D5's default table).
    claim = organisation(driver, w, talk)
    git = make_source(driver, w, "git", GIT_LINES)
    frozen.set(VERIFY_AT)
    commit = observation(
        git,
        "g1",
        1,
        at=FRESH,
        locator=f"key:{GIT_TOKEN}",
        message_key=GIT_TOKEN,
        statement_class="report_happened",
    )
    ops = [verify(claim, [piece("g1", judge(driver, w, f"item:{claim}", commit))])]
    one(refused(driver, w, ops, [commit]), "not_confirmable", "no_capability")


def test_a_verdict_with_no_recorded_judgment_is_refused(driver, w, frozen, talk, pages):
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    p1 = page_piece(pages, "p1", 1, FRESH)
    ops = [verify(claim, [piece("p1", "00000000-0000-7000-8000-00000000dead")])]
    one(refused(driver, w, ops, [p1]), "unrecorded_verdict", "unrecorded_verdict")


def test_a_judgment_of_another_claim_is_an_unrecorded_verdict(driver, w, frozen, talk, pages):
    claim = organisation(driver, w, talk)
    other = organisation(driver, w, talk, title="Varga Gallery")
    frozen.set(VERIFY_AT)
    p1 = page_piece(pages, "p1", 1, FRESH)
    ops = [verify(claim, [piece("p1", judge(driver, w, f"item:{other}", p1))])]
    one(refused(driver, w, ops, [p1]), "unrecorded_verdict", "judgment_subject")


@pytest.mark.parametrize(
    ("where", "key", "value"),
    [
        ("piece", "weight", 1.0),
        ("op", "score", 0.9),
        ("op", "band", "confirm"),
        ("op", "load_bearing", False),
        ("piece", "capability", "confirms"),
    ],
)
def test_a_caller_weight_score_band_load_bearing_or_capability_is_refused(
    driver, w, frozen, talk, pages, where, key, value
):
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    p1 = page_piece(pages, "p1", 1, FRESH)
    sent = piece("p1", judge(driver, w, f"item:{claim}", p1))
    op = verify(claim, [sent])
    (sent if where == "piece" else op)[key] = value
    one(refused(driver, w, [op], [p1]), "caller_score")
    assert item_row(driver, w, claim).get("last_confirmed") is None


# Load-bearing claims and the second judge ----------------------------------------------------


@pytest.fixture
def blocking(driver, w, talk):
    """Task A blocks task B: A carries consequences, so it is load-bearing."""
    a = task(driver, w, talk, "Draft the Okonkwo Hall venue list", 2)
    b = task(driver, w, talk, "Print the Okonkwo Hall venue list", 3)
    ev = observation(talk, "k1", 4, at=NOW - timedelta(minutes=4))
    receipt = apply(driver, w, [create_link(a, "blocks", b, "k1", recheck_by=RECHECK)], [ev])
    return {"a": a, "b": b, "link": created_id(receipt, ">", "link")}


def load_bearing_verify(driver, w, frozen, pages, claim: str, *second: tuple[str, str, bool]):
    """One fresh supporting page piece on `claim`, with second verdicts (verdict, model,
    correlated) on the same piece."""
    frozen.set(VERIFY_AT)
    p1 = page_piece(pages, "p1", 1, FRESH)
    subject = f"item:{claim}"
    seconds = [
        judge(driver, w, subject, p1, verdict, role="second_judge", model=model, correlated=corr)
        for verdict, model, corr in second
    ]
    fields = {"recheck_by": LATER_RECHECK}
    if seconds:
        fields["second_judge"] = seconds
    op = verify(claim, [piece("p1", judge(driver, w, subject, p1, model="judge-a"))], **fields)
    return op, p1


def test_a_load_bearing_claim_without_a_second_verdict_is_refused(
    driver, w, frozen, pages, blocking
):
    op, p1 = load_bearing_verify(driver, w, frozen, pages, blocking["a"])
    one(refused(driver, w, [op], [p1]), "second_judge_required", "second_judge_required")


def test_a_second_verdict_that_disagrees_is_refused(driver, w, frozen, pages, blocking):
    op, p1 = load_bearing_verify(
        driver, w, frozen, pages, blocking["a"], ("contradicts", "judge-b", False)
    )
    one(refused(driver, w, [op], [p1]), "judges_disagree")


def test_a_second_verdict_of_the_same_model_not_recorded_as_correlated_is_refused(
    driver, w, frozen, pages, blocking
):
    op, p1 = load_bearing_verify(
        driver, w, frozen, pages, blocking["a"], ("supports", "judge-a", False)
    )
    one(refused(driver, w, [op], [p1]), "second_judge_required", "same_model")


def test_a_correlated_stand_in_of_the_same_model_is_accepted(driver, w, frozen, pages, blocking):
    op, p1 = load_bearing_verify(
        driver, w, frozen, pages, blocking["a"], ("supports", "judge-a", True)
    )
    apply(driver, w, [op], [p1], caller=LEARNER)
    [row] = verifications(driver, w, blocking["a"])
    assert row["loadbearing"] is True
    assert row["correlated"] is True
    assert row["second_judge_judgment_ids"] == op["second_judge"]
    assert (row["agree"], row["disagree"], row["ratio"], row["band"]) == (1.0, 0.0, 1.0, "confirm")
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    assert utc(item_row(driver, w, blocking["a"])["last_confirmed"]) == FRESH


def test_a_verify_of_a_work_item_leaves_the_clocks_of_linked_figures_untouched(
    driver, w, frozen, pages, blocking
):
    before_b = item_row(driver, w, blocking["b"])
    before_link = link_row(driver, w, blocking["link"])
    op, p1 = load_bearing_verify(
        driver, w, frozen, pages, blocking["a"], ("supports", "judge-b", False)
    )
    apply(driver, w, [op], [p1], caller=LEARNER)
    assert item_row(driver, w, blocking["a"]).get("last_confirmed") is not None
    # The linked task and the link itself are byte-identical: no clock, no touch, no version.
    assert item_row(driver, w, blocking["b"]) == before_b
    assert link_row(driver, w, blocking["link"]) == before_link
    assert verifications(driver, w, blocking["b"]) == []


# The score, one test per band ------------------------------------------------------------------


def test_a_supporting_and_a_contradicting_piece_of_equal_weight_open_a_contradiction(
    driver, w, frozen, talk, pages
):
    # Hand computed: both fresh (1.0 each); agree 1.0, disagree 1.0, ratio 0.5: contradiction.
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    subject = f"item:{claim}"
    p1 = page_piece(pages, "p1", 1, FRESH)
    p3 = page_piece(pages, "p3", 3, FRESHER)
    pieces = [
        piece("p1", judge(driver, w, subject, p1)),
        piece("p3", judge(driver, w, subject, p3, "contradicts")),
    ]
    one(
        refused(driver, w, [verify(claim, pieces, recheck_by=RECHECK)], [p1, p3]),
        "not_confirmable",
        "band_contradiction",
    )
    moved = refused(driver, w, [verify(claim, pieces, "moved")], [p1, p3])
    one(moved, "invalid_arguments", "override_reason_required")

    apply(driver, w, [verify(claim, pieces, "deferred", recheck_by=RECHECK)], [p1, p3])
    [row] = verifications(driver, w, claim)
    assert (row["agree"], row["disagree"], row["ratio"], row["band"]) == (
        1.0,
        1.0,
        0.5,
        "contradiction",
    )
    assert row["disposition"] == "deferred"
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    found = item_row(driver, w, claim)
    assert found.get("last_confirmed") is None
    assert utc(found["recheck_by"]) == utc(RECHECK)
    page_rows = [e for e in evidence_on(driver, w, claim) if e["source_id"] == pages["source_id"]]
    assert sorted(e["polarity"] for e in page_rows) == ["contradicts", "supports"]


def test_a_fresh_contradiction_from_a_source_that_proves_the_new_state_yields_moved(
    driver, w, frozen, talk, pages
):
    # The venue list was withdrawn: the page proves the new state. The batch changes the claim
    # first (the status moves to blocked on the page's row), then verify says moved. Hand
    # computed: one contradicting piece newer than the claim, weight 1.0; agree 0, disagree 1.0,
    # ratio 0.0: band against, which allows moved.
    claim = task(driver, w, talk, "Publish the Okonkwo Hall venue list", 2, status="in progress")
    frozen.set(VERIFY_AT)
    proof = page_piece(pages, "n1", 3, FRESH, statement_class="report_state")
    seen = page_piece(pages, "n2", 2, FRESHER, statement_class="report_state")
    change = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": claim,
        "status": "blocked",
        "evidence_refs": supports("n1"),
        "reason": "the page says the list was withdrawn",
    }
    pieces = [piece("n2", judge(driver, w, f"item:{claim}", seen, "contradicts"))]

    # Without the change in the batch, moved is refused.
    alone = refused(driver, w, [verify(claim, pieces, "moved")], [seen])
    one(alone, "invalid_arguments", "moved_needs_change")

    apply(driver, w, [change, verify(claim, pieces, "moved")], [proof, seen], caller=LEARNER)
    [row] = verifications(driver, w, claim)
    assert (row["agree"], row["disagree"], row["ratio"], row["band"]) == (0.0, 1.0, 0.0, "against")
    assert row["disposition"] == "moved"
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    found = item_row(driver, w, claim)
    assert found["status"] == "blocked"
    assert found.get("last_confirmed") is None


def test_no_evidence_at_all_defers_with_zero_signal(driver, w, frozen, talk, pages):
    # The only judgment says the page is not about the claim: nothing weighs, agree = disagree = 0.
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    p2 = page_piece(pages, "p2", 2, FRESH)
    pieces = [piece("p2", judge(driver, w, f"item:{claim}", p2, "not_about_claim"))]
    one(refused(driver, w, [verify(claim, pieces)], [p2]), "judge_not_supports")
    apply(driver, w, [verify(claim, pieces, "deferred", recheck_by=RECHECK)], [p2])
    [row] = verifications(driver, w, claim)
    assert (row["agree"], row["disagree"], row["band"]) == (0.0, 0.0, "zero_signal")
    assert row.get("ratio") is None
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    # A piece that is not about the claim leaves no evidence row on it.
    assert [e for e in evidence_on(driver, w, claim) if e["source_id"] == pages["source_id"]] == []
    found = item_row(driver, w, claim)
    assert found.get("last_confirmed") is None
    assert utc(found["recheck_by"]) == utc(RECHECK)


def test_one_piece_at_its_staleness_limit_weighs_0_3_and_defers_with_the_evidence_attached(
    driver, w, frozen, talk, pages
):
    # The page piece is older than the claim (recorded NOW) and exactly 7 days old at VERIFY_AT:
    # weight = 1.0 - 0.7 * 7 / 7 = 0.3; agree 0.3, ratio 1.0, agree < 1.0: stale_support.
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    old = page_piece(pages, "p1", 1, VERIFY_AT - timedelta(days=PAGE_LIMIT_DAYS))
    pieces = [piece("p1", judge(driver, w, f"item:{claim}", old))]
    one(refused(driver, w, [verify(claim, pieces)], [old]), "not_confirmable", "band_stale_support")
    apply(driver, w, [verify(claim, pieces, "deferred", recheck_by=RECHECK)], [old])
    [row] = verifications(driver, w, claim)
    assert row["band"] == "stale_support"
    assert row["agree"] == pytest.approx(0.3)
    assert row["disagree"] == 0.0
    assert row["ratio"] == pytest.approx(1.0)
    [entry] = json.loads(row["evidence_pieces"])
    assert entry["weight"] == pytest.approx(0.3)
    assert entry["admitted"] is True
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    # Deferred with the evidence attached: the row is on the claim, the clock has not moved.
    [attached] = [e for e in evidence_on(driver, w, claim) if e["source_id"] == pages["source_id"]]
    assert attached["polarity"] == "supports"
    assert entry["evidence_id"] == attached["id"]
    assert item_row(driver, w, claim).get("last_confirmed") is None


def test_a_piece_past_its_staleness_limit_weighs_0(driver, w, frozen, talk, pages):
    # Eight days old against a 7-day limit: weight 0, so agree 0 and the band is zero_signal.
    claim = organisation(driver, w, talk)
    frozen.set(VERIFY_AT)
    old = page_piece(pages, "p1", 1, VERIFY_AT - timedelta(days=PAGE_LIMIT_DAYS + 1))
    pieces = [piece("p1", judge(driver, w, f"item:{claim}", old))]
    one(refused(driver, w, [verify(claim, pieces)], [old]), "not_confirmable", "band_zero_signal")
    apply(driver, w, [verify(claim, pieces, "deferred", recheck_by=RECHECK)], [old])
    [row] = verifications(driver, w, claim)
    [entry] = json.loads(row["evidence_pieces"])
    assert entry["weight"] == 0.0
    assert (row["agree"], row["band"]) == (0.0, "zero_signal")
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)


# Lane tests the writer answers -----------------------------------------------------------------


def test_a_claim_whose_only_reachable_source_is_an_identity_record_is_deferred(driver, w, frozen):
    # Where someone lives is a state link; an identity source confirms identity fields only.
    talk = make_source(driver, w, "whatsapp", CHAT_LINES)
    said = [observation(talk, f"s{n}", n, at=NOW - timedelta(minutes=5)) for n in (1, 2)]
    receipt = apply(
        driver,
        w,
        [
            create_item("person", "Ilse Varga", "s1", temp_id="$ilse"),
            create_item("place", "Velmora", "s2", temp_id="$velmora"),
            create_link("$ilse", "lives in", "$velmora", "s1", recheck_by=RECHECK),
        ],
        said,
    )
    link = created_id(receipt, ">", "link")
    identity = make_source(driver, w, "identity", IDENTITY_LINES)
    frozen.set(VERIFY_AT)
    profile = observation(identity, "id1", 1, at=FRESH, locator="key:Velmora")
    pieces = [piece("id1", judge(driver, w, f"link:{link}", profile))]
    one(
        refused(driver, w, [verify(link, pieces, recheck_by=LATER_RECHECK)], [profile]),
        "not_confirmable",
        "no_capability",
    )
    apply(driver, w, [verify(link, pieces, "deferred", recheck_by=LATER_RECHECK)], [profile])
    [row] = verifications(driver, w, link)
    assert row["disposition"] == "deferred"
    assert row["band"] == "zero_signal"
    [entry] = json.loads(row["evidence_pieces"])
    assert (entry["capability"], entry["admitted"]) == ("none", False)
    assert_stored_band(row, reference=NOW, now=VERIFY_AT, limit_days=PAGE_LIMIT_DAYS)
    found = link_row(driver, w, link)
    assert found.get("last_confirmed") is None
    assert utc(found["recheck_by"]) == utc(LATER_RECHECK)


def test_the_verifications_table_agrees_with_the_claims_clocks(driver, w, frozen, talk, pages):
    first = organisation(driver, w, talk)
    second = organisation(driver, w, talk, title="Varga Gallery")
    confirm_with_two_pages(driver, w, frozen, pages, first, f"item:{first}")
    # The second claim is confirmed, then a later deferral leaves its last confirmed alone.
    frozen.set(VERIFY_AT + timedelta(hours=1))
    p4 = page_piece(pages, "p4", 4, VERIFY_AT + timedelta(minutes=10))
    subject = f"item:{second}"
    apply(driver, w, [verify(second, [piece("p4", judge(driver, w, subject, p4))])], [p4])
    frozen.set(VERIFY_AT + timedelta(days=1))
    p2 = page_piece(pages, "p2b", 2, VERIFY_AT + timedelta(hours=20))
    later = [piece("p2b", judge(driver, w, subject, p2, "not_about_claim"))]
    apply(driver, w, [verify(second, later, "deferred", recheck_by=LATER_RECHECK)], [p2])

    for claim in (first, second):
        row = item_row(driver, w, claim)
        rows = verifications(driver, w, claim)
        named = next(v for v in rows if v["id"] == row["confirmed_against"])
        assert named["claim_id"] == claim
        assert named["disposition"] == "confirmed"
        admitted = [p for p in json.loads(named["evidence_pieces"]) if p["admitted"]]
        newest = max(utc(p["time_spoken_for"]) for p in admitted)
        assert utc(row["last_confirmed"]) == newest
        # The newest confirmed row is the one the claim names.
        confirmed = [v for v in rows if v["disposition"] == "confirmed"]
        assert max(confirmed, key=lambda v: v["at"])["id"] == row["confirmed_against"]
        # Each stored band equals the hand computation: every weighed piece is newer than the
        # claim's recorded time (NOW), and neither claim was confirmed before its first verify.
        for stored in rows:
            assert_stored_band(
                stored, reference=NOW, now=utc(stored["at"]), limit_days=PAGE_LIMIT_DAYS
            )
    assert [v["disposition"] for v in verifications(driver, w, second)] == [
        "confirmed",
        "deferred",
    ]
    assert utc(item_row(driver, w, second)["last_confirmed"]) == VERIFY_AT + timedelta(minutes=10)
