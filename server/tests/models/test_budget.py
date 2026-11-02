"""The model-call writes and the meter against the live dev store (D15, S20, S37).

Every call is a model_calls row, and its cost moves the world's day budget in the same
transaction. Once the day's ceiling is reached, a deferrable job waits for the next midnight UTC
and a job marked continue runs on.
"""

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from grove import clock
from grove.clock import TestClock
from grove.ids import new_id
from grove.models.config import ModelSettings, load_catalog
from grove.models.errors import BudgetReached, RunCapReached
from grove.models.meter import Gate, Meter, resets_at
from grove.store.query import WorldQuery
from grove.writer.model_calls import (
    BudgetState,
    ModelCall,
    ModelCallRefused,
    attach_receipt,
    record_model_call,
    set_budget_ceiling,
)

NOON = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
NINE = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
DAY = date(2026, 11, 2)
NEXT_DAY = date(2026, 11, 3)
RESETS_AT = "2026-11-03T00:00:00+00:00"
SPARK = "muse-spark-1.3"
GLIMMER = "muse-glimmer-30b"
CATALOG = load_catalog()
DEFER_JOBS = (
    "backfill_triage",
    "backfill_extraction",
    "update_pass",
    "verification",
    "second_opinion",
    "consolidation",
    "explain_break",
    "cleanup_pass",
    "consolidation_pass",
    "smoke",
)
CONTINUE_JOBS = ("triage", "triage_fallback", "extraction", "transcription")
THREADS = 12


@pytest.fixture(autouse=True)
def frozen_clock():
    frozen = TestClock()
    frozen.set(NOON)
    with clock.using(frozen):
        yield frozen


def settings(daily_budget: float | None) -> ModelSettings:
    return ModelSettings(
        meta_key="test-meta-key",
        glimmer_base_url="https://glimmer.example.test/v1",
        glimmer_key="test-glimmer-key",
        daily_budget=daily_budget,
        gateway_secret=b"s" * 32,
    )


def meter(driver, daily_budget: float | None) -> Meter:
    return Meter(driver, CATALOG, settings(daily_budget))


def a_call(**changes) -> ModelCall:
    base = ModelCall(
        job="extraction", model=SPARK, route="meta", outcome="ok", at=NINE, effort="minimal"
    )
    return replace(base, **changes)


def budgets(driver, world: str) -> list[dict]:
    rows = WorldQuery(driver, world).read("MATCH (b:Budget) RETURN b ORDER BY b.day")
    return [row["b"] for row in rows]


def model_calls(driver, world: str) -> list[dict]:
    rows = WorldQuery(driver, world).read("MATCH (c:ModelCall) RETURN c ORDER BY c.at, c.id")
    return [row["c"] for row in rows]


def receipt_of(driver, world: str, call_id: str):
    rows = WorldQuery(driver, world).read(
        "MATCH (c:ModelCall {id: $id}) RETURN c.receipt_reference AS receipt", id=call_id
    )
    assert len(rows) == 1
    return rows[0]["receipt"]


def budget_row(world: str, day: date, spent: float, state: str, ceiling: float | None) -> dict:
    """A :Budget row as the store holds it; a ceiling of None is no property at all."""
    row = {"world_id": world, "day": day, "spent": spent, "state": state}
    if ceiling is not None:
        row["ceiling"] = ceiling
    return row


# record_model_call: the row


def test_record_writes_every_given_property(driver, world):
    # 01:30 at +02:00 is 23:30 UTC the day before: the row holds UTC, and the day is the UTC date.
    at = datetime(2026, 11, 3, 1, 30, 15, 250000, tzinfo=timezone(timedelta(hours=2)))
    given = ModelCall(
        job="consolidation",
        model=SPARK,
        route="meta",
        outcome="ok",
        at=at,
        effort="high",
        run_id="run-orchard-1",
        input_tokens=1200,
        cached_tokens=800,
        output_tokens=640,
        reasoning_tokens=512,
        cost=0.0042,
        audio_seconds=12.5,
        latency_ms=1834,
    )

    call_id, budget = record_model_call(driver, world, given, default_ceiling=5.0)

    stored = model_calls(driver, world)
    assert stored == [
        {
            "id": call_id,
            "world_id": world,
            "job": "consolidation",
            "model": SPARK,
            "route": "meta",
            "outcome": "ok",
            "effort": "high",
            "run_id": "run-orchard-1",
            "input_tokens": 1200,
            "cached_tokens": 800,
            "output_tokens": 640,
            "reasoning_tokens": 512,
            "cost": 0.0042,
            "audio_seconds": 12.5,
            "latency_ms": 1834,
            "at": datetime(2026, 11, 2, 23, 30, 15, 250000, tzinfo=UTC),
            "day": DAY,
        }
    ]
    assert stored[0]["at"].utcoffset() == timedelta(0)
    assert budget == BudgetState(spent=0.0042, ceiling=5.0, state="open")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.0042, "open", 5.0)]


def test_record_leaves_out_what_it_was_not_given(driver, world):
    bare = ModelCall(job="triage", model=GLIMMER, route="modal", outcome="provider_error", at=NINE)

    call_id, budget = record_model_call(driver, world, bare, default_ceiling=5.0)

    assert model_calls(driver, world) == [
        {
            "id": call_id,
            "world_id": world,
            "job": "triage",
            "model": GLIMMER,
            "route": "modal",
            "outcome": "provider_error",
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cost": 0.0,
            "at": NINE,
            "day": DAY,
        }
    ]
    # A call that cost nothing still opens the day's budget row.
    assert budget == BudgetState(spent=0.0, ceiling=5.0, state="open")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.0, "open", 5.0)]


def test_record_returns_distinct_ids_one_row_each(driver, world):
    first, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)
    second, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)

    assert first != second
    assert sorted(row["id"] for row in model_calls(driver, world)) == sorted((first, second))


# record_model_call: the day budget


def test_second_call_adds_its_cost(driver, world):
    _, first = record_model_call(driver, world, a_call(cost=0.25), default_ceiling=5.0)
    _, second = record_model_call(driver, world, a_call(cost=0.5), default_ceiling=5.0)

    assert first == BudgetState(spent=0.25, ceiling=5.0, state="open")
    assert second == BudgetState(spent=0.75, ceiling=5.0, state="open")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.75, "open", 5.0)]


def test_call_that_reaches_the_ceiling_turns_the_state_in_its_own_transaction(driver, world):
    states = [
        record_model_call(driver, world, a_call(cost=cost), default_ceiling=1.0)[1]
        for cost in (0.25, 0.5, 0.25, 0.125)
    ]

    assert states == [
        BudgetState(spent=0.25, ceiling=1.0, state="open"),
        BudgetState(spent=0.75, ceiling=1.0, state="open"),
        # spent equal to the ceiling is reached, not only spent above it.
        BudgetState(spent=1.0, ceiling=1.0, state="reached"),
        # The writer still records a call after the ceiling: the gate is the meter's job.
        BudgetState(spent=1.125, ceiling=1.0, state="reached"),
    ]
    assert budgets(driver, world) == [budget_row(world, DAY, 1.125, "reached", 1.0)]
    assert len(model_calls(driver, world)) == 4


def test_default_ceiling_applies_only_when_the_day_row_is_new(driver, world):
    record_model_call(driver, world, a_call(cost=0.25), default_ceiling=5.0)
    _, later = record_model_call(driver, world, a_call(cost=0.25), default_ceiling=0.1)

    assert later == BudgetState(spent=0.5, ceiling=5.0, state="open")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.5, "open", 5.0)]


def test_no_default_ceiling_gives_a_budget_that_never_reaches(driver, world):
    _, first = record_model_call(driver, world, a_call(cost=1000.0), default_ceiling=None)
    _, second = record_model_call(driver, world, a_call(cost=1000.0), default_ceiling=None)

    assert first == BudgetState(spent=1000.0, ceiling=None, state="open")
    assert second == BudgetState(spent=2000.0, ceiling=None, state="open")
    assert budgets(driver, world) == [budget_row(world, DAY, 2000.0, "open", None)]


def test_removing_the_ceiling_reopens_the_day(driver, world):
    _, reached = record_model_call(driver, world, a_call(cost=1.0), default_ceiling=1.0)
    assert reached.state == "reached"

    reopened = set_budget_ceiling(driver, world, DAY, None)

    assert reopened == BudgetState(spent=1.0, ceiling=None, state="open")
    assert budgets(driver, world) == [budget_row(world, DAY, 1.0, "open", None)]
    # The operator's removal holds: a later call's default ceiling does not come back.
    _, after = record_model_call(driver, world, a_call(cost=50.0), default_ceiling=1.0)
    assert after == BudgetState(spent=51.0, ceiling=None, state="open")


def test_a_ceiling_under_spent_turns_the_day_reached(driver, world):
    record_model_call(driver, world, a_call(cost=0.75), default_ceiling=5.0)

    lowered = set_budget_ceiling(driver, world, DAY, 0.5)
    assert lowered == BudgetState(spent=0.75, ceiling=0.5, state="reached")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.75, "reached", 0.5)]

    raised = set_budget_ceiling(driver, world, DAY, 2.0)
    assert raised == BudgetState(spent=0.75, ceiling=2.0, state="open")

    equal = set_budget_ceiling(driver, world, DAY, 0.75)
    assert equal == BudgetState(spent=0.75, ceiling=0.75, state="reached")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.75, "reached", 0.75)]


def test_a_ceiling_set_before_any_call_holds_for_the_day(driver, world):
    ahead = set_budget_ceiling(driver, world, NEXT_DAY, 3.0)
    assert ahead == BudgetState(spent=0.0, ceiling=3.0, state="open")
    assert budgets(driver, world) == [budget_row(world, NEXT_DAY, 0.0, "open", 3.0)]

    at = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    _, budget = record_model_call(driver, world, a_call(at=at, cost=0.5), default_ceiling=5.0)

    assert budget == BudgetState(spent=0.5, ceiling=3.0, state="open")
    assert budgets(driver, world) == [budget_row(world, NEXT_DAY, 0.5, "open", 3.0)]


@pytest.mark.parametrize(
    "ceiling, message",
    [
        pytest.param(-0.5, "ceiling must be finite and not negative", id="negative"),
        pytest.param(math.nan, "ceiling must be finite and not negative", id="nan"),
        pytest.param(math.inf, "ceiling must be finite and not negative", id="infinite"),
        pytest.param(True, "ceiling must be a number", id="bool"),
        pytest.param("5", "ceiling must be a number", id="text"),
    ],
)
def test_set_budget_ceiling_refuses_a_bad_ceiling(driver, world, ceiling, message):
    with pytest.raises(ModelCallRefused, match=re.escape(message)):
        set_budget_ceiling(driver, world, DAY, ceiling)

    assert budgets(driver, world) == []


def test_the_day_comes_from_the_utc_date_of_at(driver, world):
    late = datetime(2026, 11, 2, 23, 59, 59, 999999, tzinfo=UTC)
    midnight = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    # 20:30 at -05:00 is 01:30 UTC the next day.
    west = datetime(2026, 11, 2, 20, 30, tzinfo=timezone(timedelta(hours=-5)))

    record_model_call(driver, world, a_call(at=late, cost=0.25), default_ceiling=5.0)
    record_model_call(driver, world, a_call(at=midnight, cost=0.5), default_ceiling=5.0)
    record_model_call(driver, world, a_call(at=west, cost=0.125), default_ceiling=5.0)

    assert budgets(driver, world) == [
        budget_row(world, DAY, 0.25, "open", 5.0),
        budget_row(world, NEXT_DAY, 0.625, "open", 5.0),
    ]
    assert [(row["at"], row["day"]) for row in model_calls(driver, world)] == [
        (late, DAY),
        (midnight, NEXT_DAY),
        (datetime(2026, 11, 3, 1, 30, tzinfo=UTC), NEXT_DAY),
    ]


def test_concurrent_calls_share_one_budget_row(driver, world):
    costs = [0.0125 * (n + 1) + 0.0001 * n for n in range(THREADS)]
    assert len(set(costs)) == THREADS
    start = threading.Barrier(THREADS)

    def one(cost: float) -> tuple[str, BudgetState]:
        start.wait(timeout=30)
        return record_model_call(driver, world, a_call(cost=cost), default_ceiling=0.5)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(one, costs))

    rows = budgets(driver, world)
    assert len(rows) == 1
    assert rows[0]["day"] == DAY
    assert rows[0]["ceiling"] == 0.5
    assert rows[0]["state"] == "reached"
    assert math.isclose(rows[0]["spent"], math.fsum(costs), rel_tol=0, abs_tol=1e-9)

    stored = model_calls(driver, world)
    assert len(stored) == THREADS
    assert sorted(row["cost"] for row in stored) == sorted(costs)
    assert sorted(row["id"] for row in stored) == sorted(call_id for call_id, _ in results)

    # Each commit saw its own running total, so no two calls read the same spent.
    seen = sorted(budget.spent for _, budget in results)
    steps = [seen[0]] + [after - before for before, after in zip(seen, seen[1:], strict=False)]
    assert all(
        math.isclose(step, cost, rel_tol=0, abs_tol=1e-9)
        for step, cost in zip(sorted(steps), sorted(costs), strict=True)
    )
    assert math.isclose(seen[-1], rows[0]["spent"], rel_tol=0, abs_tol=1e-9)


def test_world_isolation_of_the_budget(driver, make_world):
    full, other, fresh = make_world(), make_world(), make_world()
    gauge = meter(driver, 0.5)
    verification = CATALOG.job("verification")

    _, full_budget = gauge.record(full, a_call(cost=0.5))
    _, other_budget = gauge.record(other, a_call(cost=0.125))

    assert full_budget == BudgetState(spent=0.5, ceiling=0.5, state="reached")
    assert other_budget == BudgetState(spent=0.125, ceiling=0.5, state="open")
    with pytest.raises(BudgetReached):
        gauge.gate(full, verification)
    assert gauge.gate(other, verification) == Gate(
        day=DAY, budget=BudgetState(spent=0.125, ceiling=0.5, state="open")
    )
    assert gauge.gate(fresh, verification) == Gate(
        day=DAY, budget=BudgetState(spent=0.0, ceiling=0.5, state="open")
    )
    assert budgets(driver, other) == [budget_row(other, DAY, 0.125, "open", 0.5)]
    assert budgets(driver, fresh) == []


# record_model_call: refusals


REFUSALS = [
    pytest.param({"input_tokens": -1}, "input_tokens must be a non-negative integer", id="neg-in"),
    pytest.param(
        {"cached_tokens": -1}, "cached_tokens must be a non-negative integer", id="neg-ca"
    ),
    pytest.param(
        {"output_tokens": -1}, "output_tokens must be a non-negative integer", id="neg-out"
    ),
    pytest.param(
        {"reasoning_tokens": -1}, "reasoning_tokens must be a non-negative integer", id="neg-re"
    ),
    pytest.param({"input_tokens": True}, "input_tokens must be a non-negative integer", id="bool"),
    pytest.param(
        {"output_tokens": 2.0}, "output_tokens must be a non-negative integer", id="float"
    ),
    pytest.param(
        {"input_tokens": 10, "cached_tokens": 11},
        "cached_tokens cannot exceed input_tokens",
        id="cached-over-input",
    ),
    pytest.param(
        {"output_tokens": 10, "reasoning_tokens": 11},
        "reasoning_tokens cannot exceed output_tokens",
        id="reasoning-over-output",
    ),
    pytest.param({"outcome": "maybe"}, "outcome is not a known outcome", id="unknown-outcome"),
    pytest.param({"outcome": "OK"}, "outcome is not a known outcome", id="outcome-case"),
    pytest.param({"route": "elsewhere"}, "route is not a known route", id="unknown-route"),
    pytest.param({"effort": "extreme"}, "effort is not in the effort enum", id="unknown-effort"),
    pytest.param({"effort": "Low"}, "effort is not in the effort enum", id="effort-case"),
    pytest.param(
        {"at": datetime(2026, 11, 2, 9, 0)},
        "at must be an aware datetime from the clock provider",
        id="naive-at",
    ),
    pytest.param(
        {"at": date(2026, 11, 2)},
        "at must be an aware datetime from the clock provider",
        id="date-at",
    ),
    pytest.param({"cost": math.nan}, "cost must be finite and not negative", id="nan-cost"),
    pytest.param({"cost": -0.01}, "cost must be finite and not negative", id="negative-cost"),
    pytest.param({"cost": math.inf}, "cost must be finite and not negative", id="infinite-cost"),
    pytest.param({"cost": True}, "cost must be a number", id="bool-cost"),
    pytest.param({"cost": "0.1"}, "cost must be a number", id="text-cost"),
    pytest.param(
        {"audio_seconds": -1.0}, "audio_seconds must be finite and not negative", id="neg-audio"
    ),
    pytest.param({"latency_ms": -1}, "latency_ms must be a non-negative integer", id="neg-latency"),
    pytest.param({"job": ""}, "job must be a short ASCII code", id="empty-job"),
    pytest.param({"model": "m" * 81}, "model must be a short ASCII code", id="long-model"),
    pytest.param({"run_id": "lauf-é"}, "run_id must be a short ASCII code", id="non-ascii-run"),
]


@pytest.mark.parametrize("change, message", REFUSALS)
def test_record_refuses_a_bad_row_and_writes_nothing(driver, world, change, message):
    with pytest.raises(ModelCallRefused, match=re.escape(message)):
        record_model_call(driver, world, a_call(**{"cost": 0.25, **change}), default_ceiling=5.0)

    assert model_calls(driver, world) == []
    assert budgets(driver, world) == []


@pytest.mark.parametrize(
    "default_ceiling", [pytest.param(-1.0, id="negative"), pytest.param(math.nan, id="nan")]
)
def test_record_refuses_a_bad_default_ceiling(driver, world, default_ceiling):
    with pytest.raises(
        ModelCallRefused, match=re.escape("default_ceiling must be finite and not negative")
    ):
        record_model_call(driver, world, a_call(cost=0.25), default_ceiling=default_ceiling)

    assert model_calls(driver, world) == []
    assert budgets(driver, world) == []


def test_a_refusal_message_never_holds_the_value(driver, world):
    with pytest.raises(ModelCallRefused) as caught:
        record_model_call(driver, world, a_call(job="Lio Varn é"), default_ceiling=5.0)

    assert "Lio Varn" not in str(caught.value)


# attach_receipt


def test_attach_receipt_sets_the_receipt_once(driver, world):
    call_id, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)
    assert receipt_of(driver, world, call_id) is None

    assert attach_receipt(driver, world, call_id, "rcpt-orchard-1") is None
    assert receipt_of(driver, world, call_id) == "rcpt-orchard-1"

    # The same receipt again is accepted and changes nothing.
    attach_receipt(driver, world, call_id, "rcpt-orchard-1")
    assert receipt_of(driver, world, call_id) == "rcpt-orchard-1"

    with pytest.raises(ModelCallRefused, match="the model call already has another receipt"):
        attach_receipt(driver, world, call_id, "rcpt-orchard-2")
    assert receipt_of(driver, world, call_id) == "rcpt-orchard-1"


def test_attach_receipt_touches_only_the_named_call(driver, world):
    first, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)
    second, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)

    attach_receipt(driver, world, first, "rcpt-orchard-1")

    assert receipt_of(driver, world, first) == "rcpt-orchard-1"
    assert receipt_of(driver, world, second) is None


def test_attach_receipt_refuses_an_unknown_id(driver, world):
    call_id, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)

    with pytest.raises(ModelCallRefused, match="no such model call in this world"):
        attach_receipt(driver, world, new_id(world), "rcpt-orchard-1")

    assert receipt_of(driver, world, call_id) is None


def test_attach_receipt_does_not_find_a_call_of_another_world(driver, make_world):
    owner, stranger = make_world(), make_world()
    call_id, _ = record_model_call(driver, owner, a_call(), default_ceiling=5.0)

    with pytest.raises(ModelCallRefused, match="no such model call in this world"):
        attach_receipt(driver, stranger, call_id, "rcpt-stranger")

    assert receipt_of(driver, owner, call_id) is None
    assert model_calls(driver, stranger) == []
    attach_receipt(driver, owner, call_id, "rcpt-owner")
    assert receipt_of(driver, owner, call_id) == "rcpt-owner"


@pytest.mark.parametrize(
    "receipt",
    [pytest.param("", id="empty"), pytest.param("r" * 81, id="long"), pytest.param(7, id="int")],
)
def test_attach_receipt_refuses_a_receipt_that_is_not_a_code(driver, world, receipt):
    call_id, _ = record_model_call(driver, world, a_call(), default_ceiling=5.0)

    with pytest.raises(ModelCallRefused, match="receipt must be a short ASCII code"):
        attach_receipt(driver, world, call_id, receipt)

    assert receipt_of(driver, world, call_id) is None


# Meter


def test_the_job_table_says_which_jobs_wait_at_the_ceiling():
    by_kind = {kind: set() for kind in ("defer", "continue")}
    for name, job in CATALOG.jobs.items():
        by_kind[job.at_ceiling].add(name)

    assert by_kind == {"defer": set(DEFER_JOBS), "continue": set(CONTINUE_JOBS)}


def test_meter_record_uses_the_daily_budget_as_the_default_ceiling(driver, world):
    _, budget = meter(driver, 0.3).record(world, a_call(cost=0.3))

    assert budget == BudgetState(spent=0.3, ceiling=0.3, state="reached")
    assert budgets(driver, world) == [budget_row(world, DAY, 0.3, "reached", 0.3)]


@pytest.mark.parametrize("name", DEFER_JOBS)
def test_gate_refuses_a_deferrable_job_until_the_reset(driver, world, frozen_clock, name):
    frozen_clock.set(datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC))
    gauge = meter(driver, 0.5)
    gauge.record(world, a_call(cost=0.75))

    with pytest.raises(BudgetReached) as caught:
        gauge.gate(world, CATALOG.job(name))

    refused = caught.value
    assert refused.details == {
        "job": name,
        "day": "2026-11-02",
        "spent": 0.75,
        "ceiling": 0.5,
        "resets_at": RESETS_AT,
        # One second before midnight: the wait to the reset, rounded up.
        "retry_after_seconds": 1,
    }
    assert (refused.code, refused.status, refused.retryable) == ("budget_reached", 429, True)
    assert refused.as_body() == {
        "error": {
            "code": "budget_reached",
            "type": "budget_reached",
            "message": "the daily budget is reached; this job waits for the reset",
            "details": refused.details,
            "retryable": True,
        }
    }

    # At the reset the job runs again: the new day has no row and a fresh ceiling.
    frozen_clock.advance(seconds=1)
    assert gauge.gate(world, CATALOG.job(name)) == Gate(
        day=NEXT_DAY, budget=BudgetState(spent=0.0, ceiling=0.5, state="open")
    )


@pytest.mark.parametrize("name", CONTINUE_JOBS)
def test_gate_lets_a_continue_job_run_at_the_ceiling(driver, world, frozen_clock, name):
    frozen_clock.set(datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC))
    gauge = meter(driver, 0.5)
    gauge.record(world, a_call(cost=0.75))
    with pytest.raises(BudgetReached):
        gauge.gate(world, CATALOG.job("verification"))

    assert gauge.gate(world, CATALOG.job(name)) == Gate(
        day=DAY, budget=BudgetState(spent=0.75, ceiling=0.5, state="reached")
    )


def test_resets_at_is_the_next_midnight_utc():
    assert resets_at(DAY) == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert resets_at(date(2026, 12, 31)) == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


def test_gate_follows_an_operator_ceiling_on_the_day(driver, world):
    gauge = meter(driver, 5.0)
    gauge.record(world, a_call(cost=0.75))
    verification = CATALOG.job("verification")
    assert gauge.gate(world, verification).budget.state == "open"

    set_budget_ceiling(driver, world, DAY, 0.5)
    with pytest.raises(BudgetReached) as caught:
        gauge.gate(world, verification)
    assert caught.value.details["ceiling"] == 0.5

    set_budget_ceiling(driver, world, DAY, None)
    assert gauge.gate(world, verification) == Gate(
        day=DAY, budget=BudgetState(spent=0.75, ceiling=None, state="open")
    )


def test_zero_daily_budget_with_no_row_refuses_a_deferrable_job(driver, world):
    gauge = meter(driver, 0.0)

    assert gauge.budget(world) == BudgetState(spent=0.0, ceiling=0.0, state="reached")
    with pytest.raises(BudgetReached) as caught:
        gauge.gate(world, CATALOG.job("consolidation_pass"))
    assert caught.value.details == {
        "job": "consolidation_pass",
        "day": "2026-11-02",
        "spent": 0.0,
        "ceiling": 0.0,
        "resets_at": RESETS_AT,
        # At noon, twelve hours to the reset.
        "retry_after_seconds": 43200,
    }
    assert gauge.gate(world, CATALOG.job("extraction")) == Gate(
        day=DAY, budget=BudgetState(spent=0.0, ceiling=0.0, state="reached")
    )
    # The gate only reads: it leaves no budget row.
    assert budgets(driver, world) == []


def test_no_daily_budget_refuses_nothing(driver, world):
    gauge = meter(driver, None)

    assert gauge.budget(world) == BudgetState(spent=0.0, ceiling=None, state="open")
    for name in DEFER_JOBS:
        assert gauge.gate(world, CATALOG.job(name)) == Gate(
            day=DAY, budget=BudgetState(spent=0.0, ceiling=None, state="open")
        )

    _, budget = gauge.record(world, a_call(cost=1000.0))
    assert budget == BudgetState(spent=1000.0, ceiling=None, state="open")
    for name in DEFER_JOBS:
        assert gauge.gate(world, CATALOG.job(name)) == Gate(day=DAY, budget=budget)


def test_meter_budget_reads_the_named_day(driver, world):
    gauge = meter(driver, 5.0)
    gauge.record(world, a_call(at=datetime(2026, 11, 3, 8, 0, tzinfo=UTC), cost=0.5))

    assert gauge.budget(world) == BudgetState(spent=0.0, ceiling=5.0, state="open")
    assert gauge.budget(world, NEXT_DAY) == BudgetState(spent=0.5, ceiling=5.0, state="open")


def test_check_run_cap_counts_input_and_output_of_the_run(driver, make_world):
    world, other_world = make_world(), make_world()
    gauge = meter(driver, None)
    assert gauge.check_run_cap(world, "run-orchard", 1) == 0

    # Input holds the cached tokens and output holds the reasoning ones: neither counts twice.
    gauge.record(
        world,
        a_call(
            run_id="run-orchard",
            input_tokens=600,
            cached_tokens=500,
            output_tokens=300,
            reasoning_tokens=250,
        ),
    )
    # Another run, a call with no run, and the same run id in another world do not count.
    gauge.record(world, a_call(run_id="run-cedar", input_tokens=5000, output_tokens=5000))
    gauge.record(world, a_call(input_tokens=5000, output_tokens=5000))
    gauge.record(other_world, a_call(run_id="run-orchard", input_tokens=5000, output_tokens=5000))

    assert gauge.check_run_cap(world, "run-orchard", 1000) == 900
    assert gauge.check_run_cap(world, "run-orchard", 901) == 900
    with pytest.raises(RunCapReached) as at_cap:
        gauge.check_run_cap(world, "run-orchard", 900)
    assert at_cap.value.details == {"used": 900, "token_cap": 900}
    assert (at_cap.value.code, at_cap.value.status) == ("run_cap_reached", 429)

    gauge.record(world, a_call(run_id="run-orchard", input_tokens=60, output_tokens=40))

    assert gauge.check_run_cap(world, "run-orchard", 1001) == 1000
    with pytest.raises(RunCapReached) as over:
        gauge.check_run_cap(world, "run-orchard", 1000)
    assert over.value.details == {"used": 1000, "token_cap": 1000}


def test_meter_calls_returns_the_day_rows_oldest_first(driver, make_world):
    world, other_world = make_world(), make_world()
    gauge = meter(driver, 5.0)
    ten = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    eleven = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)

    second, _ = gauge.record(
        world,
        a_call(
            job="verification",
            effort="low",
            at=ten,
            run_id="run-orchard",
            input_tokens=900,
            cached_tokens=100,
            output_tokens=1100,
            reasoning_tokens=700,
            cost=0.006,
            latency_ms=2400,
        ),
    )
    third, _ = gauge.record(
        world,
        ModelCall(
            job="transcription",
            model="muse-voice-transcribe-1.0",
            route="meta",
            outcome="ok",
            at=eleven,
            audio_seconds=30.0,
            cost=0.0015,
        ),
    )
    first, _ = gauge.record(world, a_call(at=NINE, outcome="invalid_output", cost=0.002))
    twin, _ = gauge.record(world, a_call(at=NINE, cost=0.001))
    gauge.record(world, a_call(at=datetime(2026, 11, 3, 0, 30, tzinfo=UTC), cost=0.5))
    gauge.record(other_world, a_call(at=NINE, cost=0.5))

    rows = gauge.calls(world, DAY)

    assert [row["id"] for row in rows] == [*sorted((first, twin)), second, third]
    by_id = {row["id"]: row for row in rows}
    assert by_id[second] == {
        "id": second,
        "job": "verification",
        "model": SPARK,
        "effort": "low",
        "route": "meta",
        "outcome": "ok",
        "run_id": "run-orchard",
        "input_tokens": 900,
        "cached_tokens": 100,
        "output_tokens": 1100,
        "reasoning_tokens": 700,
        "cost": 0.006,
        "audio_seconds": None,
        "latency_ms": 2400,
        "at": ten,
        "day": DAY,
        "usage_estimated": None,
    }
    assert by_id[third] == {
        "id": third,
        "job": "transcription",
        "model": "muse-voice-transcribe-1.0",
        "effort": None,
        "route": "meta",
        "outcome": "ok",
        "run_id": None,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost": 0.0015,
        "audio_seconds": 30.0,
        "latency_ms": None,
        "at": eleven,
        "day": DAY,
        "usage_estimated": None,
    }
    assert by_id[first]["outcome"] == "invalid_output"
    assert [(row["at"], row["cost"]) for row in gauge.calls(world, NEXT_DAY)] == [
        (datetime(2026, 11, 3, 0, 30, tzinfo=UTC), 0.5)
    ]
    assert [row["cost"] for row in gauge.calls(other_world, DAY)] == [0.5]
    assert gauge.calls(world, date(2026, 11, 1)) == []
