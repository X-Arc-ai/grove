"""The clocks in words (D4 "What reads say", spec 975, 1003-1009; D5 rule 1, spec 1182).

Written from the spec's sentences, not from the implementation: "A claim past its horizon is
returned, never hidden, and flagged with both dates: 'last confirmed 3 Mar against the lease;
re-check was due 3 Jun'. 'Never confirmed' is reported as such, with the evidence's own date."
"In July" keeps July's bounds, and an inferred claim reads "inferred from 3 confirmed records",
never "confirmed". The phrases are pinned on rows alone first; then the reader is run against
claims the real writer stored. All names are invented, and every time comes from a TestClock.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.store.query import WorldQuery
from grove.store.words import (
    Step,
    clock_value_words,
    clock_words,
    day_words,
    how_words,
    last_confirmed_words,
    moment_words,
    provenance_timezone,
    recheck_words,
    recorded_words,
    source_type_words,
    source_words,
    weekday_words,
    zone_of,
)
from grove.writer.testing import plant_node
from tests.writer.helpers import (
    LEARNER,
    apply,
    clock_at,
    create_item,
    create_link,
    created_id,
    make_source,
    new_op_id,
    observation,
    read,
    seeded,  # noqa: F401
)

# The phrases, on rows alone ---------------------------------------------------------------------

NOW = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)  # a Tuesday
TODAY_0915 = datetime(2026, 10, 27, 9, 15, tzinfo=UTC)
RECORDED = datetime(2026, 10, 26, 22, 40, tzinfo=UTC)
THURSDAY = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
MARCH_3 = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)
JUNE_3 = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
UTC_ZONE = zone_of("UTC")
DUBAI = zone_of("Asia/Dubai")


def test_a_fresh_confirmation_reads_today_and_its_source():
    # Spec 1004: "last confirmed   today 09:15, against the hackathon rules page (fetched)".
    text = last_confirmed_words(
        TODAY_0915,
        against=source_words("web_pages"),
        provenance_source=source_words("whatsapp"),
        provenance_at=RECORDED,
        recheck_by=THURSDAY,
        zone=UTC_ZONE,
        now=NOW,
    )
    assert text == "today 09:15, against a fetched page"


def test_yesterday_reads_yesterday_and_an_older_day_reads_its_date():
    assert moment_words(RECORDED, zone=UTC_ZONE, now=NOW) == "yesterday 22:40"
    assert moment_words(RECORDED - timedelta(days=1), zone=UTC_ZONE, now=NOW) == "25 Oct, 22:40"
    assert day_words(MARCH_3, zone=UTC_ZONE, now=NOW) == "3 Mar"
    # The year shows only when it is not the year of now.
    assert day_words(MARCH_3, zone=UTC_ZONE, now=NOW + timedelta(days=90)) == "3 Mar 2026"


def test_a_lapsed_horizon_is_flagged_with_both_dates():
    # Spec 975: "last confirmed 3 Mar against the lease; re-check was due 3 Jun".
    text = last_confirmed_words(
        MARCH_3,
        against=source_words("notes_files_releases"),
        provenance_source=source_words("whatsapp"),
        provenance_at=MARCH_3 - timedelta(days=2),
        recheck_by=JUNE_3,
        zone=UTC_ZONE,
        now=NOW,
    )
    assert text == "3 Mar, against a file; re-check was due 3 Jun"
    assert recheck_words(JUNE_3, how=None, zone=UTC_ZONE, now=NOW) == "was due 3 Jun"


def test_a_horizon_ahead_is_not_flagged():
    text = last_confirmed_words(
        MARCH_3,
        against=source_words("notes_files_releases"),
        provenance_source=None,
        provenance_at=None,
        recheck_by=THURSDAY,
        zone=UTC_ZONE,
        now=NOW,
    )
    assert text == "3 Mar, against a file"


def test_never_confirmed_reads_as_such_with_the_rows_own_date():
    # Spec 975: "'Never confirmed' is reported as such, with the evidence's own date."
    text = last_confirmed_words(
        None,
        against=None,
        provenance_source=source_words("whatsapp"),
        provenance_at=RECORDED,
        recheck_by=None,
        zone=UTC_ZONE,
        now=NOW,
    )
    assert text == "never confirmed, from a chat message of 26 Oct"


def test_a_backfilled_claim_nine_months_old_arrives_already_due():
    # Spec 1022: "reads 'never confirmed, from a message of <date>'", already due.
    said = datetime(2026, 1, 20, 18, 0, tzinfo=UTC)
    due = datetime(2026, 1, 27, 18, 0, tzinfo=UTC)
    now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    text = last_confirmed_words(
        None,
        against=None,
        provenance_source=source_words("whatsapp"),
        provenance_at=said,
        recheck_by=due,
        zone=UTC_ZONE,
        now=now,
    )
    assert text == "never confirmed, from a chat message of 20 Jan; re-check was due 27 Jan"


def test_the_recheck_reads_its_weekday_and_how_when_it_names_a_connector():
    # Spec 1005: "re-check by      Thu Oct 29, by fetching the rules page again".
    assert weekday_words(THURSDAY, zone=UTC_ZONE, now=NOW) == "Thu 29 Oct"
    how = how_words({"connector": "web_pages", "locator_or_query": "rules", "compare": "date"})
    assert how == "by checking a fetched page again"
    assert recheck_words(THURSDAY, how=how, zone=UTC_ZONE, now=NOW) == (
        "Thu 29 Oct, by checking a fetched page again"
    )
    assert recheck_words(THURSDAY, how=None, zone=UTC_ZONE, now=NOW) == "Thu 29 Oct"
    assert recheck_words(None, how=None, zone=UTC_ZONE, now=NOW) == "not set"
    assert how_words("ask_the_person") == "by asking you"
    assert how_words({"connector": "a_connector_nobody_has"}) is None
    assert how_words(None) is None


def test_recorded_reads_the_commit_time_and_the_creating_source():
    # Spec 1007: "recorded         Oct 26, 22:40, from a message in the build chat".
    earlier = RECORDED - timedelta(days=3)
    text = recorded_words(earlier, source=source_words("whatsapp"), zone=UTC_ZONE, now=NOW)
    assert text == "23 Oct, 22:40, from a chat message"
    assert recorded_words(None, source=None, zone=UTC_ZONE, now=NOW) == "unknown"


def test_a_source_is_named_by_its_connector_type_only():
    assert source_words("screenshots") == "a screenshot"
    assert source_words("calendar") == "the calendar"
    assert source_words(None) == "a source"


@pytest.mark.parametrize(
    ("earliest", "latest", "precision", "words"),
    [
        (None, None, "unknown", "unknown"),
        (None, None, None, "unknown"),
        # "In July" is stored with July's bounds, and reads as the month.
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC),
            "month",
            "July 2026",
        ),
        (None, RECORDED, "before", "before 26 Oct"),
        (MARCH_3, None, "after", "after 3 Mar"),
        (
            datetime(2026, 7, 12, tzinfo=UTC),
            datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC),
            "day",
            "12 Jul",
        ),
        (RECORDED, RECORDED, "minute", "26 Oct, 22:40"),
        (
            datetime(2026, 4, 1, tzinfo=UTC),
            datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
            "quarter",
            "Apr to Jun 2026",
        ),
        (
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
            "year",
            "2025",
        ),
    ],
)
def test_became_true_keeps_its_precision(earliest, latest, precision, words):
    assert clock_value_words(earliest, latest, precision, zone=UTC_ZONE, now=NOW) == words


def test_a_month_stored_in_one_zone_reads_as_that_month_in_a_neighbouring_one():
    # July in Dubai starts at 30 Jun 20:00 UTC: read in UTC it is still July.
    earliest = datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
    latest = datetime(2026, 7, 31, 19, 59, 59, tzinfo=UTC)
    assert clock_value_words(earliest, latest, "month", zone=UTC_ZONE, now=NOW) == "July 2026"
    assert clock_value_words(earliest, latest, "month", zone=DUBAI, now=NOW) == "July 2026"


def test_dates_read_in_the_given_timezone():
    # 22:40 UTC on 26 Oct is 02:40 on 27 Oct in Dubai: today there.
    assert moment_words(RECORDED, zone=DUBAI, now=NOW) == "today 02:40"
    assert day_words(datetime(2026, 10, 28, 21, 0, tzinfo=UTC), zone=DUBAI, now=NOW) == "29 Oct"


def test_a_bad_timezone_is_refused_and_utc_is_utc():
    assert zone_of(None) is UTC and zone_of("UTC") is UTC
    with pytest.raises(ValueError):
        zone_of("Velmora/Nowhere")
    with pytest.raises(ValueError):
        moment_words(datetime(2026, 10, 26, 22, 40), zone=UTC_ZONE, now=NOW)


def test_the_source_type_reads_its_ladder_in_time_order():
    # Spec 1008: "source type      confirmed (said in the chat; confirmed by the page)".
    ladder = [Step("said", "a chat message"), Step("confirmed", "a fetched page")]
    assert source_type_words("confirmed", "observed", ladder) == (
        "confirmed (said in a chat message; confirmed by a fetched page)"
    )
    # A repeated step reads once.
    twice = [Step("said", "a chat message"), Step("said", "a chat message")]
    assert source_type_words("said", "observed", twice) == "said (said in a chat message)"
    steps = [Step("planned", "the calendar"), Step("reported_done", "a chat message")]
    assert source_type_words("reported_done", "observed", steps) == (
        "reported done (planned in the calendar; reported in a chat message)"
    )
    assert source_type_words(None, None, []) == "unknown"


def test_an_inferred_claim_reads_inferred_from_and_never_confirmed():
    # D5 rule 1 (spec 1071): reads say "inferred from 3 confirmed records", never "confirmed".
    text = source_type_words("confirmed", "inferred", [], ["confirmed"] * 3)
    assert text == "inferred from 3 confirmed records"
    assert "confirmed (" not in text
    mixed = source_type_words("said", "inferred", [], ["confirmed", "said", "confirmed"])
    assert mixed == "inferred from 2 confirmed records and 1 said record"
    assert source_type_words("planned", "rolled_up", [], ["confirmed"]) == (
        "rolled up from 1 confirmed record"
    )


# The reader, on claims the writer stored -------------------------------------------------------

T = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)  # a Monday
VERIFY_AT = T + timedelta(minutes=40)
RECHECK = "2027-03-15T09:00:00+00:00"  # a Monday
CHAT_LINES = [f"line {n}: Rowan says the Okonkwo Hall venue list is in hand" for n in range(20)]
PAGE_LINES = [
    "Okonkwo Hall: the venue list for Rowan Okonkwo is published and final.",
    "Okonkwo Hall: bookings for the spring season are open to members.",
    "Okonkwo Hall: the floor plan for the Ilse Varga dinner is attached.",
    "Okonkwo Hall: the cloakroom opens at six.",
]
FIELDS = {"last confirmed", "re-check by", "became true", "recorded", "source type", "state"}


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(T)
    with clock.using(test_clock):
        yield test_clock


@pytest.fixture
def w(frozen, seeded):  # noqa: F811
    return seeded["world_id"]


@pytest.fixture
def talk(driver, w):
    return make_source(driver, w, "whatsapp", CHAT_LINES)


@pytest.fixture
def pages(driver, w):
    return make_source(driver, w, "web_pages", PAGE_LINES)


def page_piece(src: dict, ev_id: str, line: int, at: datetime) -> dict[str, Any]:
    """One line of a fetched page, by its characters (web pages have no lines)."""
    start = sum(len(text) + 1 for text in PAGE_LINES[: line - 1])
    locator = f"char:{start}-{start + len(PAGE_LINES[line - 1])}"
    return observation(src, ev_id, line, at=at, locator=locator)


def judged(driver, w: str, subject: str, obs: dict[str, Any]) -> str:
    """A recorded supporting verdict of a recorded model call, on a fetch with no row yet."""
    call_id = plant_node(driver, w, "ModelCall", model="judge-a", job="verify", outcome="ok", at=T)
    return plant_node(
        driver,
        w,
        "Judgment",
        role="first_judge",
        subject=subject,
        verdict="supports",
        source_id=obs["source_id"],
        locator=obs["locator"],
        model_call_id=call_id["id"],
        at=T,
    )["id"]


def said_hall(driver, w: str, talk: dict, **extra: Any) -> str:
    """A lasting thing said in a chat five minutes before T, recorded at T. An opinion is not a
    report, so the row is said (D5)."""
    extra.setdefault("statement_class", "preference_or_opinion")
    ev = observation(talk, "said", 1, at=T - timedelta(minutes=5), **extra)
    return created_id(apply(driver, w, [create_item("organisation", "Okonkwo Hall", "said")], [ev]))


def words_of(driver, w: str, claim_id: str, timezone: str = "UTC") -> dict[str, str]:
    found = clock_words(WorldQuery(driver, w), claim_id, timezone=timezone)
    assert found is not None and set(found) == FIELDS
    return found


def test_a_claim_said_in_a_chat_reads_never_confirmed_with_the_rows_date(driver, w, talk):
    hall = said_hall(driver, w, talk)
    assert words_of(driver, w, hall) == {
        "last confirmed": "never confirmed, from a chat message of 1 Mar",
        "re-check by": "not set",
        "became true": "unknown",
        "recorded": "today 09:00, from a chat message",
        "source type": "said (said in a chat message)",
        "state": "current",
    }


def test_a_verify_reads_today_against_the_page_and_then_the_lapsed_horizon(
    driver, w, frozen, talk, pages
):
    hall = said_hall(driver, w, talk)
    frozen.set(VERIFY_AT)
    # The lane fetches the page now and stores the fetch first; the piece speaks for no later.
    pages = make_source(driver, w, "web_pages", PAGE_LINES)
    fetched = page_piece(pages, "p1", 1, T + timedelta(minutes=30))
    verify = {
        "op": "verify",
        "op_id": new_op_id(),
        "claim": hall,
        "pieces": [{"ev_id": "p1", "judgment_id": judged(driver, w, f"item:{hall}", fetched)}],
        "disposition": "confirmed",
        "reason": "the judge read the page again",
        "recheck_by": RECHECK,
        "recheck_how": {
            "connector": "web_pages",
            "locator_or_query": "the venue list page",
            "compare": "the hall is listed",
        },
    }
    apply(driver, w, [verify], [fetched], caller=LEARNER)

    now = words_of(driver, w, hall)
    assert now["last confirmed"] == "today 09:30, against a fetched page"
    assert now["re-check by"] == "Mon 15 Mar, by checking a fetched page again"
    assert now["source type"] == "confirmed (said in a chat message; confirmed by a fetched page)"
    # Recorded and became true are not the truth clock: a verify leaves them as they were.
    assert now["recorded"] == "today 09:00, from a chat message"
    assert now["became true"] == "unknown"

    frozen.set(T + timedelta(days=1))
    assert words_of(driver, w, hall)["last confirmed"] == "yesterday 09:30, against a fetched page"

    # Past its horizon the claim is returned, never hidden, and flagged with both dates.
    frozen.set(datetime(2027, 3, 20, 9, 0, tzinfo=UTC))
    later = words_of(driver, w, hall)
    assert later["last confirmed"] == "1 Mar, against a fetched page; re-check was due 15 Mar"
    assert later["re-check by"] == "was due 15 Mar"


def test_dates_read_in_the_persons_timezone_from_the_creating_row(driver, w, frozen, talk):
    # 23:30 UTC on 1 Mar is 03:30 on 2 Mar in Dubai.
    frozen.set(datetime(2027, 3, 1, 23, 30, tzinfo=UTC))
    ev = observation(
        talk, "late", 2, at=datetime(2027, 3, 1, 23, 20, tzinfo=UTC), person_timezone="Asia/Dubai"
    )
    op = create_item("organisation", "Brightwater Rowing Club", "late")
    hall = created_id(apply(driver, w, [op], [ev]))
    query = WorldQuery(driver, w)
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=hall)
    assert provenance_timezone(query, row["i"]) == "Asia/Dubai"
    local = words_of(driver, w, hall, provenance_timezone(query, row["i"]))
    assert local["last confirmed"] == "never confirmed, from a chat message of 2 Mar"
    assert local["recorded"] == "today 03:30, from a chat message"
    assert words_of(driver, w, hall)["recorded"] == "today 23:30, from a chat message"
    # No timezone on the creating row reads UTC.
    plain = said_hall(driver, w, talk)
    [other] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=plain)
    assert provenance_timezone(query, other["i"]) == "UTC"


def test_became_true_from_the_store_keeps_its_precision(driver, w, talk):
    july = {
        "earliest": "2027-07-01T00:00:00+00:00",
        "latest": "2027-07-31T23:59:59+00:00",
        "precision": "month",
        "words": "in July",
    }
    before = {"latest": "2027-02-26T12:00:00+00:00", "precision": "before"}
    ops = [
        create_item("decision", "Rowan picked the July date", "d1", became_true=july),
        create_item("decision", "Rowan chose the cheaper hall", "d1", became_true=before),
        create_item("event", "Rowan paid the deposit", "d1", became_true=clock_at(T, "day")),
    ]
    ev = observation(talk, "d1", 3, at=T - timedelta(minutes=5), statement_class="decision")
    receipt = apply(driver, w, ops, [ev])
    month, earlier, day = (created_id(receipt, nth=n) for n in range(3))
    assert words_of(driver, w, month)["became true"] == "July 2027"
    assert words_of(driver, w, earlier)["became true"] == "before 26 Feb"
    assert words_of(driver, w, day)["became true"] == "1 Mar"


def test_a_claim_inferred_from_confirmed_records_reads_inferred_from(driver, w, talk, pages):
    titles = ("Rowan published the venue list", "Spring bookings opened", "Ilse got the floor plan")
    ops = [create_item("event", title, f"p{n}") for n, title in enumerate(titles, 1)]
    evidence = [page_piece(pages, f"p{n}", n, T - timedelta(minutes=10)) for n in (1, 2, 3)]
    receipt = apply(driver, w, ops, evidence)
    records = [created_id(receipt, nth=n) for n in range(3)]
    rows = read(
        driver,
        w,
        "MATCH (e:Evidence) WHERE e.target_id IN $ids RETURN e.id AS id, e.source_type AS t",
        ids=records,
    )
    assert sorted(row["t"] for row in rows) == ["confirmed"] * 3
    inferred = observation(
        talk, "i1", 4, at=T, derivation="inferred", premises=[row["id"] for row in rows]
    )
    claim = created_id(
        apply(driver, w, [create_item("organisation", "Okonkwo Hall is open", "i1")], [inferred])
    )
    found = words_of(driver, w, claim)
    assert found["source type"] == "inferred from 3 confirmed records"
    assert "confirmed (" not in found["source type"]


def test_a_link_reads_its_clocks_and_an_unknown_id_reads_none(driver, w, talk):
    ops = [
        create_item("person", "Rowan Okonkwo", "e1", temp_id="$rowan"),
        create_item("organisation", "Okonkwo Hall", "e1", temp_id="$hall"),
        create_link("$rowan", "related to", "$hall", "e1", recheck_by=RECHECK),
    ]
    receipt = apply(driver, w, ops, [observation(talk, "e1", 5, at=T - timedelta(minutes=5))])
    link = created_id(receipt, ">", "link")
    found = words_of(driver, w, link)
    assert found["state"] == "current"
    assert found["last confirmed"] == "never confirmed, from a chat message of 1 Mar"
    assert found["re-check by"] == "Mon 15 Mar"
    assert clock_words(WorldQuery(driver, w), "no-such-claim") is None
