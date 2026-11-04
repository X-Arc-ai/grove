"""Code-held Calendar facts survive only the dedicated reader and retained source parts."""

import json
from dataclasses import replace

import httpx
import pytest

from grove.connectors.calendar import SPEC, CalendarRefused, event_chat, read_calendar
from grove.ingest import CalendarEvent, ConnectorSpec, ParsedChat, ReadBudget
from grove.ingest.take_in import take_in, take_parsed
from grove.store.query import WorldQuery
from grove.writer.connectors import ensure_connection
from grove.writer.errors import InvalidRecord
from tests.connectors.test_calendar import event
from tests.ingest.conftest import counts, make_screener, upload_of


def test_reader_keeps_schedule_revision_and_trusted_calendar_zone_separate():
    chat = event_chat("selected", event(), "Europe/Athens", ReadBudget())
    message = chat.messages[0]
    facts = message.calendar_event
    assert facts.scheduled_at == message.at
    assert facts.updated_at < facts.scheduled_at
    assert facts.timezone == "Europe/Athens" and facts.status == "confirmed"
    with pytest.raises(ValueError, match="schedule time"):
        replace(message, at=facts.updated_at)


def test_missing_provider_timezone_never_becomes_trusted_utc(fake_google):
    fake_google.answer = lambda *_: (200, {"items": [event()], "nextSyncToken": "s"})
    with httpx.Client() as http:
        result = read_calendar(http, "primary", "synthetic", base_url=fake_google.base)
    message = result.chats[0].messages[0]
    assert message.at is not None
    assert message.calendar_event is None
    assert json.loads(message.text)["calendar_timezone"] is None


def test_unknown_timezone_cannot_invent_all_day_time(fake_google):
    fake_google.answer = lambda *_: (
        200,
        {"items": [event(start={"date": "2026-10-01"})], "nextSyncToken": "s"},
    )
    with httpx.Client() as http, pytest.raises(CalendarRefused):
        read_calendar(http, "primary", "synthetic", base_url=fake_google.base)


def test_authoritative_facts_reject_invalid_revision_instant():
    with pytest.raises(CalendarRefused):
        event_chat("selected", event(updated="not-an-instant"), "UTC", ReadBudget())


def test_screening_drops_event_facts_with_their_message_and_keeps_retained_parts(
    driver, seeded_world, monkeypatch
):
    import grove.ingest.take_in as pipeline

    chosen = ensure_connection(driver, seeded_world, "google_calendar", "selected")
    messages = tuple(
        event_chat("selected", event(name, summary=title), "UTC", ReadBudget()).messages[0]
        for name, title in (
            ("first", "Keep first"),
            ("second", "privatephrase"),
            ("third", "Keep last"),
        )
    )
    monkeypatch.setattr(pipeline, "MAX_SOURCE_CHARS", 1)
    result = take_parsed(
        driver,
        seeded_world,
        SPEC,
        chosen,
        [ParsedChat("one-chat", messages)],
        screener=make_screener(denylist=["privatephrase"]),
    )
    # Bounded source parts remain one logical episode for this synthetic chat.
    assert result.failed_chats == 0 and result.episodes_added == 1
    sources = WorldQuery(driver, seeded_world).read("MATCH (s:Source) RETURN s")
    all_facts = {}
    for row in sources:
        retained = {msg["key"] for msg in json.loads(row["s"]["content"])}
        facts = json.loads(row["s"].get("calendar_events", "{}"))
        speakers = json.loads(row["s"].get("speakers", "{}"))
        assert set(facts) == retained == set(speakers)
        all_facts.update(facts)
    assert set(all_facts) == {messages[0].key, messages[2].key}


def test_other_reader_cannot_transport_calendar_facts(driver, seeded_world):
    chosen = ensure_connection(driver, seeded_world, "git", "sample")
    chat = event_chat("selected", event(), "UTC", ReadBudget())
    spec = ConnectorSpec("git", "git", "git_clone.v1", "commit.v1", "git", lambda _: [])
    before = counts(driver, seeded_world)
    with pytest.raises(InvalidRecord):
        take_parsed(driver, seeded_world, spec, chosen, [chat], screener=make_screener())
    assert counts(driver, seeded_world) == before


def test_upload_parser_cannot_assert_calendar_facts_before_any_source_write(
    driver, seeded_world, monkeypatch
):
    import grove.ingest.take_in as pipeline

    chat = event_chat("selected", event(), "UTC", ReadBudget())
    spec = ConnectorSpec(
        "synthetic", "notes_files_releases", "test.v1", "note.v1", "note", lambda _: [chat]
    )
    monkeypatch.setattr(pipeline, "spec_for", lambda _: spec)
    before = counts(driver, seeded_world)
    with pytest.raises(InvalidRecord):
        take_in(driver, seeded_world, "synthetic", upload_of(b"{}"), screener=make_screener())
    assert counts(driver, seeded_world) == before


def test_calendar_is_not_an_upload_format(client_for, seeded_world, driver):
    client = client_for(seeded_world)
    before = counts(driver, seeded_world)
    response = client.post(
        "/v1/sources/upload?provider=google_calendar",
        content=b'{"calendar_events":{}}',
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400
    assert counts(driver, seeded_world) == before


def test_atomic_writer_refuses_facts_for_nonretained_keys(driver, seeded_world):
    from grove.screening import Message
    from grove.writer.ingest import write_chat_delta

    chosen = ensure_connection(driver, seeded_world, "google_calendar", "selected")
    chat = event_chat("selected", event(), "UTC", ReadBudget())
    message = chat.messages[0]
    screened = make_screener(denylist=["privatephrase"]).screen_source(
        "selected", [Message(message.key, None, "privatephrase", message.at)]
    )
    before = counts(driver, seeded_world)
    with pytest.raises(InvalidRecord):
        write_chat_delta(
            driver,
            seeded_world,
            screened,
            connector_id=chosen,
            parser_version="google_calendar.v1",
            delta_keys=[message.key],
            episodes=[],
            newest_at=message.at,
            calendar_events={message.key: message.calendar_event.to_writer()},
        )
    assert counts(driver, seeded_world) == before


def test_facts_are_typed_and_code_fields_cannot_contain_prose():
    message = event_chat("selected", event(), "UTC", ReadBudget()).messages[0]
    with pytest.raises(ValueError):
        replace(message, calendar_event={"status": "confirmed"})
    with pytest.raises(ValueError):
        CalendarEvent(message.at, "Invented prose", "confirmed")
    with pytest.raises(ValueError):
        CalendarEvent(None, "UTC", "confirmed")
