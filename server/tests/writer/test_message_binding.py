"""Synthetic exact-message and nested-support regressions; no database fixture."""

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from grove.writer import api, apply, composite, ontology, provenance
from grove.writer.caller import Caller
from grove.writer.errors import BatchDeclined, Refused
from grove.writer.message_binding import (
    MAX_ROWS,
    check_batch_sources,
    check_evidence_graph,
    check_observation_binding,
    retained_messages,
)

AT = "2026-11-01T09:00:00+01:00"
LATER = "2026-11-01T10:00:00+01:00"
NOW = datetime(2026, 11, 2, tzinfo=UTC)
WORLD = "t_ingest-b_11111111-1111-4111-8111-111111111111"


def message(key="message-1", at=AT, **extra):
    return {
        "key": key,
        "at": at,
        "text": "The Cedar meeting starts today.",
        "sender": "cedar-person",
        "speaker_kind": "person",
        "modality": "text",
        **extra,
    }


def observation(**extra):
    return {
        "ev_id": "ev1",
        "source_id": "source-1",
        "episode_id": "episode-1",
        "message_key": "message-1",
        "source_time": AT,
        "locator": "key:message-1",
        "speaker_kind": "person",
        "modality": "text",
        "derivation": "observed",
        "statement_class": "report_state",
        "premises": [],
        **extra,
    }


class MemoryRows:
    world_id = WORLD

    def __init__(self, *, messages=None, raw=False):
        self.rows = {
            "Source": {
                "source-1": {
                    "id": "source-1",
                    "connector_id": "connector-1",
                    "parser_version": "chatgpt-1",
                    "received_at": NOW,
                    "content": "raw message-1\nraw message-2"
                    if raw
                    else json.dumps(
                        messages
                        if messages is not None
                        else [message(), message("message-2", LATER)]
                    ),
                }
            },
            "Connector": {
                "connector-1": {
                    "id": "connector-1",
                    "type": "past_ai_conversations",
                    "provider": "chatgpt",
                }
            },
            "Episode": {
                "episode-1": {
                    "id": "episode-1",
                    "connector_id": "connector-1",
                    "source_ids": ["source-1"],
                    "first_message_key": "message-1",
                    "last_message_key": "message-2",
                    "start_time": datetime(2026, 11, 1, tzinfo=UTC),
                    "end_time": NOW,
                }
            },
            "Evidence": {},
        }
        self.request = None
        self.writes = []

    def run(self, query, **params):
        if "r.said_at AS said_at" in query:
            return [{"said_at": self.request.get("said_at")}] if self.request else []
        if "r.verbatim AS verbatim" in query:
            return [self.request] if self.request else []
        if "target_id: $target" in query:
            return [
                {"e": e}
                for e in self.rows["Evidence"].values()
                if e.get("target_id") == params["target"] and e["message_key"] == params["key"]
            ]
        if "target_id:" in query:
            return [
                {"e": e}
                for e in self.rows["Evidence"].values()
                if e.get("target_id") == params.get("id") and e.get("active")
            ]
        matched = re.search(r"\((\w+):(\w+) \{id: \$id", query)
        if matched:
            alias, label = matched.groups()
            row = self.rows.get(label, {}).get(params.get("id"))
            if "SET " in query:
                self.writes.append(query)
            return [{alias: row}] if row else []
        raise AssertionError(query)

    read = run

    def lock_world(self):
        pass

    def evidence(self, row_id="evidence-1", **extra):
        row = {**observation(), "id": row_id, "active": True, "source_type": "said", **extra}
        self.rows["Evidence"][row_id] = row
        return row


def refused(rule, fn):
    with pytest.raises(Refused) as caught:
        fn()
    assert caught.value.rule == rule


def test_own_message_time_succeeds_but_another_time_inside_episode_refuses():
    tx = MemoryRows()
    assert provenance.resolve_observation(tx, observation(), now=NOW)["source_time"].hour == 8
    refused(
        "message_source_time",
        lambda: provenance.resolve_observation(tx, observation(source_time=LATER), now=NOW),
    )


def test_key_must_equal_a_retained_key_not_a_substring_of_content():
    tx = MemoryRows()
    refused(
        "message_key_not_retained",
        lambda: check_observation_binding(
            tx, observation(message_key="message", locator="key:message")
        ),
    )


def test_locator_must_belong_to_the_exact_message():
    refused(
        "message_key_locator",
        lambda: check_observation_binding(MemoryRows(), observation(locator="key:message-2")),
    )


def test_episode_range_membership_spans_source_parts():
    tx = MemoryRows(messages=[message("outside"), message()])
    tx.rows["Source"]["source-2"] = {
        **tx.rows["Source"]["source-1"],
        "id": "source-2",
        "content": json.dumps([message("message-2", LATER)]),
    }
    tx.rows["Episode"]["episode-1"]["source_ids"] += ["source-2"]
    check_observation_binding(tx, observation())
    check_observation_binding(
        tx,
        observation(
            source_id="source-2",
            message_key="message-2",
            locator="key:message-2",
            source_time=LATER,
        ),
    )
    refused(
        "episode_message_range",
        lambda: check_observation_binding(
            tx, observation(message_key="outside", locator="key:outside")
        ),
    )
    tx.rows["Episode"]["episode-1"]["first_message_key"] = "absent"
    refused("episode_message_range", lambda: check_observation_binding(tx, observation()))


@pytest.mark.parametrize(
    "extra,rule",
    [
        ({"speaker_kind": "ai_host"}, "message_speaker"),
        ({"modality": "voice"}, "message_modality"),
        ({"source_offset_minutes": -60}, "message_source_offset"),
        ({"person_timezone": "Europe/Paris"}, "message_person_timezone"),
    ],
)
def test_message_metadata_cannot_be_substituted(extra, rule):
    refused(rule, lambda: check_observation_binding(MemoryRows(), observation(**extra)))


def test_exact_retained_source_offset_is_checked_before_utc_conversion():
    check_observation_binding(MemoryRows(), observation(source_offset_minutes=60))


def test_unknown_time_or_speaker_stays_held():
    tx = MemoryRows(messages=[message(at=None), message("message-2", LATER)])
    refused("message_time_unknown", lambda: check_observation_binding(tx, observation()))
    tx.rows["Source"]["source-1"]["content"] = json.dumps(
        [message(speaker_kind=None), message("message-2", LATER)]
    )
    refused("message_speaker_unknown", lambda: check_observation_binding(tx, observation()))


@pytest.mark.parametrize("content", [None, "raw text", "[]"])
@pytest.mark.parametrize("typed", ["parser", "provider", "facts"])
def test_typed_identity_refuses_before_any_raw_or_empty_return(content, typed):
    source, connector = {"content": content}, {}
    if typed == "parser":
        source["parser_version"] = "whatsapp.export.v1"
    elif typed == "provider":
        connector["provider"] = "document"
    else:
        source["source_facts"] = None
    refused("typed_source_unadmitted", lambda: retained_messages(source, connector))


def test_typed_facts_in_an_unselected_row_refuse_the_complete_source():
    source = {"content": json.dumps([message(), message("typed", source_facts={})])}
    refused("typed_source_unadmitted", lambda: retained_messages(source, {}))


@pytest.mark.parametrize("content", ["[", "{}", '[{"key":"a","key":"b"}]'])
def test_malformed_structured_content_never_falls_back_to_raw(content):
    refused("source_content_invalid", lambda: retained_messages({"content": content}, {}))


def test_raw_and_pointer_sources_keep_legacy_behavior():
    tx = MemoryRows(raw=True)
    assert retained_messages(tx.rows["Source"]["source-1"], {}) is None
    assert retained_messages({"content": None}, {}) is None
    provenance.resolve_observation(tx, observation(), now=NOW)


def test_untimed_host_request_uses_only_its_own_said_at():
    tx = MemoryRows(messages=[message(at=None)])
    tx.rows["Connector"]["connector-1"]["type"] = "ai_host"
    tx.rows["Episode"]["episode-1"].update(
        first_message_key="source-1:message-1",
        last_message_key="source-1:message-1",
        cut_rule="one_request",
    )
    tx.request = {"said_at": AT, "verbatim": False, "host_message_id": None, "relay": "remember"}
    row = observation(locator="whole")
    assert provenance.resolve_observation(tx, row, now=NOW)["speaker_kind"] == "ai_host"
    refused(
        "message_source_time",
        lambda: provenance.resolve_observation(tx, {**row, "source_time": LATER}, now=NOW),
    )


def typed_premise(tx):
    tx.rows["Source"]["typed-source"] = {
        **tx.rows["Source"]["source-1"],
        "id": "typed-source",
        "parser_version": "document.text.v1",
    }
    tx.evidence("typed-evidence", source_id="typed-source")
    return tx.evidence(premises=["typed-evidence"])


def test_valid_raw_legacy_outer_row_cannot_hide_a_typed_premise():
    tx = MemoryRows(raw=True)
    outer = typed_premise(tx)
    refused("typed_source_unadmitted", lambda: check_evidence_graph(tx, [outer]))
    run = SimpleNamespace(tx=tx, at=NOW)
    refused("typed_source_unadmitted", lambda: apply._existing_evidence(run, outer["id"]))
    refused(
        "typed_source_unadmitted", lambda: ontology._message_key(run, {"evidence_id": outer["id"]})
    )


def test_observed_outer_row_cannot_discard_a_typed_premise_before_checks():
    tx = MemoryRows(raw=True)
    typed_premise(tx)
    refused(
        "typed_source_unadmitted",
        lambda: provenance.resolve_observation(
            tx, observation(premises=["typed-evidence"]), now=NOW
        ),
    )


def test_nested_calendar_confirmation_keeps_its_trusted_binding_check():
    tx = MemoryRows()
    tx.rows["Connector"]["connector-1"]["type"] = "calendar"
    row = tx.evidence(source_type="confirmed")
    refused("calendar_binding", lambda: check_evidence_graph(tx, [row]))


@pytest.mark.parametrize("kind", ["missing", "inactive", "cycle", "cross-world", "depth"])
def test_dependency_walk_refuses_invalid_graphs(kind):
    tx = MemoryRows(raw=True)
    row = tx.evidence(premises=["child"])
    rule = "provenance_not_found"
    if kind == "inactive":
        tx.evidence("child", active=False)
        rule = "evidence_not_found"
    elif kind == "cycle":
        tx.evidence("child", premises=[row["id"]])
        rule = "evidence_dependency_cycle"
    elif kind == "cross-world":
        tx.evidence("child", world_id="another-world")
    elif kind == "depth":
        for index in range(18):
            tx.evidence(
                "child" if index == 0 else f"child-{index}",
                premises=[f"child-{index + 1}"] if index < 17 else [],
            )
        rule = "evidence_dependency_limit"
    refused(rule, lambda: check_evidence_graph(tx, [row]))


def test_dependency_row_limit_is_enforced():
    tx = MemoryRows(raw=True)
    children = [tx.evidence(f"child-{index}")["id"] for index in range(MAX_ROWS)]
    root = tx.evidence(premises=children)
    refused("evidence_dependency_limit", lambda: check_evidence_graph(tx, [root]))


def test_recompute_rejects_nested_typed_support_before_any_write():
    tx = MemoryRows(raw=True)
    typed_premise(tx)["target_id"] = "item-1"
    refused(
        "typed_source_unadmitted",
        lambda: apply._recompute(SimpleNamespace(tx=tx, at=NOW), "item-1", "item"),
    )
    assert not tx.writes


def test_copy_evidence_checks_before_duplicate_fast_return():
    tx = MemoryRows(raw=True)
    typed_premise(tx)["target_id"] = "old-link"
    tx.evidence("duplicate", target_id="new-link", polarity="supports")
    refused(
        "typed_source_unadmitted",
        lambda: composite._copy_evidence(
            SimpleNamespace(tx=tx, at=NOW), "old-link", "new-link", "reason"
        ),
    )
    assert not tx.writes


def test_settle_checks_existing_row_before_duplicate_fast_return():
    tx = MemoryRows(raw=True)
    typed_premise(tx).update(target_id="item-1", polarity="supports")
    plan = apply._Plan("new-evidence", "supports", observation())
    refused(
        "typed_source_unadmitted",
        lambda: apply._settle(SimpleNamespace(tx=tx, at=NOW), [plan], "item-1"),
    )
    assert plan.known is False


def test_rollup_checks_every_premise_before_completion():
    tx = MemoryRows(raw=True)
    outer = typed_premise(tx)
    original_run = tx.run

    def rows(query, **params):
        if query.startswith("MATCH (p:Item)"):
            return [{"id": "part-1", "role": "done"}]
        if "e.target_id AS target" in query:
            return [
                {
                    "id": outer["id"],
                    "target": "part-1",
                    "active": True,
                    "polarity": "supports",
                    "source_type": "confirmed",
                    "evidence": outer,
                }
            ]
        return original_run(query, **params)

    tx.run = rows
    run = SimpleNamespace(
        tx=tx, at=NOW, registry=SimpleNamespace(meanings={"parts": {"rolls_up_completion": True}})
    )
    refused(
        "typed_source_unadmitted",
        lambda: apply._rolled_up_from_parts(run, "item-1", [outer["id"]]),
    )


def test_batch_checks_nested_reused_support_even_without_direct_observations():
    tx = MemoryRows(raw=True)
    typed_premise(tx)
    refused(
        "typed_source_unadmitted",
        lambda: check_batch_sources(
            tx, {"ops": [{"worked_example": {"evidence_id": "evidence-1"}}]}
        ),
    )


@pytest.mark.parametrize("locked", [False, True])
def test_typed_request_source_is_checked_before_both_replay_returns(monkeypatch, locked):
    tx = MemoryRows()
    tx.rows["Source"]["source-1"]["parser_version"] = "whatsapp.export.v1"
    request = {"id": "request-1", "source_id": "source-1"}
    monkeypatch.setattr(api, "read_row", lambda *args: request)
    answers = iter([api.APPLY, "saved"] if locked else ["saved"])
    monkeypatch.setattr(api, "replay_rule", lambda *args: next(answers))
    monkeypatch.setattr(api, "WorldQuery", lambda *args: tx)
    monkeypatch.setattr(
        api, "WorldWriter", lambda *args: SimpleNamespace(transaction=lambda fn: fn(tx))
    )
    monkeypatch.setattr(api, "merge_row", lambda *args: (request, False))
    monkeypatch.setattr(api, "_log_decline", lambda *args: None)
    monkeypatch.setattr(api, "_answer_replay", lambda *args: pytest.fail("unsafe replay"))
    monkeypatch.setattr(api, "shape_refusals", lambda batch: ([], {}))
    caller = Caller(
        kind="update", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:l"
    )
    with pytest.raises(BatchDeclined) as caught:
        api.apply_batch(
            None,
            WORLD,
            caller,
            {"ops": [{"op": "create_item", "op_id": "o1"}], "evidence": []},
            request_id="request-1",
        )
    assert caught.value.refusals[0].rule == "typed_source_unadmitted"
