import json
from datetime import UTC, datetime

import httpx
import pytest

from grove.connectors.calendar import CalendarRefused, event_chat, read_calendar
from grove.ingest import ReadBudget


def event(event_id="event-one", etag="revision-one", **extra):
    return {
        "id": event_id,
        "etag": etag,
        "status": "confirmed",
        "summary": "Invented design review",
        "start": {"dateTime": "2026-11-06T11:00:00+02:00", "timeZone": "Europe/Athens"},
        "end": {"dateTime": "2026-11-06T12:00:00+02:00", "timeZone": "Europe/Athens"},
        "updated": "2026-10-30T09:00:00Z",
        "organizer": {"displayName": "Mira Vale"},
        "attendees": [{"displayName": "Cai Stone"}],
        **extra,
    }


def test_full_pagination_incremental_and_code_diff(fake_google):
    def answer(path, query):
        assert path.endswith("/calendars/selected%40example.test/events")
        assert query["singleEvents"] == "true" and query["showDeleted"] == "true"
        assert not ({"timeMin", "timeMax", "orderBy", "updatedMin"} & query.keys())
        if query.get("syncToken") == "sync-two":
            return 200, {
                "items": [event(etag="revision-two", summary="Changed review")],
                "nextSyncToken": "sync-three",
                "timeZone": "Europe/Athens",
            }
        if query.get("pageToken") == "page-two":
            return 200, {
                "items": [event("event-two")],
                "nextSyncToken": "sync-two",
                "timeZone": "Europe/Athens",
            }
        return 200, {"items": [event()], "nextPageToken": "page-two", "timeZone": "Europe/Athens"}

    fake_google.answer = answer
    with httpx.Client() as http:
        first = read_calendar(
            http, "selected@example.test", "synthetic-access", base_url=fake_google.base
        )
        assert len(first.chats) == 2 and first.sync_token == "sync-two"
        second = read_calendar(
            http,
            "selected@example.test",
            "synthetic-access",
            sync_token=first.sync_token,
            versions=first.versions,
            base_url=fake_google.base,
        )
    assert len(second.chats) == 1
    message = second.chats[0].messages[0]
    body = json.loads(message.text)
    assert body["changed_fields"] == ["summary"]
    assert body["start"]["dateTime"] == "2026-11-06T11:00:00+02:00"
    assert body["updated"] == "2026-10-30T09:00:00Z"
    assert message.at == datetime(2026, 11, 6, 9, tzinfo=UTC)
    assert message.sender == "Mira Vale" and message.speaker_kind == "third_party"
    assert "does not confirm occurrence" in body["evidence_rule"]


def test_410_discards_partial_pages_and_restarts_once(fake_google):
    def answer(_path, query):
        if query.get("pageToken"):
            return 410, {}
        if query.get("syncToken"):
            return 200, {"items": [event("partial")], "nextPageToken": "expired-page"}
        return 200, {"items": [event()], "nextSyncToken": "fresh"}

    fake_google.answer = answer
    with httpx.Client() as http:
        result = read_calendar(
            http, "primary", "synthetic-access", sync_token="expired", base_url=fake_google.base
        )
    assert result.reset and len(result.chats) == 1 and result.sync_token == "fresh"
    assert "syncToken" not in fake_google.requests[-1][1]


def test_unchanged_versions_add_nothing_and_cancelled_event_is_retained(fake_google):
    fake_google.answer = lambda *_: (200, {"items": [event()], "nextSyncToken": "a"})
    with httpx.Client() as http:
        first = read_calendar(http, "primary", "synthetic-access", base_url=fake_google.base)
        repeat = read_calendar(
            http,
            "primary",
            "synthetic-access",
            versions=first.versions,
            sync_token="a",
            base_url=fake_google.base,
        )
        assert repeat.chats == []
        fake_google.answer = lambda *_: (
            200,
            {
                "items": [{"id": "event-one", "etag": "cancelled-v2", "status": "cancelled"}],
                "nextSyncToken": "b",
            },
        )
        cancelled = read_calendar(
            http,
            "primary",
            "synthetic-access",
            versions=first.versions,
            sync_token="a",
            base_url=fake_google.base,
        )
    assert len(cancelled.chats) == 1
    assert cancelled.chats[0].messages[0].at is None
    assert json.loads(cancelled.chats[0].messages[0].text)["status"] == "cancelled"


@pytest.mark.parametrize("status", [401, 403, 410, 429, 500])
def test_failed_provider_never_returns_empty_success(fake_google, status):
    fake_google.answer = lambda *_: (status, {"message": "private provider failure"})
    with httpx.Client() as http, pytest.raises(CalendarRefused) as caught:
        read_calendar(http, "primary", "synthetic-access", base_url=fake_google.base)
    assert "private" not in str(caught.value)


def test_all_day_local_date_and_original_timezone():
    chat = event_chat(
        "selected",
        event(start={"date": "2026-11-06"}, end={"date": "2026-11-07"}),
        "America/Los_Angeles",
        ReadBudget(),
    )
    message = chat.messages[0]
    assert message.at == datetime(2026, 11, 6, 8, tzinfo=UTC)
    assert json.loads(message.text)["calendar_timezone"] == "America/Los_Angeles"


def test_ids_are_bounded_before_hash_and_keys_do_not_join_raw_separators():
    with pytest.raises(CalendarRefused):
        event_chat("selected", event(event_id="x" * 1025), "UTC", ReadBudget())
    # Component hashes retain unambiguous identities without changing the shared helper.
    a = event_chat("a", event(event_id="b", etag="c"), "UTC", ReadBudget())
    b = event_chat("a", event(event_id="bc", etag="" + "d"), "UTC", ReadBudget())
    assert a.messages[0].key != b.messages[0].key
    maximal = event_chat(
        "a" * 1024, event(event_id="b" * 1024, etag="c" * 1024), "UTC", ReadBudget()
    )
    assert maximal.messages[0].key


def test_repeated_page_token_is_a_failure(fake_google):
    fake_google.answer = lambda *_: (200, {"items": [], "nextPageToken": "same"})
    with httpx.Client() as http, pytest.raises(CalendarRefused):
        read_calendar(http, "primary", "synthetic-access", base_url=fake_google.base)


def test_account_namespace_prevents_identical_primary_event_keys(fake_google):
    fake_google.answer = lambda *_: (200, {"items": [event()], "nextSyncToken": "s"})
    with httpx.Client() as http:
        first = read_calendar(
            http, "primary", "synthetic-access", namespace="account-one", base_url=fake_google.base
        )
        second = read_calendar(
            http, "primary", "synthetic-access", namespace="account-two", base_url=fake_google.base
        )
    assert first.chats[0].messages[0].key != second.chats[0].messages[0].key


def test_datetime_with_iana_zone_and_no_offset_is_resolved():
    parsed = event_chat(
        "primary",
        event(start={"dateTime": "2026-11-06T11:00:00", "timeZone": "Europe/Athens"}),
        "UTC",
        ReadBudget(),
    )
    assert parsed.messages[0].at == datetime(2026, 11, 6, 9, tzinfo=UTC)


def test_missing_optional_status_uses_schedule_default():
    value = event()
    del value["status"]
    chat = event_chat("primary", value, "UTC", ReadBudget())
    assert json.loads(chat.messages[0].text)["status"] == "confirmed"


def test_minimal_deletion_without_etag_replays_across_reset_and_repeated_lifecycle(fake_google):
    current = event()
    fake_google.answer = lambda *_: (200, {"items": [current], "nextSyncToken": "next"})
    with httpx.Client() as http:

        def read(previous=None, token=None):
            return read_calendar(
                http,
                "primary",
                "synthetic-access",
                versions=previous,
                sync_token=token,
                base_url=fake_google.base,
            )

        initial = read()
        current = {"id": "event-one", "status": "cancelled"}
        deleted = read(initial.versions, "next")
        assert len(deleted.chats) == 1
        assert deleted.chats[0].messages[0].at is None
        assert json.loads(deleted.chats[0].messages[0].text)["status"] == "cancelled"
        assert read(deleted.versions, "next").chats == []
        fake_google.answer = lambda _path, query: (
            (410, {})
            if "syncToken" in query
            else (200, {"items": [current], "nextSyncToken": "reset"})
        )
        reset = read(deleted.versions, "expired")
        assert reset.reset and reset.chats == []
        current = event(etag="restored")
        restored = read(reset.versions)
        current = {"id": "event-one", "status": "cancelled"}
        again = read(restored.versions)
        assert len(again.chats) == 1
        assert again.chats[0].messages[0].key != deleted.chats[0].messages[0].key
