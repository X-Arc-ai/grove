"""The take-in pipeline (A.4, D10, D13, D14) with a small test-only connector.

The parsers have their own tests; these prove what every connector shares: one source per chat
and read, the part added since the last read, an idempotent re-import, screening before the
sources table, the connector's coverage, and the guards that keep a chat from being stored twice
or stored with a changed key. Invented names only (R3).
"""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from grove.ingest import (
    ConnectorSpec,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    chat_locator,
    message_key,
    register,
    spec_for,
)
from grove.ingest import take_in as take_in_module
from grove.ingest.archive import json_document
from grove.ingest.connector import MAX_MESSAGE_CHARS
from grove.ingest.take_in import KEPT_OUT, NOTHING_NEW, TAKEN_IN, UNTIMED, take_in
from grove.store.query import WorldQuery
from tests.ingest.conftest import counts, make_screener, upload_of

PROVIDER = "zz_core_test"
T0 = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def _parse(upload):
    """{"chats": [{"id", "lines": [text | [text, minutes] | [text, null]], "start"?, "end"?}]}."""
    doc = json_document(upload.read_all(1_000_000))
    if not isinstance(doc, dict) or not isinstance(doc.get("chats"), list):
        raise NotAnExport("not a test export")
    for chat in doc["chats"]:
        messages = []
        for index, line in enumerate(chat["lines"]):
            text, minutes = (line, index) if isinstance(line, str) else line
            at = None if minutes is None else T0 + timedelta(minutes=minutes)
            messages.append(
                ParsedMessage(
                    key=message_key("zc", chat["id"], index),
                    text=text,
                    at=at,
                    speaker_kind="person" if index % 2 == 0 else "ai_host",
                )
            )
        start = chat.get("start")
        yield ParsedChat(
            chat_id=chat["id"],
            messages=tuple(messages),
            started_at=datetime.fromisoformat(start) if start else None,
        )


SPEC = spec_for(PROVIDER) or register(
    ConnectorSpec(
        provider=PROVIDER,
        connector_type="past_ai_conversations",
        parser_version="zz_core.v1",
        batch_rule="session_delta.v1",
        key_prefix="zc",
        parse=_parse,
    )
)


def export(*chats: dict) -> bytes:
    return json.dumps({"chats": list(chats)}).encode()


def run(driver, world_id, data: bytes, **screening):
    return take_in(driver, world_id, PROVIDER, upload_of(data), screener=make_screener(**screening))


def sources(driver, world_id) -> list[dict]:
    return WorldQuery(driver, world_id).read(
        "MATCH (s:Source) RETURN s.id AS id, s.external_locator AS locator, "
        "s.content AS content ORDER BY s.received_at, s.id"
    )


def episodes(driver, world_id) -> list[dict]:
    return WorldQuery(driver, world_id).read(
        "MATCH (e:Episode) RETURN e.first_message_key AS first, e.last_message_key AS last, "
        "e.start_time AS start, e.end_time AS end, e.cut_rule AS rule, e.run_id AS run_id, "
        "e.source_ids AS source_ids ORDER BY e.start_time, e.id"
    )


def connector(driver, world_id) -> dict:
    rows = WorldQuery(driver, world_id).read("MATCH (c:Connector) RETURN c")
    assert len(rows) == 1
    return rows[0]["c"]


CHAT_A = {"id": "chat-a", "lines": ["We meet Kestrel at nine", "Noted", "Bring the deck"]}
CHAT_B = {"id": "chat-b", "lines": ["Second chat starts", "Fine"]}


def test_first_upload_stores_one_source_and_one_episode_per_chat(driver, seeded_world, test_clock):
    result = run(driver, seeded_world, export(CHAT_A, CHAT_B))
    assert result.state == TAKEN_IN
    assert (result.chats, result.chats_new, result.sources_added, result.episodes_added) == (
        2,
        2,
        2,
        2,
    )
    assert counts(driver, seeded_world) == {
        "connectors": 1,
        "sources": 2,
        "episodes": 2,
        "receipts": 2,
    }
    stored = sources(driver, seeded_world)
    # Each chat has its own locator, and a source holds only its own chat's messages.
    assert {row["locator"] for row in stored} == {
        chat_locator("zc", "chat-a"),
        chat_locator("zc", "chat-b"),
    }
    by_locator = {row["locator"]: json.loads(row["content"]) for row in stored}
    texts_a = [row["text"] for row in by_locator[chat_locator("zc", "chat-a")]]
    assert texts_a == CHAT_A["lines"]
    rows_a = by_locator[chat_locator("zc", "chat-a")]
    assert [row["speaker_kind"] for row in rows_a] == ["person", "ai_host", "person"]
    assert {row["modality"] for row in rows_a} == {"text"}


def test_the_same_export_again_writes_nothing(driver, seeded_world, test_clock):
    run(driver, seeded_world, export(CHAT_A, CHAT_B))
    before = counts(driver, seeded_world)
    first_read = connector(driver, seeded_world)["last_attempted_read"]
    test_clock.advance(hours=1)
    again = run(driver, seeded_world, export(CHAT_A, CHAT_B))
    assert again.state == NOTHING_NEW
    assert again.to_api()["sources_added"] == 0 and again.episodes_added == 0
    assert counts(driver, seeded_world) == before
    row = connector(driver, seeded_world)
    assert row["last_outcome"] == "nothing_new"
    assert row["last_attempted_read"] > first_read
    assert row["read_count"] == 2


def test_a_grown_chat_adds_only_the_part_since_the_last_read(driver, seeded_world, test_clock):
    run(driver, seeded_world, export(CHAT_A, CHAT_B))
    grown = {**CHAT_A, "lines": [*CHAT_A["lines"], "Deck sent", "Thanks"]}
    result = run(driver, seeded_world, export(grown, CHAT_B))
    assert result.state == TAKEN_IN
    assert (result.sources_added, result.episodes_added, result.messages_read) == (1, 1, 2)
    (newest,) = [row for row in sources(driver, seeded_world) if row["id"] in result.source_ids]
    assert newest["locator"] == chat_locator("zc", "chat-a")
    assert [row["text"] for row in json.loads(newest["content"])] == ["Deck sent", "Thanks"]
    first_new = [
        ep for ep in episodes(driver, seeded_world) if ep["first"] == message_key("zc", "chat-a", 3)
    ]
    assert len(first_new) == 1
    assert first_new[0]["last"] == message_key("zc", "chat-a", 4)
    assert first_new[0]["source_ids"] == [newest["id"]]


def test_episodes_take_the_messages_own_times_not_the_read_time(driver, seeded_world, test_clock):
    test_clock.set(datetime(2027, 1, 1, tzinfo=UTC))
    chat = {"id": "timed", "lines": [["one", 5], ["two", 50], ["three", 400]]}
    run(driver, seeded_world, export(chat))
    (episode,) = episodes(driver, seeded_world)
    assert episode["start"] == T0 + timedelta(minutes=5)
    assert episode["end"] == T0 + timedelta(minutes=400)
    assert episode["rule"] == "session_delta.v1"
    # No pass has taken it yet: an episode with no run id is the handoff to the update agent.
    assert episode["run_id"] is None
    row = connector(driver, seeded_world)
    assert row["watermark"] == "at:" + (T0 + timedelta(minutes=400)).isoformat()
    assert row["oldest_unprocessed_item"] == T0 + timedelta(minutes=5)


def test_a_chat_with_no_times_falls_back_to_its_own_time_then_the_read_time(
    driver, seeded_world, test_clock
):
    started = "2026-08-01T10:00:00+00:00"
    with_start = {"id": "with-start", "lines": [["a", None], ["b", None]], "start": started}
    untimed = {"id": "untimed", "lines": [["c", None]]}
    run(driver, seeded_world, export(with_start, untimed))
    rows = {ep["first"]: ep for ep in episodes(driver, seeded_world)}
    dated = rows[message_key("zc", "with-start", 0)]
    assert dated["start"] == datetime.fromisoformat(started)
    assert dated["rule"] == "session_delta.v1"
    bare = rows[message_key("zc", "untimed", 0)]
    assert bare["start"] == test_clock.now()
    assert bare["rule"] == "session_delta.v1" + UNTIMED


def test_screening_runs_before_the_sources_table(driver, seeded_world, test_clock):
    chat = {
        "id": "screened",
        "lines": [
            "Call Orla Pennick on +44 7700 900123 tomorrow",
            "Will do",
            "The Brightwell contract is late",
            "Understood",
        ],
    }
    result = run(
        driver,
        seeded_world,
        export(chat),
        rename={"Orla Pennick": "Co-founder"},
        denylist=["Brightwell"],
    )
    assert result.screened == {"contact_phone": 1, "denylisted": 1, "renamed": 1}
    (stored,) = sources(driver, seeded_world)
    assert "Orla" not in stored["content"]
    assert "7700" not in stored["content"]
    assert "Brightwell" not in stored["content"]
    texts = [row["text"] for row in json.loads(stored["content"])]
    assert texts[0] == "Call Co-founder on [phone 1] tomorrow"
    assert len(texts) == 3
    receipt = WorldQuery(driver, seeded_world).read(
        "MATCH (r:ScreeningReceipt) RETURN r.counts_per_class AS counts, r.source_id AS source"
    )
    assert receipt == [
        {
            "counts": json.dumps({"contact_phone": 1, "denylisted": 1, "renamed": 1}),
            "source": stored["id"],
        }
    ]


def test_a_chat_screening_drops_whole_is_stored_empty_once(driver, seeded_world, test_clock):
    chat = {"id": "kept-out", "lines": ["Kestrel order", "Kestrel invoice"]}
    first = run(driver, seeded_world, export(chat), denylist=["Kestrel"])
    assert first.state == KEPT_OUT
    (stored,) = sources(driver, seeded_world)
    assert stored["content"] == "[]"
    # No message was kept, so no episode exists for the update agent.
    assert episodes(driver, seeded_world) == []
    before = counts(driver, seeded_world)
    again = run(driver, seeded_world, export(chat), denylist=["Kestrel"])
    assert again.state == NOTHING_NEW
    assert counts(driver, seeded_world) == before
    assert connector(driver, seeded_world)["last_outcome"] == "nothing_new"


def test_a_re_import_with_dropped_messages_stays_idempotent(driver, seeded_world, test_clock):
    chat = {"id": "mixed", "lines": ["Kestrel news", "ok", "Lunch at one", "sure"]}
    run(driver, seeded_world, export(chat), denylist=["Kestrel"])
    before = counts(driver, seeded_world)
    again = run(driver, seeded_world, export(chat), denylist=["Kestrel"])
    assert again.state == NOTHING_NEW
    assert counts(driver, seeded_world) == before


def test_a_file_that_is_no_export_writes_nothing(driver, seeded_world, test_clock):
    with pytest.raises(NotAnExport):
        run(driver, seeded_world, b"[1, 2, 3]")
    assert counts(driver, seeded_world) == {
        "connectors": 0,
        "sources": 0,
        "episodes": 0,
        "receipts": 0,
    }


def test_a_changed_key_fails_the_chat_closed(driver, seeded_world, test_clock, monkeypatch):
    real = take_in_module._messages

    def altered(delta):
        return [
            type(message)(**{**message.__dict__, "key": message.key + "x"})
            for message in real(delta)
        ]

    monkeypatch.setattr(take_in_module, "_messages", altered)
    result = run(driver, seeded_world, export(CHAT_A))
    assert result.failed_chats == 1
    assert result.state == "failed"
    assert counts(driver, seeded_world)["sources"] == 0
    row = connector(driver, seeded_world)
    assert row["last_outcome"] == "failed"
    assert row["failed_count"] == 1
    assert row.get("last_successful_read") is None


def test_a_delta_another_upload_stored_first_is_read_again(
    driver, seeded_world, test_clock, monkeypatch
):
    run(driver, seeded_world, export(CHAT_A))
    before = counts(driver, seeded_world)
    real = take_in_module.stored_keys
    calls = {"n": 0}

    def stale_first(query, connector_id, locator):
        calls["n"] += 1
        if calls["n"] == 1:
            return set(), 0
        return real(query, connector_id, locator)

    monkeypatch.setattr(take_in_module, "stored_keys", stale_first)
    result = run(driver, seeded_world, export(CHAT_A))
    # The stale read made a delta; the write saw the keys under the lock and refused it; the
    # second read found nothing new.
    assert calls["n"] == 2
    assert result.state == NOTHING_NEW and result.failed_chats == 0
    assert counts(driver, seeded_world) == before


def test_two_uploads_of_one_export_at_once_store_it_once(driver, seeded_world, test_clock):
    chats = [{"id": f"race-{n}", "lines": [f"line {n}", "reply", "more"]} for n in range(6)]
    data = export(*chats)
    results, errors = [], []

    def go():
        try:
            results.append(run(driver, seeded_world, data))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert counts(driver, seeded_world) == {
        "connectors": 1,
        "sources": 6,
        "episodes": 6,
        "receipts": 6,
    }
    assert sum(result.sources_added for result in results) == 6


def test_coverage_of_a_read(driver, seeded_world, test_clock):
    result = run(driver, seeded_world, export(CHAT_A, CHAT_B))
    row = connector(driver, seeded_world)
    assert row["id"] == result.connector_id
    assert row["type"] == "past_ai_conversations"
    assert row["provider"] == PROVIDER
    assert row["auth_reference"] == "upload"
    assert row["batch_rule"] == "session_delta.v1"
    assert row["last_outcome"] == "took_in"
    assert row["last_successful_read"] == test_clock.now()
    assert row["read_count"] == 2
    assert row["failed_count"] == 0
    assert row["version"] >= 3


def test_a_message_past_the_cap_is_cut_at_a_space_before_screening(
    driver, seeded_world, test_clock
):
    """The screener's copies of a text take many times its size: one crafted message must not
    take the API's memory. The cut is the same on a re-import, so the keys stay stored."""
    long_text = "Kestrel " + "plan " * 30_000
    data = export({"id": "chat-long", "lines": [long_text, "Noted"]})

    result = run(driver, seeded_world, data, rename={"Kestrel": "Person K"})

    assert result.state == TAKEN_IN
    (row,) = sources(driver, seeded_world)
    first, second = (message["text"] for message in json.loads(row["content"]))
    body, mark = first.rsplit("\n", 1)
    assert mark == "[cut: the message is longer than Grove keeps]"
    assert len(body) <= MAX_MESSAGE_CHARS
    assert body.startswith("Person K plan plan") and set(body.split()[2:]) == {"plan"}
    assert second == "Noted"
    assert run(driver, seeded_world, data, rename={"Kestrel": "Person K"}).state == NOTHING_NEW


def test_a_chat_past_the_source_cap_is_stored_in_parts(
    driver, seeded_world, test_clock, monkeypatch
):
    """A crafted chat must not reach the store as one huge property: its new part is cut into
    sources of at most MAX_SOURCE_CHARS characters, with one session episode across them."""
    monkeypatch.setattr(take_in_module, "MAX_SOURCE_CHARS", 30)
    lines = [["one two three", 0], ["four five six", 1], ["seven eight", 2], ["nine", 3]]
    data = export({"id": "chat-parts", "lines": lines})

    result = run(driver, seeded_world, data)

    assert result.state == TAKEN_IN
    assert (result.chats_new, result.sources_added, result.episodes_added) == (1, 2, 1)
    stored = [json.loads(row["content"]) for row in sources(driver, seeded_world)]
    assert sorted([m["text"] for m in content] for content in stored) == [
        ["one two three", "four five six"],
        ["seven eight", "nine"],
    ]
    assert all(sum(len(m["text"]) for m in content) <= 30 for content in stored)
    again = run(driver, seeded_world, data)
    assert again.state == NOTHING_NEW
    assert counts(driver, seeded_world)["sources"] == 2


def test_parts_hold_one_message_at_least_and_keep_the_order(monkeypatch):
    monkeypatch.setattr(take_in_module, "MAX_SOURCE_CHARS", 10)

    def made(index, text):
        return ParsedMessage(
            key=message_key("zc", index), text=text, at=None, speaker_kind="person"
        )

    delta = [made(1, "a" * 25), made(2, "bbb"), made(3, "cccc"), made(4, "dddd")]
    parts = take_in_module.parts_of(delta)
    assert [[m.text for m in part] for part in parts] == [["a" * 25], ["bbb", "cccc"], ["dddd"]]
    assert take_in_module.parts_of([]) == []
