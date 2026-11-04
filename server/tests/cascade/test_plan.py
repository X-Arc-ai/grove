"""Synthetic D6 P0 controls, including actual red runs with each rule disabled."""

import copy
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from grove import cascade

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
THEN = NOW - timedelta(days=1)


def item(name, role="active", **fields):
    return {"id": name, "kind": "kind-a", "status_role": role, **fields}


def link(name, source, target, meaning="out", active=True):
    return {
        "id": name,
        "from_item": source,
        "to_item": target,
        "meaning": meaning,
        "active_key": name if active else None,
    }


def snapshot(items=None, links=None):
    return {
        "items": items or {"a": item("a", "done"), "b": item("b", "blocked")},
        "links": [link("ab", "a", "b")] if links is None else links,
        "meanings": {
            "out": {
                "id": "out",
                "name": "consequence",
                "carries_consequences": True,
                "downstream_end": "to",
                "status": "canonical",
            },
            "in": {
                "id": "in",
                "name": "dependency",
                "carries_consequences": True,
                "downstream_end": "from",
                "status": "canonical",
            },
            "history": {
                "id": "history",
                "name": "lineage",
                "carries_consequences": False,
                "status": "canonical",
            },
            "parts": {
                "id": "parts",
                "name": "composition",
                "carries_consequences": False,
                "decomposition": True,
                "rolls_up_completion": True,
                "status": "canonical",
            },
        },
        "kinds": {"kind-a": {"name": "work", "propagating_fields": ["deadline"]}},
        "now": NOW,
        "latest_change": {},
        "prior_status": {"b": {"status": "in progress", "role": "active"}},
        "r1_endings": [],
    }


def change(root="a", **fields):
    return {
        "item_id": root,
        "change_set_id": f"change-{root}",
        "world_time": THEN,
        "source_type": "confirmed",
        "terminal": True,
        "reopened": False,
        "person_correction": False,
        "fields": [],
        **fields,
    }


def actions(result, rule):
    return [row for row in result.proposals if row["rule"] == rule]


def test_r1_positive():
    result = cascade.plan(snapshot(), [change()])
    assert result.endings == (
        {
            "link_id": "ab",
            "trigger_id": "a",
            "change_set_id": "change-a",
            "world_time": THEN,
        },
    )


def test_r2_positive():
    result = cascade.plan(snapshot(), [change(terminal=False)])
    assert [(m["affected_item"], m["depth"], m["path"]) for m in result.marks] == [("b", 1, ["ab"])]
    assert not result.endings


def test_r4_positive():
    result = cascade.plan(snapshot(), [change()])
    (proposal,) = actions(result, "R4")
    assert proposal["target_item"] == "b"
    assert proposal["action"] == "resume"
    assert proposal["suggested_status"] == "in progress"
    assert proposal["link_ids"] == ["ab"]
    assert proposal["mark_targets"] == ["b"]


@pytest.mark.parametrize("rule", ["R1", "R2", "R4"])
def test_each_rule_positive_really_fails_when_disabled(rule):
    """CI runs the actual positive test with its production rule removed, and requires a fail."""
    test = f"tests/cascade/test_plan.py::test_{rule.lower()}_positive"
    code = (
        "import pytest; from grove import cascade; "
        f"cascade.RULES = cascade.RULES - {{{rule!r}}}; "
        f"raise SystemExit(pytest.main([{test!r}, '-q', '--tb=short']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed" in result.stdout, result.stdout + result.stderr
    assert "ERROR" not in result.stdout, result.stdout + result.stderr


def test_nonterminal_role_change_does_not_end_or_propose_followup():
    result = cascade.plan(snapshot(), [change(terminal=False)])
    assert len(result.marks) == 1
    assert not result.endings
    assert not actions(result, "R1")
    assert not actions(result, "R4")


@pytest.mark.parametrize("source_type", ["planned", "said", "reported_done"])
def test_source_type_gate_stores_marks_and_proposals(source_type):
    result = cascade.plan(snapshot(), [change(source_type=source_type)])
    assert not result.endings
    assert {m["affected_item"] for m in result.marks} == {"b"}
    assert result.marks[0]["source_type"] == source_type
    assert len(actions(result, "R1")) == 1
    assert len(actions(result, "R4")) == 1
    assert all(p["source_type"] == source_type for p in result.proposals)


def test_reported_done_role_is_proposed_terminal_even_without_terminal_flag():
    state = snapshot()
    state["items"]["a"]["status_role"] = "reported_done"
    result = cascade.plan(state, [change(terminal=False, source_type="reported_done")])
    assert not result.endings
    assert {m["affected_item"] for m in result.marks} == {"b"}
    assert len(actions(result, "R1")) == 1
    assert len(actions(result, "R4")) == 1


def test_persons_correction_can_end_from_said_evidence():
    result = cascade.plan(snapshot(), [change(source_type="said", person_correction=True)])
    assert [end["link_id"] for end in result.endings] == ["ab"]


def test_future_end_never_applies_even_with_confirming_source():
    future = NOW + timedelta(days=30)
    result = cascade.plan(snapshot(), [change(world_time=future)])
    assert not result.endings
    assert result.marks[0]["world_time"] == future
    assert actions(result, "R1")[0]["world_time"] == future


def test_unknown_time_stays_unknown_and_proposes_only():
    state = snapshot()
    state["latest_change"]["b"] = NOW
    result = cascade.plan(state, [change(world_time=None, end_clock="unknown")])
    assert not result.endings
    assert result.marks[0]["world_time"] is None
    assert result.marks[0]["state"] == "open"
    assert actions(result, "R1")[0]["end_clock"] == "unknown"


def test_bounded_end_clock_is_preserved_without_aliasing_input():
    end_clock = {"earliest": THEN.isoformat(), "latest": NOW.isoformat(), "precision": "day"}
    trigger = change(end_clock=end_clock)
    result = cascade.plan(snapshot(), [trigger])
    assert result.endings[0]["end_clock"] == end_clock
    result.endings[0]["end_clock"]["precision"] = "month"
    assert trigger["end_clock"]["precision"] == "day"


def test_r1_ends_both_endpoints_but_not_history_or_inactive_links():
    state = snapshot(
        {name: item(name) for name in "abcd"},
        [
            link("ab", "a", "b"),
            link("ca", "c", "a"),
            link("history", "a", "d", "history"),
            link("inactive", "a", "d", active=False),
            link("part", "a", "d", "parts"),
        ],
    )
    result = cascade.plan(state, [change()])
    assert {end["link_id"] for end in result.endings} == {"ab", "ca"}
    assert {m["affected_item"] for m in result.marks} == {"b"}
    assert {p["target_item"] for p in actions(result, "R4")} == {"b"}


def test_downstream_end_from_follows_declared_direction():
    state = snapshot(links=[link("ba", "b", "a", "in")])
    result = cascade.plan(state, [change()])
    assert [m["affected_item"] for m in result.marks] == ["b"]
    assert [p["target_item"] for p in actions(result, "R4")] == ["b"]
    reverse = cascade.plan(state, [change("b")])
    assert not reverse.marks
    assert not actions(reverse, "R4")
    assert len(reverse.endings) == 1


def test_history_never_carries_marks_even_with_matching_prose():
    state = snapshot(links=[link("ab", "a", "b", "history")])
    state["items"]["a"]["body"] = "the other item depends on this item"
    result = cascade.plan(state, [change()])
    assert result == cascade.CascadePlan()


def test_provisional_meaning_produces_only_proposals():
    state = snapshot()
    state["meanings"]["out"]["status"] = "provisional"
    result = cascade.plan(state, [change()])
    assert not result.endings
    assert not result.marks
    assert {p["rule"] for p in result.proposals} == {"R1", "R2", "R4"}
    assert actions(result, "R2")[0]["action"] == "mark"


def test_provisional_path_taints_descendants_but_not_an_alternative_canonical_path():
    state = snapshot(
        {name: item(name) for name in "abcde"},
        [
            link("ab", "a", "b", "temporary"),
            link("bc", "b", "c"),
            link("ad", "a", "d"),
            link("dc", "d", "c"),
            link("ce", "c", "e"),
        ],
    )
    state["meanings"]["temporary"] = {**state["meanings"]["out"], "status": "provisional"}
    result = cascade.plan(state, [change(terminal=False)])
    assert {m["affected_item"]: m["path"] for m in result.marks} == {
        "c": ["ad", "dc"],
        "d": ["ad"],
        "e": ["ad", "dc", "ce"],
    }
    assert [p["target_item"] for p in actions(result, "R2")] == ["b"]


def test_short_provisional_path_does_not_suppress_longer_canonical_path():
    state = snapshot(
        {name: item(name) for name in "abcd"},
        [
            link("ab", "a", "b", "temporary"),
            link("ac", "a", "c"),
            link("cd", "c", "d"),
            link("db", "d", "b"),
        ],
    )
    state["meanings"]["temporary"] = {**state["meanings"]["out"], "status": "provisional"}
    result = cascade.plan(state, [change(terminal=False)])
    assert {m["affected_item"]: m["path"] for m in result.marks}["b"] == ["ac", "cd", "db"]
    assert not actions(result, "R2")


def test_cycle_and_depth_limit_keep_exact_reach():
    state = snapshot(
        {name: item(name) for name in "abcde"},
        [
            link("ab", "a", "b"),
            link("bc", "b", "c"),
            link("ca", "c", "a"),
            link("cd", "c", "d"),
            link("de", "d", "e"),
        ],
    )
    result = cascade.plan(state, [change(terminal=False)])
    assert {m["affected_item"]: m["depth"] for m in result.marks} == {"b": 1, "c": 2, "d": 3}


def test_planted_thirty_item_graph_ten_changes_has_exact_precision_and_recall():
    items = {f"{letter}{n}": item(f"{letter}{n}") for n in range(10) for letter in "abc"}
    links = []
    expected = set()
    for n in range(10):
        links.extend(
            [
                link(f"ab{n}", f"a{n}", f"b{n}"),
                link(f"cb{n}", f"c{n}", f"b{n}", "in"),
                link(f"history{n}", f"c{n}", f"a{(n + 1) % 10}", "history"),
            ]
        )
        expected.update({(f"change-a{n}", f"b{n}"), (f"change-a{n}", f"c{n}")})
    result = cascade.plan(snapshot(items, links), [change(f"a{n}") for n in range(10)])
    actual = {(m["trigger_change_set"], m["affected_item"]) for m in result.marks}
    assert actual == expected
    assert len(actual & expected) / len(actual) == 1.0
    assert len(actual & expected) / len(expected) == 1.0
    assert all(not any(path.startswith("history") for path in m["path"]) for m in result.marks)


def test_wide_impact_keeps_hop_one_and_stores_one_counted_proposal():
    items = {"a": item("a"), "b": item("b")}
    items.update({f"c{n}": item(f"c{n}", kind="kind-b") for n in range(101)})
    links = [link("ab", "a", "b"), *[link(f"bc{n}", "b", f"c{n}") for n in range(101)]]
    result = cascade.plan(snapshot(items, links), [change(terminal=False)])
    assert [m["affected_item"] for m in result.marks] == ["b"]
    (wide,) = [p for p in result.proposals if p["proposal_type"] == "wide_impact"]
    assert wide["detail"] == {
        "reached": 102,
        "kept": 1,
        "omitted": 101,
        "counts_by_kind": {"kind-a": 1, "kind-b": 101},
    }


def test_wide_direct_fanout_retains_all_direct_marks():
    items = {"a": item("a"), **{f"b{n}": item(f"b{n}") for n in range(101)}}
    result = cascade.plan(
        snapshot(items, [link(f"ab{n}", "a", f"b{n}") for n in range(101)]),
        [change(terminal=False)],
    )
    assert len(result.marks) == 101
    assert len([p for p in result.proposals if p["proposal_type"] == "wide_impact"]) == 1


def test_exactly_one_hundred_reached_items_does_not_trigger_wide_impact():
    items = {"a": item("a"), **{f"b{n}": item(f"b{n}") for n in range(100)}}
    result = cascade.plan(
        snapshot(items, [link(f"ab{n}", "a", f"b{n}") for n in range(100)]),
        [change(terminal=False)],
    )
    assert len(result.marks) == 100
    assert not result.proposals


def test_backfilled_trigger_is_born_resolved_after_dependents_newer_change():
    state = snapshot()
    state["latest_change"]["b"] = NOW - timedelta(days=10)
    result = cascade.plan(state, [change(world_time=NOW - timedelta(days=90))])
    assert len(result.endings) == 1
    assert result.endings[0]["world_time"] == NOW - timedelta(days=90)
    assert result.marks[0]["state"] == "born_resolved"
    same = cascade.plan(state, [change(world_time=state["latest_change"]["b"])])
    assert same.marks[0]["state"] == "open"


def test_r4_uses_review_when_another_upstream_remains():
    state = snapshot(
        {"a": item("a"), "b": item("b", "blocked"), "c": item("c")},
        [link("ab", "a", "b"), link("cb", "c", "b")],
    )
    result = cascade.plan(state, [change()])
    (proposal,) = actions(result, "R4")
    assert proposal["action"] == "review"
    assert "suggested_status" not in proposal


@pytest.mark.parametrize(
    "previous", [{}, {"status": "done", "role": "done"}, {"status": "blocked", "role": "blocked"}]
)
def test_r4_does_not_invent_or_use_terminal_previous_status(previous):
    state = snapshot()
    state["prior_status"]["b"] = previous
    (proposal,) = actions(cascade.plan(state, [change()]), "R4")
    assert proposal["action"] == "review"
    assert "suggested_status" not in proposal


def test_r4_target_terminal_does_not_get_followup():
    state = snapshot()
    state["items"]["b"]["status_role"] = "done"
    assert not actions(cascade.plan(state, [change()]), "R4")


def test_r4_combines_lost_links_and_resumes_after_two_confirmed_upstreams_end():
    state = snapshot(
        {"a": item("a"), "b": item("b", "blocked"), "c": item("c")},
        [link("ab", "a", "b"), link("cb", "c", "b"), link("ab2", "a", "b", "in")],
    )
    result = cascade.plan(state, [change(), change("c")])
    assert {p["action"] for p in actions(result, "R4")} == {"resume"}


def test_reopen_only_proposes_prior_r1_endings_and_never_restores_them():
    state = snapshot(links=[link("ab", "a", "b", active=False)])
    state["items"]["a"]["status_role"] = "open"
    state["r1_endings"] = [{"link_id": "ab", "trigger_id": "a"}]
    before = copy.deepcopy(state)
    result = cascade.plan(state, [change(terminal=False, reopened=True)])
    assert not result.endings
    assert not result.marks
    (proposal,) = result.proposals
    assert proposal["proposal_type"] == "reopen"
    assert proposal["action"] == "reopen_links"
    assert proposal["link_ids"] == ["ab"]
    assert state == before
    state["r1_endings"][0]["trigger_id"] = "b"
    assert not cascade.plan(state, [change(terminal=False, reopened=True)]).proposals


def test_plan_is_pure_and_deterministic_under_input_order():
    state = snapshot(
        {name: item(name) for name in "abcd"},
        [link("ab", "a", "b"), link("ac", "a", "c"), link("bd", "b", "d"), link("cd", "c", "d")],
    )
    changes = [change(), change("c", terminal=False)]
    before = copy.deepcopy((state, changes))
    first = cascade.plan(state, changes)
    assert (state, changes) == before
    state["links"].reverse()
    state["items"] = dict(reversed(list(state["items"].items())))
    assert cascade.plan(state, reversed(changes)) == first


def test_duplicate_triggers_and_batch_roots_keep_one_mark_per_change_set_and_item():
    state = snapshot(
        {name: item(name) for name in "abc"},
        [link("ab", "a", "b"), link("cb", "c", "b")],
    )
    a = change(change_set_id="one-batch")
    c = change("c", change_set_id="one-batch")
    result = cascade.plan(state, [a, c, a])
    assert len(result.marks) == 1
    assert len({(p["trigger_id"], p["action"], p["target_item"]) for p in result.proposals}) == len(
        result.proposals
    )
    assert len(result.endings) == 2


def test_names_cannot_change_the_plan():
    state = snapshot()
    expected = cascade.plan(state, [change()])
    for row in [*state["kinds"].values(), *state["meanings"].values()]:
        row["name"] = "renamed invented word"
        row["description"] = "A changed title and description must not steer the graph."
    for row in state["items"].values():
        row["title"] = "renamed invented item"
    assert cascade.plan(state, [change()]) == expected


def test_rule_registry_rejects_a_consequence_meaning_without_direction():
    state = snapshot()
    del state["meanings"]["out"]["downstream_end"]
    with pytest.raises(ValueError, match="downstream end"):
        cascade.plan(state, [change()])


def test_unknown_meaning_and_missing_consequence_endpoint_refuse():
    state = snapshot()
    state["links"][0]["meaning"] = "absent"
    with pytest.raises(ValueError, match="absent meaning"):
        cascade.plan(state, [change()])
    state = snapshot()
    del state["items"]["b"]
    with pytest.raises(ValueError, match="endpoint is absent"):
        cascade.plan(state, [change()])


def test_missing_trigger_and_bad_time_refuse():
    with pytest.raises(ValueError, match="trigger is absent"):
        cascade.plan(snapshot(), [change("missing")])
    with pytest.raises(ValueError, match="aware datetime"):
        cascade.plan(snapshot(), [change(world_time=datetime(2026, 11, 4))])


def test_no_trigger_has_no_effect():
    assert cascade.plan(snapshot(), []) == cascade.CascadePlan()
