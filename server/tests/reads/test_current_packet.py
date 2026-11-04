"""Current cards from synthetic detached rows; no store, provider or model is used."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from grove.reads.capture import encode
from grove.reads.coverage import ConnectorState, evaluate_coverage
from grove.reads.current_packet import (
    MAX_CARD_BYTES,
    CurrentRenderer,
    CurrentRows,
    MarkExplanation,
    PacketError,
)
from grove.store.primitives import (
    KIND_BELIEF,
    MEANING_AFFECTED_BY,
    MEANING_CORRECTED_BY,
    MEANING_DESCRIBES,
)

WORLD = "t_hosts-b1_11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def row(identity, **values):
    return {"id": identity, "world_id": WORLD, **values}


def item(identity="subject", **values):
    return row(
        identity,
        **{
            "kind": "work",
            "title": identity,
            "summary": "Current summary",
            "properties": '{"priority":"high"}',
            "status": "active",
            "status_role": "active",
            "status_since": NOW - 2 * HOUR,
            "recorded": NOW - 3 * HOUR,
            "last_confirmed": NOW - 2 * HOUR,
            "recheck_by": NOW + HOUR,
            "became_true_earliest": NOW - 3 * HOUR,
            "became_true_latest": NOW - 3 * HOUR,
            "became_true_precision": "exact",
            "derived_source_type": "confirmed",
            "derivation": "direct",
            "speaker": "the_person",
            "modality": "asserted",
            **values,
        },
    )


def link(identity="link", start="subject", end="other", meaning="related", **values):
    return row(
        identity,
        **{
            "from_item": start,
            "to_item": end,
            "meaning": meaning,
            "active_key": identity,
            "recorded": NOW - HOUR,
            "derived_source_type": "confirmed",
            **values,
        },
    )


def proof(identity="proof", **values):
    return row(
        identity,
        **{
            "source_id": "source",
            "episode_id": "episode",
            "locator": "line:2",
            "message_key": "message-002",
            "source_time": NOW - HOUR,
            "source_offset_minutes": 60,
            "person_timezone": "Africa/Casablanca",
            "recorded": NOW,
            "target_type": "item",
            "target_id": "subject",
            "polarity": "supports",
            "source_type": "confirmed",
            "derivation": "direct",
            "speaker_kind": "person",
            "modality": "asserted",
            "active": True,
            **values,
        },
    )


def rows(**values):
    defaults = dict(
        world_id=WORLD,
        items=[item(provenance_evidence_id="proof")],
        kinds=[row("work", name="work", lifecycle_class="work")],
        meanings=[row("related", name="related")],
        evidence=[proof()],
        sources=[row("source", connector_id="connector", content="Header\nCurrent proof\nTail")],
        episodes=[row("episode", connector_id="connector", source_ids=["source"])],
    )
    return CurrentRows(**(defaults | values))


def coverage(**values):
    state = ConnectorState(
        **{
            "world_id": WORLD,
            "connector_id": "connector",
            "source_kind": "calendar",
            "last_outcome": "nothing_new",
            "expected_read_interval": 3600,
            "last_successful_read": NOW,
            **values,
        }
    )
    return evaluate_coverage(
        WORLD, [state], now=NOW, inventory_complete=True, disclosure_complete=True
    )


def renderer(data=None, **values):
    return CurrentRenderer(
        data or rows(),
        **{
            "captured_at": NOW,
            "coverage": coverage(),
            "complete": True,
            **values,
        },
    )


def card(data=None, **values):
    return renderer(data, **values).render_item("subject").card


def test_required_fields_and_exact_provenance_are_from_current_rows():
    data = rows(
        items=[
            item(
                provenance_evidence_id="proof",
                provenance_reason="reason",
                became_true_words="start",
                body="BODY_DO_NOT_COPY",
                search_terms="OLD_SEARCH_VALUE",
                property_values="SEARCH_ONLY",
            )
        ],
        history_values=[
            row("reason", value="Recorded source"),
            row("start", value="three hours ago"),
            row("old", value="OLD_TITLE"),
        ],
    )
    result = card(data)
    assert result["id"] == "subject"
    assert result["kind"] == result["kind_name"] == "work"
    assert result["title"] == "subject"
    assert result["summary"] == "Current summary"
    assert result["properties"] == {"priority": "high"}
    assert result["status_since"] == NOW - 2 * HOUR
    assert result["standing"] == "current"
    assert result["source_type"] == "confirmed"
    assert result["derivation"] == "direct"
    assert result["speaker"] == "the_person"
    assert result["modality"] == "asserted"
    assert result["clocks"]["became_true"]["words"] == "three hours ago"
    assert result["clocks"]["became_true"]["earliest"] == NOW - 3 * HOUR
    assert result["clocks"]["recorded"] == NOW - 3 * HOUR
    assert result["clocks"]["last_confirmed"] == NOW - 2 * HOUR
    assert result["clocks"]["recheck_by"] == NOW + HOUR
    assert result["provenance_reason"] == "Recorded source"
    evidence = result["newest_evidence"]
    assert evidence == result["provenance"]
    assert evidence["source_time"] == NOW - HOUR
    assert evidence["source"] == "source"
    assert evidence["episode"] == "episode"
    assert evidence["locator"] == "line:2"
    assert evidence["message_key"] == "message-002"
    assert evidence["excerpt"] == "Current proof"
    assert evidence["source_offset_minutes"] == 60
    assert result["flags"] == ["newer_evidence_than_state"]
    assert result["coverage_gap"] is None
    assert all(
        value not in encode(result).decode()
        for value in ("BODY_DO_NOT_COPY", "OLD_SEARCH_VALUE", "SEARCH_ONLY", "OLD_TITLE")
    )


def test_newest_uses_source_time_then_recorded_then_id_and_excludes_inactive():
    values = [
        proof("older", source_time=NOW - 2 * HOUR, recorded=NOW + HOUR),
        proof("tie-a"),
        proof("tie-b"),
        proof("inactive", source_time=NOW + HOUR, active=False),
    ]
    result = card(rows(items=[item()], evidence=values))
    assert result["newest_evidence"]["id"] == "tie-b"
    assert card(rows(items=[item()], evidence=list(reversed(values)))) == result


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            {"last_confirmed": None, "recheck_by": NOW},
            ["never_confirmed", "past_recheck_by", "newer_evidence_than_state"],
        ),
        ({"last_confirmed": NOW, "recheck_by": NOW - HOUR}, ["past_recheck_by"]),
        ({"status_since": NOW, "last_confirmed": NOW - 2 * HOUR}, []),
        ({"status_since": NOW - HOUR, "last_confirmed": NOW - HOUR}, []),
    ],
)
def test_flags_preserve_distinct_evidence_state_and_recheck_times(values, expected):
    assert card(rows(items=[item(**values)]))["flags"] == expected


def test_timestamps_compare_utc_instants_across_a_dst_fold():
    zone = ZoneInfo("America/New_York")
    before = datetime(2026, 11, 1, 1, 50, tzinfo=zone, fold=0)
    after = datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    result = card(
        rows(
            items=[item(status_since=before, last_confirmed=before)],
            evidence=[proof(source_time=after)],
        )
    )
    assert "newer_evidence_than_state" in result["flags"]
    assert result["newest_evidence"]["source_time"] == after.astimezone(UTC)


@pytest.mark.parametrize(
    ("kind", "values", "expected"),
    [
        ("work", {"derived_source_type": "planned"}, "current"),
        ("bounded_period", {"derived_source_type": "planned"}, "planned"),
        ("bounded_period", {"derived_source_type": "reported_done"}, "reported"),
        ("bounded_period", {"derivation": "inferred"}, "planned"),
        ("bounded_period", {"became_true_precision": "unknown"}, "planned"),
        ("bounded_period", {}, "current"),
    ],
)
def test_canonical_d3_does_not_promote_plans_or_reports(kind, values, expected):
    result = card(
        rows(items=[item(**values)], kinds=[row("work", name="work", lifecycle_class=kind)])
    )
    assert result["standing"] == expected


def test_planned_origin_and_scheduled_roles_use_canonical_d3():
    data = rows(
        items=[item(derived_source_type="reported_done", properties={"starts": "tomorrow"})],
        kinds=[row("work", name="record", lifecycle_class="record")],
        property_names=[row("start-name", kind="work", name="starts", scheduled_role="start")],
        evidence=[proof(source_type="planned")],
    )
    result = card(data)
    assert result["standing"] == "reported"
    assert result["scheduled"] == {"start": "tomorrow"}


@pytest.mark.parametrize(
    "values",
    [{"end_class": "ended"}, {"status_role": "done"}, {"became_true_earliest": NOW + HOUR}],
)
def test_ended_or_not_started_item_is_ineligible_without_a_successor(values):
    held = renderer(rows(items=[item(**values)]))
    assert "subject" not in held.eligible_ids
    with pytest.raises(PacketError, match="^item_not_current$"):
        held.render_item("subject")


@pytest.mark.parametrize(
    ("values", "state"), [({}, "overtaken"), ({"end_class": "refuted"}, "ended")]
)
def test_historical_discovery_retains_successor_and_old_state(values, state):
    data = rows(
        items=[item(**values), item("successor")],
        links=[link(end="successor", meaning=MEANING_CORRECTED_BY)],
    )
    result = card(data)
    assert result["standing"] == state
    assert result["successors"][0]["item"] == "successor"
    assert result["successors"][0]["standing"] == "current"


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("line:2-3", "Current proof\nTail"),
        ("char:7-14", "Current"),
        ("key:proof", "Current proof"),
        ("whole", "Header\nCurrent proof\nTail"),
    ],
)
def test_excerpt_uses_stored_locator_semantics(locator, expected):
    assert card(rows(evidence=[proof(locator=locator)]))["newest_evidence"]["excerpt"] == expected


def test_stored_excerpt_is_bounded_without_altering_exact_locator():
    result = card(
        rows(
            sources=[row("source", connector_id="connector", content="x" * 6000)],
            evidence=[proof(locator="whole")],
        )
    )
    assert result["newest_evidence"]["excerpt"] == "x" * 4000
    assert result["newest_evidence"]["locator"] == "whole"


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"sources": []}, "missing_evidence_provenance"),
        ({"episodes": []}, "missing_evidence_provenance"),
        (
            {"episodes": [row("episode", connector_id="connector", source_ids=[])]},
            "missing_evidence_provenance",
        ),
        (
            {"episodes": [row("episode", connector_id="different", source_ids=["source"])]},
            "invalid_evidence_provenance",
        ),
        (
            {"sources": [row("source", connector_id="connector", content=None)]},
            "stored_excerpt_unavailable",
        ),
        ({"evidence": [proof(locator="line:99")]}, "invalid_evidence_locator"),
        ({"evidence": [proof(source_time=None)]}, "missing_evidence_time"),
        ({"evidence": [proof(speaker_item="hidden")]}, "missing_evidence_speaker"),
        ({"evidence": [proof(original_author="hidden")]}, "missing_evidence_speaker"),
        ({"items": [item(provenance_evidence_id="hidden")]}, "missing_item_provenance"),
        ({"items": [item(provenance_reason="hidden")]}, "missing_packet_value"),
    ],
)
def test_missing_or_invalid_provenance_refuses_without_revealing_identifiers(changes, error):
    with pytest.raises(PacketError, match=f"^{error}$"):
        card(rows(**changes))


def test_provenance_cannot_point_at_another_items_evidence():
    data = rows(
        items=[item(provenance_evidence_id="proof"), item("other")],
        evidence=[proof(target_id="other")],
    )
    with pytest.raises(PacketError, match="^invalid_item_provenance$"):
        card(data)


def test_future_or_ended_links_and_ineligible_endpoints_do_not_enter_counts():
    data = rows(
        items=[item(), item("other"), item("ended", end_class="ended")],
        links=[
            link("kept"),
            link("past", active_key=None),
            link("future", became_true_earliest=NOW + HOUR),
            link("to-ended", end="ended"),
            link("standing-until-future", active_key=None, stopped_being_true_earliest=NOW + HOUR),
        ],
    )
    result = renderer(data).render_item("subject")
    assert result.link_ids == ("kept", "standing-until-future")
    assert result.card["link_counts"]["total"] == 2
    assert result.card["links_remaining"] == 0
    assert result.card["links_need_cursor"] is False


def test_hub_has_complete_order_counts_five_per_meaning_and_explicit_continuation():
    others = [item(f"other-{index:04}") for index in range(5000)]
    links = [
        link(f"link-{index:04}", end=value["id"], recorded=NOW - index * HOUR)
        for index, value in enumerate(others)
    ]
    result = renderer(rows(items=[item(), *others], links=list(reversed(links)))).render_item(
        "subject"
    )
    assert result.link_ids == tuple(value["id"] for value in links)
    assert [value["id"] for value in result.card["links"]] == list(result.link_ids[:5])
    assert result.card["link_counts"] == {
        "total": 5000,
        "by_meaning": {"related": 5000},
        "by_kind": {"work": 5000},
    }
    assert result.card["links_remaining"] == 4995
    assert result.card["links_need_cursor"] is True
    assert "cursor" not in result.card
    assert len(encode(result.card)) <= MAX_CARD_BYTES


def test_per_meaning_previews_respect_total_budget_and_retain_complete_counts():
    meanings = [row(f"meaning-{index}", name=f"meaning {index}") for index in range(12)]
    links = [
        link(f"{meaning['id']}-{index}", meaning=meaning["id"], recorded=NOW - index * HOUR)
        for meaning in meanings
        for index in range(6)
    ]
    result = card(rows(items=[item(), item("other")], meanings=meanings, links=links))
    assert len(result["links"]) == 50
    for meaning in meanings:
        assert sum(value["meaning_id"] == meaning["id"] for value in result["links"]) <= 5
        assert result["link_counts"]["by_meaning"][meaning["id"]] == 6
    assert result["links_remaining"] == 22
    assert result["links_need_cursor"] is True


def marked(**changes):
    return rows(
        items=[item(), item("other")],
        links=[link(meaning=MEANING_AFFECTED_BY)],
        impacts=[
            {
                "world_id": WORLD,
                "link_id": "link",
                "affected_item": "subject",
                "state": "open",
                "path": ["link"],
                "rule": "changed_input",
                "depth": 1,
                "stakes": "high",
                **changes,
            }
        ],
    )


def test_marks_use_cached_text_or_prepared_owner_output_without_callbacks():
    cached = replace(
        marked(explanation="explanation"),
        history_values=[row("explanation", value="The dependency changed.")],
    )
    result = card(cached)["affected_by"][0]
    assert result["explanation"] == "The dependency changed."
    assert result["needs_explanation"] is False
    result = card(
        marked(), mark_explanations=[MarkExplanation("link", "A current mark exists.", True)]
    )
    assert result["affected_by"][0]["needs_explanation"] is True
    with pytest.raises(PacketError, match="^invalid_mark_explanation$"):
        renderer(marked(), mark_explanations=[lambda: "not an owner output"])


def test_missing_explanation_does_not_make_a_mark_disappear():
    with pytest.raises(PacketError, match="^mark_explanation_unavailable$"):
        card(marked())


def test_forgotten_explanation_is_not_replaced_by_prepared_text():
    data = replace(
        marked(explanation="old"), history_values=[row("old", value="Forgotten", forgotten_at=NOW)]
    )
    with pytest.raises(PacketError, match="^missing_packet_value$"):
        card(data, mark_explanations=[MarkExplanation("link", "Fallback", True)])


def contradiction(**changes):
    return row(
        "contradiction",
        **{
            "target": "subject",
            "standing_target_id": "subject",
            "standing_change_id": 7,
            "fresh_evidence_id": "proof",
            "detected_at": NOW,
            "status": "open",
            **changes,
        },
    )


def test_contradiction_keeps_standing_sequence_distinct_from_fresh_proof():
    data = rows(
        contradictions=[contradiction()],
        change_entries=[
            {
                "world_id": WORLD,
                "sequence": 7,
                "entity_id": "subject",
                "commit_id": "different-journal-commit",
            }
        ],
    )
    result = card(data)["contradictions"][0]
    assert result["standing"] == {"item": "subject", "change_sequence": 7}
    assert result["fresh"]["id"] == "proof"
    assert "different-journal-commit" not in encode(result).decode()
    data = replace(
        data,
        contradictions=[
            contradiction(fresh_evidence_id=None, fresh_source_id="source", fresh_locator="line:3")
        ],
    )
    assert card(data)["contradictions"][0]["fresh"]["excerpt"] == "Tail"


def test_missing_contradiction_standing_cannot_be_hidden():
    with pytest.raises(PacketError, match="^missing_contradiction_standing$"):
        card(rows(contradictions=[contradiction()]))


@pytest.mark.parametrize("state", ["tentative", "established", "weakening"])
def test_describing_belief_retains_state_episode_count_and_latest_retest(state):
    data = rows(
        items=[item(), item("belief", kind=KIND_BELIEF)],
        links=[link(start="belief", end="subject", meaning=MEANING_DESCRIBES)],
        beliefs=[
            {
                "world_id": WORLD,
                "item_id": "belief",
                "state": state,
                "state_since": NOW - 3 * HOUR,
                "supporting_episodes": ["episode", "episode"],
            }
        ],
        verifications=[
            row("test-one", claim_id="belief", at=NOW - HOUR),
            row("test-two", claim_id="belief", at=NOW),
        ],
    )
    result = card(data)["beliefs"][0]
    assert result["state"] == state
    assert result["supporting_episode_count"] == 1
    assert result["last_retest_at"] == NOW
    assert result["state_since"] == NOW - 3 * HOUR


def test_coverage_gap_retains_evidence_and_does_not_guess_recheck_sources():
    result = card(coverage=coverage(last_outcome="failed"))
    assert result["newest_evidence"]["id"] == "proof"
    assert result["coverage_gap"]["connector_ids"] == ("connector",)
    data = rows(items=[item(recheck_how='"ask_the_person"')])
    assert card(data)["coverage_gap"]["incomplete"] is True
    assert card(data, recheck_references={"subject": {"complete": True}})["coverage_gap"] is None


@pytest.mark.parametrize("how", ['"ask_the_person"', "ask_the_person"])
def test_recheck_accepts_stored_or_detached_person_code(how):
    assert card(rows(items=[item(recheck_how=how)]))["clocks"]["recheck_how"] == "ask_the_person"


def test_hidden_connector_reference_is_not_exposed_in_recheck_method():
    with pytest.raises(PacketError, match="^missing_recheck_connector$"):
        card(rows(items=[item(recheck_how={"connector": "hidden", "query": "private"})]))


@pytest.mark.parametrize("stored_json", [False, True])
def test_recheck_connector_type_resolves_explicit_authorized_instance(stored_json):
    how = {"connector": "web_pages", "locator_or_query": "synthetic page", "compare": "date"}
    data = rows(items=[item(recheck_how=json.dumps(how) if stored_json else how)])
    result = card(
        data,
        coverage=coverage(source_kind="web_pages"),
        recheck_references={"subject": {"connector_ids": ["connector"], "complete": True}},
    )
    assert result["clocks"]["recheck_how"] == how
    assert "by checking a fetched page again" in result["clocks"]["recheck_display"]
    assert result["coverage_gap"] is None


def test_recheck_kind_match_does_not_hide_an_unavailable_explicit_reference():
    data = rows(items=[item(recheck_how={"connector": "web_pages"})])
    with pytest.raises(PacketError, match="^missing_recheck_connector$"):
        card(
            data,
            coverage=coverage(source_kind="web_pages"),
            recheck_references={"subject": {"connector_ids": ["hidden"], "complete": True}},
        )


def test_recheck_explicit_instance_must_match_the_canonical_type():
    web_coverage = coverage(source_kind="web_pages", connector_id="pages")
    statement = replace(web_coverage, sources=(*web_coverage.sources, *coverage().sources))
    data = rows(items=[item(recheck_how={"connector": "web_pages"})])
    with pytest.raises(PacketError, match="^missing_recheck_connector$"):
        card(
            data,
            coverage=statement,
            recheck_references={"subject": {"connector_ids": ["connector"], "complete": True}},
        )


def test_recheck_unresolved_type_stays_incomplete_and_cannot_claim_complete_references():
    data = rows(items=[item(recheck_how={"connector": "web_pages"})])
    statement = coverage(source_kind="web_pages")
    result = card(data, coverage=statement)
    assert result["clocks"]["recheck_how"] == {"connector": "web_pages"}
    assert result["coverage_gap"]["incomplete"] is True
    with pytest.raises(PacketError, match="^missing_recheck_connector$"):
        card(data, coverage=statement, recheck_references={"subject": {"complete": True}})


def test_recheck_explicit_unconnected_type_has_a_safe_coverage_gap():
    data = rows(items=[item(recheck_how={"connector": "web_pages"})])
    statement = replace(coverage(), not_connected=("web_pages",))
    result = card(
        data,
        coverage=statement,
        recheck_references={"subject": {"missing_kinds": ["web_pages"], "complete": True}},
    )
    assert result["coverage_gap"]["not_connected"] == ("web_pages",)
    assert result["coverage_gap"]["incomplete"] is False


def test_recheck_type_resolution_uses_only_explicit_instances_for_coverage():
    statement = coverage(source_kind="web_pages")
    other = replace(statement.sources[0], connector_id="unrelated-pages", reasons=("failed",))
    statement = replace(statement, sources=(*statement.sources, other))
    data = rows(items=[item(recheck_how={"connector": "web_pages"})])
    result = card(
        data,
        coverage=statement,
        recheck_references={"subject": {"connector_ids": ["connector"], "complete": True}},
    )
    assert result["coverage_gap"] is None


@pytest.mark.parametrize("claim", [None, "other"])
def test_confirmation_requires_the_captured_verification_for_this_item(claim):
    data = rows(
        items=[item(confirmed_against="verification")],
        verifications=[] if claim is None else [row("verification", claim_id=claim, at=NOW)],
    )
    with pytest.raises(PacketError, match="^missing_confirmation$"):
        card(data)


def test_input_and_returned_cards_do_not_mutate_the_renderer():
    data = rows()
    original = deepcopy(data)
    held = renderer(data)
    expected = held.render_item("subject")
    data.items[0]["title"] = "Changed later"
    data.sources[0]["content"] = "Changed source"
    changed = held.render_item("subject")
    changed.card["properties"]["priority"] = "Changed output"
    assert held.render_item("subject") == expected
    assert original.items[0]["title"] == "subject"


def test_incomplete_rows_and_cross_world_references_refuse():
    with pytest.raises(PacketError, match="^incomplete_packet_inputs$"):
        renderer(complete=False)
    data = rows(items=[item(world_id="other-world")])
    with pytest.raises(PacketError, match="^invalid_packet_world$"):
        renderer(data)
    with pytest.raises(PacketError, match="^missing_link_endpoint$"):
        renderer(rows(links=[link(end="hidden")]))


def test_naive_time_duplicate_id_and_invalid_properties_refuse():
    with pytest.raises(PacketError, match="^invalid_packet_time$"):
        renderer(rows(items=[item(became_true_earliest=NOW.replace(tzinfo=None))]))
    with pytest.raises(PacketError, match="^duplicate_packet_reference$"):
        renderer(rows(items=[item(), item()]))
    with pytest.raises(PacketError, match="^invalid_packet_value$"):
        renderer(rows(items=[item(properties='{"x":')]))


def test_oversized_required_card_refuses_instead_of_silently_truncating():
    with pytest.raises(PacketError, match="^item_card_too_large$"):
        card(rows(items=[item(summary="x" * MAX_CARD_BYTES)]))


def test_continuation_uses_preview_fields_and_refuses_ineligible_or_unrelated_links():
    data = rows(
        items=[item(), item("other"), item("unrelated")],
        links=[link(f"link-{index}") for index in range(8)] + [link("ended", active_key=None)],
    )
    held = renderer(data)
    result = held.render_item("subject")
    assert held.render_link("subject", "link-0") == result.card["links"][0]
    assert held.render_link("subject", "link-7")["id"] == "link-7"
    assert "link-7" in result.link_ids
    assert "link-7" not in [value["id"] for value in result.card["links"]]
    for item_id, link_id in [("subject", "ended"), ("unrelated", "link-0"), ("subject", "hidden")]:
        with pytest.raises(PacketError, match="^link_not_current$"):
            held.render_link(item_id, link_id)


def test_json_text_and_detached_mapping_properties_render_identically():
    assert card(rows(items=[item(properties='{"priority":"high"}')])) == card(
        rows(items=[item(properties={"priority": "high"})])
    )


def test_many_meanings_remain_bounded_and_all_have_exact_counts():
    meanings = [row(f"meaning-{index:04}", name=f"meaning {index}") for index in range(5000)]
    links = [link(value["id"], meaning=value["id"]) for value in meanings]
    # Complete per-meaning metadata can itself exceed the byte cap. Never return a prefix
    # with omitted meanings, or label that prefix as complete metadata.
    with pytest.raises(PacketError, match="^item_card_too_large$"):
        card(rows(items=[item(), item("other")], meanings=meanings, links=links))


def test_missing_mark_path_and_belief_episode_fail_closed():
    with pytest.raises(PacketError, match="^missing_mark_path$"):
        card(marked(path=["hidden"]), mark_explanations=[MarkExplanation("link", "Prepared", True)])
    data = rows(
        items=[item(), item("belief", kind=KIND_BELIEF)],
        links=[link(start="belief", end="subject", meaning=MEANING_DESCRIBES)],
        beliefs=[
            {
                "world_id": WORLD,
                "item_id": "belief",
                "state": "tentative",
                "supporting_episodes": ["hidden"],
            }
        ],
    )
    with pytest.raises(PacketError, match="^missing_belief_episode$"):
        card(data)


def test_rendering_never_calls_store_clock_or_live_excerpt(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure packet rendering crossed a live boundary")

    monkeypatch.setattr("grove.store.query.WorldQuery.read", forbidden)
    monkeypatch.setattr("grove.models.verify_judge.excerpt", forbidden)
    monkeypatch.setattr("grove.clock.now", forbidden)
    assert card()["newest_evidence"]["excerpt"] == "Current proof"
