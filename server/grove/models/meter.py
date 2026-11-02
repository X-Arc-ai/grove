"""Metering and the daily budget (D15): the gate before a call and the record after it.

A job marked `defer` waits once the world's day has reached its ceiling: verification,
consolidation, second opinions, and every agent pass (an agent call stops at the ceiling). A job
marked `continue` runs on, because it keeps memory current, runs in code and is the cheap part:
triage, extraction and transcription. Both kinds are metered the same way.

Two calls that pass the gate at the same moment can both run, so a day can end above its ceiling
by the cost of the calls in flight. The ceiling stops the next deferrable call, not a call that
has already started.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from grove import clock
from grove.models.config import Catalog, Job, ModelSettings
from grove.models.errors import BudgetReached, RunCapReached
from grove.store.connection import Driver
from grove.store.query import WorldQuery
from grove.writer.model_calls import (
    OPEN,
    REACHED,
    BudgetState,
    ModelCall,
    attach_receipt,
    record_model_call,
)

_BUDGET = (
    "MATCH (b:Budget {day: $day}) RETURN b.spent AS spent, b.ceiling AS ceiling, b.state AS state"
)
_RUN_TOKENS = (
    "MATCH (c:ModelCall {run_id: $run_id}) "
    "RETURN coalesce(sum(c.input_tokens + c.output_tokens), 0) AS used"
)
_DAY_CALLS = (
    "MATCH (c:ModelCall {day: $day}) RETURN c.id AS id, c.job AS job, c.model AS model, "
    "c.effort AS effort, c.route AS route, c.outcome AS outcome, c.run_id AS run_id, "
    "c.input_tokens AS input_tokens, c.cached_tokens AS cached_tokens, "
    "c.output_tokens AS output_tokens, c.reasoning_tokens AS reasoning_tokens, c.cost AS cost, "
    "c.audio_seconds AS audio_seconds, c.latency_ms AS latency_ms, c.at AS at, c.day AS day, "
    "c.usage_estimated AS usage_estimated "
    "ORDER BY c.at, c.id"
)


def reached(budget: BudgetState) -> bool:
    over = budget.ceiling is not None and budget.spent >= budget.ceiling
    return budget.state == REACHED or over


def resets_at(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=UTC)


@dataclass(frozen=True)
class Gate:
    day: date
    budget: BudgetState


class Meter:
    def __init__(self, driver: Driver, catalog: Catalog, settings: ModelSettings) -> None:
        self._driver = driver
        self._catalog = catalog
        self._settings = settings

    def now(self, world_id: str) -> datetime:
        return clock.now(world_id).astimezone(UTC)

    def budget(self, world_id: str, day: date | None = None) -> BudgetState:
        day = day or self.now(world_id).date()
        rows = WorldQuery(self._driver, world_id).read(_BUDGET, day=day)
        if not rows:
            ceiling = self._settings.daily_budget
            state = REACHED if ceiling is not None and ceiling <= 0 else OPEN
            return BudgetState(spent=0.0, ceiling=ceiling, state=state)
        row = rows[0]
        return BudgetState(spent=row["spent"], ceiling=row.get("ceiling"), state=row["state"])

    def gate(self, world_id: str, job: Job, *, every_job: bool = False) -> Gate:
        """Refuse a deferrable job once the day's ceiling is reached. Nothing is sent then. The
        gateway passes every_job: an agent call stops at the ceiling, whatever its token names."""
        now = self.now(world_id)
        day = now.date()
        budget = self.budget(world_id, day)
        if (every_job or job.at_ceiling == "defer") and reached(budget):
            wait = math.ceil((resets_at(day) - now).total_seconds())
            raise BudgetReached(
                "the daily budget is reached; this job waits for the reset",
                details={
                    "job": job.name,
                    "day": day.isoformat(),
                    "spent": budget.spent,
                    "ceiling": budget.ceiling,
                    "resets_at": resets_at(day).isoformat(),
                    "retry_after_seconds": max(wait, 1),
                },
            )
        return Gate(day=day, budget=budget)

    def check_run_cap(self, world_id: str, run_id: str, token_cap: int) -> int:
        used = WorldQuery(self._driver, world_id).read(_RUN_TOKENS, run_id=run_id)[0]["used"]
        if used >= token_cap:
            raise RunCapReached(
                "the run used its token cap", details={"used": used, "token_cap": token_cap}
            )
        return used

    def record(self, world_id: str, call: ModelCall) -> tuple[str, BudgetState]:
        return record_model_call(
            self._driver, world_id, call, default_ceiling=self._settings.daily_budget
        )

    def attach_receipt(self, world_id: str, model_call_id: str, receipt: str) -> None:
        """The receipt a call led to (D15). A gateway row has no id for its caller: it joins its
        receipts through its run id and the run's record."""
        attach_receipt(self._driver, world_id, model_call_id, receipt)

    def calls(self, world_id: str, day: date) -> list[dict]:
        """The day's model_calls rows, oldest first: the evidence and operator read."""
        return WorldQuery(self._driver, world_id).read(_DAY_CALLS, day=day)
