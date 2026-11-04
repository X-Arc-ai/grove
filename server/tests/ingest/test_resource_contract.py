"""Resource limits preserve episode identity, atomicity and bounded retained text."""

import pytest

from grove.ingest import ParsedMessage, message_key
from grove.ingest import take_in as take_in_module
from grove.writer import ingest as writer_ingest
from tests.ingest.test_take_in import connector, counts, episodes, export, run, sources


def test_source_parts_keep_one_session_episode(driver, seeded_world, test_clock, monkeypatch):
    monkeypatch.setattr(take_in_module, "MAX_SOURCE_CHARS", 30)
    lines = ["one two three", "four five six", "seven eight", "nine"]
    result = run(driver, seeded_world, export({"id": "parts", "lines": lines}))
    assert result.sources_added == 2
    assert result.episodes_added == 1
    (episode,) = episodes(driver, seeded_world)
    assert set(episode["source_ids"]) == {s["id"] for s in sources(driver, seeded_world)}
    assert episode["first"] == message_key("zc", "parts", 0)
    assert episode["last"] == message_key("zc", "parts", 3)
    assert episode["rule"] == "session_delta.v1"
    assert connector(driver, seeded_world)["read_count"] == 1


def test_a_later_source_failure_rolls_back_the_whole_chat(
    driver, seeded_world, test_clock, monkeypatch
):
    monkeypatch.setattr(take_in_module, "MAX_SOURCE_CHARS", 30)
    original = writer_ingest.write_screened_source
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("synthetic second source failure")
        return result

    monkeypatch.setattr(writer_ingest, "write_screened_source", fail_second)
    with pytest.raises(RuntimeError, match="synthetic second source failure"):
        run(
            driver,
            seeded_world,
            export(
                {"id": "parts", "lines": ["one two three", "four five six", "seven eight", "nine"]}
            ),
        )
    assert calls == 2
    assert counts(driver, seeded_world) == {
        "connectors": 1,
        "sources": 0,
        "episodes": 0,
        "receipts": 0,
    }
    assert "watermark" not in connector(driver, seeded_world)


def test_parsed_messages_do_not_retain_text_beyond_the_screening_cap():
    text = "word " * 100_000 + "\U0001f331"
    sender = "name " * 1000
    message = ParsedMessage(message_key("cc", "retained"), text, None, "person", sender=sender)
    body, mark = message.text.rsplit("\n", 1)
    sender_body, sender_mark = message.sender.rsplit("\n", 1)
    assert len(body) <= 100_000
    assert len(sender_body) <= 200
    assert mark == sender_mark == "[cut: the message is longer than Grove keeps]"


def test_episode_ranges_name_only_the_sources_they_use(
    driver, seeded_world, test_clock, monkeypatch
):
    from dataclasses import replace

    from grove.ingest import silence_windows
    from tests.ingest.test_take_in import SPEC

    monkeypatch.setattr(take_in_module, "MAX_SOURCE_CHARS", 30)
    monkeypatch.setattr(
        take_in_module,
        "spec_for",
        lambda provider: replace(SPEC, cut=silence_windows(90), cut_rule="silence_90m.v1"),
    )
    lines = [["one two three", 0], ["four five six", 1], ["seven eight", 200], ["nine", 201]]
    result = run(driver, seeded_world, export({"id": "windows", "lines": lines}))
    assert (result.sources_added, result.episodes_added) == (2, 2)
    rows = episodes(driver, seeded_world)
    assert all(len(row["source_ids"]) == 1 for row in rows)
    assert rows[0]["source_ids"] != rows[1]["source_ids"]
    assert {source for row in rows for source in row["source_ids"]} == set(result.source_ids)
    before = counts(driver, seeded_world)
    again = run(driver, seeded_world, export({"id": "windows", "lines": lines}))
    assert again.state == "nothing_new"
    assert counts(driver, seeded_world) == before


def test_dropped_parts_add_no_episode_and_reimport_adds_nothing(
    driver, seeded_world, test_clock, monkeypatch
):
    monkeypatch.setattr(take_in_module, "MAX_SOURCE_CHARS", 10)
    data = export(
        {"id": "dropped-parts", "lines": ["Kestrel first", "Kestrel second", "Keep this"]}
    )
    result = run(driver, seeded_world, data, denylist=["Kestrel"])
    assert (result.messages_kept, result.episodes_added) == (1, 1)
    (episode,) = episodes(driver, seeded_world)
    kept_ids = {row["id"] for row in sources(driver, seeded_world) if row["content"] != "[]"}
    assert set(episode["source_ids"]) == kept_ids
    assert result.screened == {"denylisted": 2}
    before = counts(driver, seeded_world)
    again = run(driver, seeded_world, data, denylist=["Kestrel"])
    assert again.state == "nothing_new"
    assert counts(driver, seeded_world) == before
