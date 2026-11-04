"""D5 controls through provider readers, screened sources, and the actual writer.

No Source or Episode is planted. The Google service is a local fake and every Git
object comes from the isolated synthetic repository fixture.
"""

# Pytest collects these imported fixtures and injects them into test functions.
# ruff: noqa: F811
import json
from datetime import UTC, datetime, timedelta

import pytest

from grove.connectors.service import sync_calendar, sync_git
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined, provenance
from grove.writer.connectors import ensure_connection, update_connection
from tests.connectors.test_calendar import event
from tests.connectors.test_connections import connect, deps  # noqa: F401
from tests.connectors.test_git import repository  # noqa: F401
from tests.ingest.conftest import make_screener
from tests.writer.helpers import LEARNER, apply, create_item, created_id, observation
from tests.writer.test_verify import judge, verify

NOW = datetime(2027, 3, 1, 9, tzinfo=UTC)
PAST = datetime(2027, 2, 25, 9, tzinfo=UTC)
FUTURE = datetime(2027, 3, 10, 9, tzinfo=UTC)
BOUNDARY = datetime(2027, 2, 19, 21, tzinfo=UTC)


@pytest.fixture
def frozen(test_clock):
    test_clock.set(NOW)
    return test_clock


def stored_source(driver, world, connector, *, commit_sha=None):
    rows = WorldQuery(driver, world).read(
        "MATCH (s:Source {connector_id: $id}), (e:Episode {connector_id: $id}) "
        "WHERE s.id IN e.source_ids RETURN s, e",
        id=connector,
    )
    if commit_sha is not None:
        rows = [
            row
            for row in rows
            if json.loads(json.loads(row["s"]["content"])[0]["text"])["commit"] == commit_sha
        ]
    assert len(rows) == 1
    source, episode = rows[0]["s"], rows[0]["e"]
    [message] = json.loads(source["content"])
    assert episode["first_message_key"] == episode["last_message_key"] == message["key"]
    return {
        "source_id": source["id"],
        "episode_id": episode["id"],
        "connector_id": connector,
        "key": message["key"],
        "source": source,
        "message": message,
    }


@pytest.fixture
def calendar_source(driver, seeded_world, client_for, fake_google, deps, frozen):
    def ingest(at=PAST, *, zone="UTC", status="confirmed", settings=None):
        chosen, _ = connect(
            client_for(seeded_world), fake_google, driver, seeded_world, **(settings or {})
        )
        value = event(
            summary="Orion session finished; the words alone must not confirm occurrence",
            status=status,
            start={"dateTime": at.isoformat(), "timeZone": zone} if at else None,
            end={"dateTime": (at + timedelta(hours=1)).isoformat(), "timeZone": zone}
            if at
            else None,
            updated=PAST.isoformat(),
        )
        if at is None:
            value.pop("start")
            value.pop("end")
            value.pop("etag")  # A real deleted-event response need not supply an etag.
        fake_google.answer = lambda *_: (
            200,
            {"items": [value], "nextSyncToken": "d5-sync", "timeZone": zone},
        )
        result = sync_calendar(
            driver,
            seeded_world,
            chosen,
            http=deps.http,
            client=deps.client,
            vault=deps.vault,
            screener=make_screener(),
            base_url=fake_google.base,
        )
        assert result["sources_added"] == result["episodes_added"] == 1
        return stored_source(driver, seeded_world, chosen)

    return ingest


def observe(src, at=PAST, zone="UTC", *, key=None, locator=None, statement="report_happened"):
    key = src["key"] if key is None else key
    extra = {} if zone is None else {"person_timezone": zone}
    return observation(
        src,
        "c1",
        1,
        at=at,
        message_key=key,
        locator=locator or "key:" + key,
        statement_class=statement,
        speaker_kind="third_party",
        **extra,
    )


def save_event(driver, world, row):
    item = created_id(
        apply(driver, world, [create_item("event", "Orion session", "c1")], [row], caller=LEARNER)
    )
    [stored] = WorldQuery(driver, world).read(
        "MATCH (e:Evidence {target_id: $id}) RETURN e", id=item
    )
    return item, stored["e"]


def finish_task(driver, world, row):
    return apply(
        driver,
        world,
        [create_item("task", "Finish the Orion session", "c1", status="done")],
        [row],
        caller=LEARNER,
    )


@pytest.mark.parametrize(
    "scheduled,supplied,status,trusted_zone,supplied_zone,settings,expected",
    [
        pytest.param(
            FUTURE,
            FUTURE,
            "confirmed",
            "UTC",
            "UTC",
            {"log_from": "2027-02-01"},
            "planned",
            id="future-honest",
        ),
        pytest.param(
            FUTURE,
            PAST,
            "confirmed",
            "UTC",
            "UTC",
            {"log_from": "2027-02-01"},
            "planned",
            id="future-old-time",
        ),
        pytest.param(PAST, PAST, "confirmed", "UTC", "UTC", {}, "planned", id="past-no-log"),
        pytest.param(
            PAST,
            PAST,
            "confirmed",
            "UTC",
            "UTC",
            {"log_from": "2027-02-25"},
            "confirmed",
            id="log-from-inclusive",
        ),
        pytest.param(
            PAST,
            PAST,
            "confirmed",
            "UTC",
            "UTC",
            {"log_from": "2027-02-26"},
            "planned",
            id="before-log-from",
        ),
        pytest.param(
            PAST,
            PAST,
            "confirmed",
            "UTC",
            "UTC",
            {"logged_days": ["2027-02-25"]},
            "confirmed",
            id="logged-day",
        ),
        pytest.param(
            PAST,
            PAST,
            "confirmed",
            "UTC",
            "UTC",
            {"logged_days": ["2027-02-24"]},
            "planned",
            id="other-day",
        ),
        pytest.param(
            PAST,
            PAST - timedelta(days=2),
            "confirmed",
            "UTC",
            "UTC",
            {"log_from": "2027-02-01"},
            "planned",
            id="past-substituted-time",
        ),
        pytest.param(
            BOUNDARY,
            BOUNDARY,
            "confirmed",
            "UTC",
            "Asia/Dubai",
            {"log_from": "2027-02-20"},
            "planned",
            id="zone-substitution",
        ),
        pytest.param(
            BOUNDARY,
            BOUNDARY,
            "confirmed",
            "Asia/Dubai",
            "Asia/Dubai",
            {"log_from": "2027-02-20"},
            "confirmed",
            id="trusted-log-zone",
        ),
        pytest.param(
            BOUNDARY,
            BOUNDARY,
            "confirmed",
            "Asia/Dubai",
            None,
            {"log_from": "2027-02-20"},
            "confirmed",
            id="omitted-observation-zone",
        ),
        pytest.param(
            PAST,
            PAST,
            "cancelled",
            "UTC",
            "UTC",
            {"log_from": "2027-02-01"},
            "planned",
            id="cancelled-with-start",
        ),
        pytest.param(
            None,
            PAST,
            "cancelled",
            "UTC",
            "UTC",
            {"log_from": "2027-02-01"},
            "planned",
            id="cancelled-without-start",
        ),
        pytest.param(
            PAST,
            PAST,
            "tentative",
            "UTC",
            "UTC",
            {"log_from": "2027-02-01"},
            "planned",
            id="tentative",
        ),
    ],
)
def test_actual_calendar_read_controls_evidence_and_completion(
    driver,
    seeded_world,
    calendar_source,
    scheduled,
    supplied,
    status,
    trusted_zone,
    supplied_zone,
    settings,
    expected,
):
    src = calendar_source(scheduled, zone=trusted_zone, status=status, settings=settings)
    row = observe(src, supplied, supplied_zone)
    _, evidence = save_event(driver, seeded_world, row)
    assert evidence["source_type"] == expected
    if expected == "confirmed":
        assert finish_task(driver, seeded_world, row).state == "saved"
    else:
        with pytest.raises(BatchDeclined) as caught:
            finish_task(driver, seeded_world, row)
        assert {refusal.rule for refusal in caught.value.refusals} == {
            "terminal_needs_confirmation"
        }


def test_calendar_source_binds_exact_key_and_separates_schedule_from_update(calendar_source):
    src = calendar_source(FUTURE, settings={"log_from": "2027-02-01"})
    facts = json.loads(src["source"].get("calendar_events") or "{}")
    assert set(facts) == {src["key"]}
    assert facts[src["key"]] == {
        "scheduled_at": FUTURE.isoformat(),
        "updated_at": PAST.isoformat(),
        "timezone": "UTC",
        "status": "confirmed",
    }
    assert datetime.fromisoformat(src["message"]["at"]) == FUTURE


def test_actual_minimal_cancellation_keeps_null_schedule_binding(calendar_source):
    src = calendar_source(None, status="cancelled", settings={"log_from": "2027-02-01"})
    facts = json.loads(src["source"].get("calendar_events") or "{}")
    assert set(facts) == {src["key"]}
    assert facts[src["key"]]["scheduled_at"] is None
    assert facts[src["key"]]["status"] == "cancelled"
    assert src["message"]["at"] is None


@pytest.mark.parametrize("borrowing", ["whole", "another-key"])
def test_actual_calendar_observation_cannot_borrow_event_binding(
    driver, seeded_world, calendar_source, borrowing
):
    src = calendar_source(settings={"log_from": "2027-02-01"})
    key = src["key"] if borrowing == "whole" else "another-message-key"
    locator = "whole" if borrowing == "whole" else "key:" + src["key"]
    _, evidence = save_event(driver, seeded_world, observe(src, key=key, locator=locator))
    assert evidence["source_type"] == "planned"


def test_clock_advance_does_not_promote_the_actual_calendar_evidence(
    driver, seeded_world, calendar_source, frozen
):
    src = calendar_source(FUTURE, settings={"log_from": "2027-02-01"})
    item, evidence = save_event(driver, seeded_world, observe(src, FUTURE))
    before = WorldQuery(driver, seeded_world).read("MATCH (i:Item {id: $id}) RETURN i", id=item)[0][
        "i"
    ]
    frozen.set(FUTURE + timedelta(days=1))
    after = WorldQuery(driver, seeded_world).read("MATCH (i:Item {id: $id}) RETURN i", id=item)[0][
        "i"
    ]
    stored_evidence = WorldQuery(driver, seeded_world).read(
        "MATCH (e:Evidence {id: $id}) RETURN e", id=evidence["id"]
    )[0]["e"]
    assert before == after
    assert stored_evidence["source_type"] == after["derived_source_type"] == "planned"


def test_actual_logged_calendar_evidence_reuse_keeps_valid_binding(
    driver, seeded_world, calendar_source
):
    src = calendar_source(settings={"log_from": "2027-02-01"})
    item, evidence = save_event(driver, seeded_world, observe(src))
    assert evidence["source_type"] == "confirmed"
    op = create_item(
        "task",
        "Complete the Orion session",
        "unused",
        status="done",
        evidence_refs=[{"evidence_id": evidence["id"], "polarity": "supports"}],
    )
    task = created_id(apply(driver, seeded_world, [op], [], caller=LEARNER))
    [stored] = WorldQuery(driver, seeded_world).read(
        "MATCH (i:Item {id: $id}) RETURN i.status AS status", id=task
    )
    assert stored["status"] == "done"
    attach = {
        "op": "attach_evidence",
        "op_id": "again",
        "target": item,
        "ev_id": "c1",
        "polarity": "supports",
    }
    assert apply(driver, seeded_world, [attach], [observe(src)], caller=LEARNER).state == "saved"


@pytest.mark.parametrize("legacy", ["missing-binding", "substituted-time"])
@pytest.mark.parametrize("reuse", ["evidence-id", "same-message"])
def test_invalid_legacy_confirmation_from_actual_calendar_cannot_be_reused(
    driver, seeded_world, calendar_source, monkeypatch, legacy, reuse
):
    import grove.writer.ingest as ingest_writer

    original = ingest_writer.write_screened_source

    def old_source_writer(*args, **kwargs):
        kwargs.pop("calendar_events", None)
        return original(*args, **kwargs)

    with monkeypatch.context() as old_transport:
        if legacy == "missing-binding":
            old_transport.setattr(ingest_writer, "write_screened_source", old_source_writer)
        src = calendar_source(FUTURE, settings={"log_from": "2027-02-01"})
    # Only the old typing decision is simulated. The real connector created the source.
    with monkeypatch.context() as old_writer:
        old_writer.setattr(provenance, "calendar_logged", lambda *args, **kwargs: True)
        item, evidence = save_event(driver, seeded_world, observe(src, PAST))
    assert evidence["source_type"] == "confirmed"
    if reuse == "evidence-id":
        op = create_item(
            "task",
            "Complete the Orion session",
            "unused",
            status="done",
            evidence_refs=[{"evidence_id": evidence["id"], "polarity": "supports"}],
        )
        rows = []
    else:
        op = {
            "op": "attach_evidence",
            "op_id": "again",
            "target": item,
            "ev_id": "c1",
            "polarity": "supports",
        }
        rows = [observe(src, PAST)]
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, seeded_world, [op], rows, caller=LEARNER)
    assert {refusal.rule for refusal in caught.value.refusals} == {"calendar_binding"}


@pytest.fixture
def git_source(driver, seeded_world, repository, frozen, request):
    path, _, commit = repository
    parents = []
    if getattr(request, "param", "commit") == "merge":
        parents = [
            commit("Left branch", at=PAST.isoformat()),
            commit("Right branch", at=PAST.isoformat()),
        ]
    sha = commit(
        "The Orion feature is live and the deployment is done", parents, at=PAST.isoformat()
    )
    chosen = ensure_connection(driver, seeded_world, "git", "synthetic-orion")
    update_connection(driver, seeded_world, chosen, connection_status="connected")
    result = sync_git(driver, seeded_world, chosen, path=path, screener=make_screener())
    assert result["sources_added"] == result["episodes_added"] == 1 + len(parents)
    src = stored_source(driver, seeded_world, chosen, commit_sha=sha)
    assert json.loads(src["message"]["text"])["commit"] == sha
    assert json.loads(src["message"]["text"])["artifact"] == ("merge" if parents else "commit")
    return src


@pytest.mark.parametrize("git_source", ["commit", "merge"], indirect=True)
def test_actual_git_artifact_can_confirm_that_a_commit_exists(driver, seeded_world, git_source):
    row = observe(git_source, statement="artifact")
    commit_item = created_id(
        apply(
            driver,
            seeded_world,
            [create_item("event", "The Orion commit exists", "c1")],
            [row],
            caller=LEARNER,
        )
    )
    [found] = WorldQuery(driver, seeded_world).read(
        "MATCH (e:Evidence {target_id: $id}) RETURN e", id=commit_item
    )
    evidence = found["e"]
    assert evidence["source_type"] == "confirmed"
    judgment = judge(driver, seeded_world, f"item:{commit_item}", row)
    result = apply(
        driver,
        seeded_world,
        [verify(commit_item, [{"ev_id": "c1", "judgment_id": judgment}])],
        [row],
        caller=LEARNER,
    )
    assert result.state == "saved"
    [stored] = WorldQuery(driver, seeded_world).read(
        "MATCH (i:Item {id: $id}) RETURN i.last_confirmed AS confirmed", id=commit_item
    )
    assert stored["confirmed"] is not None


def test_actual_git_message_does_not_prove_deployment_done(driver, seeded_world, git_source):
    row = observe(git_source)
    with pytest.raises(BatchDeclined) as caught:
        apply(
            driver,
            seeded_world,
            [create_item("task", "Deploy the Orion feature", "c1", status="done")],
            [row],
            caller=LEARNER,
        )
    assert {refusal.rule for refusal in caught.value.refusals} == {"terminal_needs_confirmation"}


def test_actual_git_commit_cannot_verify_a_live_feature_claim(driver, seeded_world, git_source):
    row = observe(git_source)
    feature = created_id(
        apply(
            driver,
            seeded_world,
            [
                create_item(
                    "task",
                    "The Orion feature is live",
                    "c1",
                    status="open",
                    recheck_by="2027-03-10T09:00:00+00:00",
                )
            ],
            [row],
            caller=LEARNER,
        )
    )
    judgment = judge(driver, seeded_world, f"item:{feature}", row)
    with pytest.raises(BatchDeclined) as caught:
        apply(
            driver,
            seeded_world,
            [
                verify(
                    feature,
                    [{"ev_id": "c1", "judgment_id": judgment}],
                    recheck_by="2027-03-10T09:00:00+00:00",
                )
            ],
            [row],
            caller=LEARNER,
        )
    assert {refusal.code for refusal in caught.value.refusals} == {"not_confirmable"}
    assert {refusal.rule for refusal in caught.value.refusals} <= {"source_type", "no_capability"}
