"""D10 regressions: exact retained metadata and explicit pending source work."""

import json
from datetime import UTC, datetime

import pytest

from grove.learner_input import select_input
from grove.runner import brain
from grove.writer import Caller, requests
from grove.writer.errors import BatchDeclined

AT = "2026-11-02T09:00:00+00:00"
LATER = "2026-11-02T10:00:00+00:00"
READ = datetime(2026, 11, 3, tzinfo=UTC)
WORLD = "t_ingest-b_00000000-0000-0000-0000-000000000001"


def message(key="message-1", **changes):
    return {
        "key": key,
        "text": "Mira chose the north gate.",
        "sender": "Mira",
        "at": AT,
        "speaker_kind": "person",
        "modality": "text",
        **changes,
    }


def source(rows=None, **changes):
    return {
        "id": "source-1",
        "connector_id": "connector-1",
        "parser_version": "chatgpt-1",
        "received_at": READ,
        "version": 1,
        "content": json.dumps(rows or [message()]),
        **changes,
    }


CONNECTOR = {"id": "connector-1", "type": "past_ai_conversations", "provider": "chatgpt"}


def test_message_time_order_and_speakers_come_from_retained_rows():
    picked = select_input(
        source([message("later", at=LATER, speaker_kind="automated"), message("earlier", at=AT)]),
        CONNECTOR,
        {},
    )
    assert picked.complete
    assert [m["key"] for m in picked.messages] == ["earlier", "later"]
    assert [m["at"] for m in picked.messages] == [AT, LATER]
    assert [m["speaker_kind"] for m in picked.messages] == ["person", "automated"]
    assert picked.messages[0]["locator"] == "key:earlier"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"at": None}, "source_time_unknown"),
        ({"at": "2026-11-02T09:00:00"}, "source_time_unknown"),
        ({"at": "2027-11-02T09:00:00Z"}, "source_time_unknown"),
        ({"speaker_kind": None}, "speaker_unknown"),
        ({"speaker_kind": "forwarded"}, "speaker_unknown"),
        ({"modality": "image"}, "media_unavailable"),
    ],
)
def test_unknown_or_unadmitted_input_is_held(changes, reason):
    picked = select_input(source([message(**changes)]), CONNECTOR, {})
    assert not picked.complete
    assert not picked.messages
    assert picked.held_count == 1
    assert reason in picked.reasons


def test_partial_selection_accounts_for_each_held_message():
    picked = select_input(source([message(), message("unknown", at=None)]), CONNECTOR, {})
    assert len(picked.messages) == 1 and picked.held_count == 1
    assert not picked.complete


def test_metadata_keeps_forwarded_author_and_rejects_person_substitution():
    original = "00000000-0000-0000-0000-000000000002"
    picked = select_input(
        source(
            speakers=json.dumps(
                {
                    "message-1": {
                        "kind": "forwarded",
                        "locator": "key:message-1",
                        "original_author": original,
                    }
                }
            )
        ),
        CONNECTOR,
        {},
    )
    assert picked.complete
    assert picked.messages[0]["speaker_kind"] == "forwarded"
    assert picked.messages[0]["original_author"] == original


@pytest.mark.parametrize(
    "request,speaker",
    [
        ({"verbatim": True, "host_message_id": "host-1"}, "person"),
        ({"verbatim": True}, "ai_host"),
        ({"verbatim": False, "host_message_id": "host-1"}, "ai_host"),
    ],
)
def test_host_person_voice_requires_both_verbatim_and_message_id(request, speaker):
    picked = select_input(source(), {"type": "ai_host"}, request)
    assert picked.complete
    assert picked.messages[0]["speaker_kind"] == speaker


def test_untimed_request_uses_only_its_explicit_said_at():
    item = source([message(at=None)])
    picked = select_input(item, {"type": "ai_host"}, {"said_at": AT})
    assert picked.complete and picked.messages[0]["at"] == AT
    held = select_input(item, {"type": "ai_host"}, {})
    assert not held.complete and not held.messages
    assert held.held_count == 1


def test_typed_source_has_no_model_input_even_with_claimed_admission():
    item = source([message(source_facts={"admitted": True})], parser_version="whatsapp.export.v1")
    picked = select_input(item, {"type": "whatsapp", "provider": "whatsapp"}, {})
    assert not picked.complete and not picked.messages
    assert picked.reasons == ("source_unavailable",)


@pytest.mark.parametrize(
    "meta",
    [
        {"kind": "person", "locator": "key:another-message"},
        {"kind": [], "locator": "key:message-1"},
    ],
)
def test_unusable_speaker_metadata_never_reaches_the_packet(meta):
    picked = select_input(source(speakers=json.dumps({"message-1": meta})), CONNECTOR, {})
    assert not picked.complete and not picked.messages


def test_forgotten_source_cannot_reenter_model_input():
    picked = select_input(source(forget_state="forgotten"), CONNECTOR, {})
    assert not picked.complete and not picked.messages


class PacketQuery:
    def __init__(self, item, connector=CONNECTOR, request=None):
        self.source = item
        self.connector = connector
        self.request = request or {}

    def read(self, query, **params):
        if "(r:Request" in query:
            return [
                {
                    "source": self.source["id"],
                    "status": "applying",
                    "said_at": None,
                    "verbatim": False,
                    **self.request,
                }
            ]
        if "(s:Source" in query:
            return [{**self.source, "connector": self.source["connector_id"], "received": READ}]
        if "(c:Connector" in query:
            return [{"c": self.connector}]
        if "(e:Episode" in query:
            return [{"id": "episode-1"}]
        raise AssertionError(query)


def test_brain_packet_excludes_held_text_and_preserves_exact_metadata():
    item = source(
        [message(speaker_kind="automated"), message("unknown", at=None, text="Held secret text")]
    )
    entry, blocks = brain._request_input(PacketQuery(item), "request-1", 0)
    assert len(blocks) == 1
    assert "Held secret text" not in brain.render_blocks(blocks)
    assert entry["messages"][0]["speaker_kind"] == "automated"
    assert entry["messages"][0]["at"] == AT
    assert entry["held_messages"] == 1 and not entry["input_complete"]
    evidence = brain.batch_form(entry)["batch"]["evidence"][0]
    assert evidence["source_time"] == AT
    assert evidence["locator"] == "key:message-1"
    assert evidence["speaker_kind"] == "automated"


def test_empty_eligible_packet_has_no_fabricated_batch():
    entry, blocks = brain._request_input(PacketQuery(source([message(at=None)])), "request-1", 0)
    assert not blocks
    assert "batch" not in brain.batch_form(entry)


@pytest.mark.parametrize(
    "rows,refused,status",
    [
        ([message(at=None)], True, "applying"),
        ([message(at=None)], True, "saved"),
        ([message(source_facts={"admitted": True})], True, "saved"),
        ([message()], False, "applying"),
    ],
)
def test_finish_checks_pending_source_inside_its_transaction(monkeypatch, rows, refused, status):
    item = source(rows)
    row = {"id": "request-1", "status": status, "claimed_by": "run-1", "source_id": "source-1"}
    updates = []

    class Tx:
        def run(self, query, **params):
            if query == requests._LOCK:
                return [{"r": row}]
            if "(s:Source" in query:
                return [{"s": item}]
            if "(c:Connector" in query:
                return [{"c": CONNECTOR}]
            if query == requests._UPDATE:
                updates.append(params["columns"])
                return []
            raise AssertionError(query)

    monkeypatch.setattr(requests, "_worked", lambda driver, world, key, work: work(Tx()))
    monkeypatch.setattr(requests, "_full_state", lambda driver, world, state: state)
    caller = Caller(kind="update", scopes=frozenset({"grove.propose"}), actor="agent:learner")
    if refused:
        with pytest.raises(BatchDeclined) as caught:
            requests.finish_request(None, WORLD, caller, "request-1", worker="run-1")
        assert caught.value.refusals[0].rule == "source_work_pending"
        assert updates == []
    else:
        result = requests.finish_request(None, WORLD, caller, "request-1", worker="run-1")
        assert result["status"] == "saved" and len(updates) == 1
