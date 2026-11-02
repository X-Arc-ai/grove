"""The model-call layer's writes: one model_calls row per call, and the budget it spends (S20, S39).

Only grove.models calls these. The row and the budget move in one transaction, so the day's
`spent` is always the sum of the day's recorded costs. The budget's state turns `reached` in the
same transaction as the call that reaches the ceiling.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from neo4j import Driver

from grove.ids import new_id
from grove.store.enums import ENUMS
from grove.writer.tx import WorldTx, WorldWriter

OPEN = "open"
REACHED = "reached"
ROUTES = ("meta", "modal")
# A short code per outcome. The layer records every call that reached a provider, a failed one
# included, so the share of calls that wrote nothing counts them all (D15).
OUTCOMES = (
    "ok",
    "invalid_output",
    "rate_limited",
    "provider_error",
    "client_closed",
)
_EFFORTS = frozenset(ENUMS["effort"])
_STATES = frozenset(ENUMS["budget_state"])

# The SET on the budget takes its write lock before it reads `spent`, so two calls that commit at
# once both count. MERGE on (world_id, day), which has a uniqueness constraint, leaves one row.
_RECORD = (
    "CREATE (c:ModelCall) SET c += $row "
    "MERGE (b:Budget {day: $day}) "
    "ON CREATE SET b.spent = 0.0, b.state = $open, b.ceiling = $default_ceiling "
    "SET b.spent = b.spent + $cost "
    "SET b.state = CASE WHEN b.ceiling IS NOT NULL AND b.spent >= b.ceiling "
    "THEN $reached ELSE $open END "
    "RETURN c.id AS id, b.spent AS spent, b.ceiling AS ceiling, b.state AS state"
)
_SET_CEILING = (
    "MERGE (b:Budget {day: $day}) "
    "ON CREATE SET b.spent = 0.0 "
    "SET b.ceiling = $ceiling "
    "SET b.state = CASE WHEN b.ceiling IS NOT NULL AND b.spent >= b.ceiling "
    "THEN $reached ELSE $open END "
    "RETURN b.spent AS spent, b.ceiling AS ceiling, b.state AS state"
)
_ATTACH = (
    "MATCH (c:ModelCall {id: $id}) "
    "SET c.receipt_reference = CASE WHEN c.receipt_reference IS NULL "
    "THEN $receipt ELSE c.receipt_reference END "
    "RETURN c.receipt_reference AS receipt_reference"
)


class ModelCallRefused(ValueError):
    """A model call row the writer will not store. The message names the field, never a value."""


@dataclass(frozen=True)
class ModelCall:
    job: str
    model: str
    route: str
    outcome: str
    at: datetime
    effort: str | None = None
    run_id: str | None = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    audio_seconds: float | None = None
    latency_ms: int | None = None
    usage_estimated: bool = False


@dataclass(frozen=True)
class BudgetState:
    spent: float
    ceiling: float | None
    state: str


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelCallRefused(f"{name} must be a non-negative integer")
    return value


def _money(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelCallRefused(f"{name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ModelCallRefused(f"{name} must be finite and not negative")
    return float(value)


def _code(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 80 or not value.isascii():
        raise ModelCallRefused(f"{name} must be a short ASCII code")
    return value


def _row(call: ModelCall) -> dict:
    if not isinstance(call.at, datetime) or call.at.utcoffset() is None:
        raise ModelCallRefused("at must be an aware datetime from the clock provider")
    if call.route not in ROUTES:
        raise ModelCallRefused("route is not a known route")
    if call.outcome not in OUTCOMES:
        raise ModelCallRefused("outcome is not a known outcome")
    if call.effort is not None and (
        not isinstance(call.effort, str) or call.effort not in _EFFORTS
    ):
        raise ModelCallRefused("effort is not in the effort enum")
    input_tokens = _count("input_tokens", call.input_tokens)
    cached = _count("cached_tokens", call.cached_tokens)
    output = _count("output_tokens", call.output_tokens)
    reasoning = _count("reasoning_tokens", call.reasoning_tokens)
    if cached > input_tokens:
        raise ModelCallRefused("cached_tokens cannot exceed input_tokens")
    if reasoning > output:
        raise ModelCallRefused("reasoning_tokens cannot exceed output_tokens")
    row = {
        "job": _code("job", call.job),
        "model": _code("model", call.model),
        "route": call.route,
        "outcome": call.outcome,
        "effort": call.effort,
        "run_id": None if call.run_id is None else _code("run_id", call.run_id),
        "input_tokens": input_tokens,
        "cached_tokens": cached,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "cost": _money("cost", call.cost),
        "audio_seconds": (
            None if call.audio_seconds is None else _money("audio_seconds", call.audio_seconds)
        ),
        "latency_ms": None if call.latency_ms is None else _count("latency_ms", call.latency_ms),
        "usage_estimated": True if call.usage_estimated is True else None,
        "at": call.at.astimezone(UTC),
    }
    return {key: value for key, value in row.items() if value is not None}


def _budget(row: dict) -> BudgetState:
    state = row["state"]
    if state not in _STATES:
        raise ModelCallRefused("the stored budget state is not in the enum")
    return BudgetState(spent=row["spent"], ceiling=row.get("ceiling"), state=state)


def record_model_call(
    driver: Driver, world_id: str, call: ModelCall, *, default_ceiling: float | None
) -> tuple[str, BudgetState]:
    """Store one call and add its cost to the day's budget. `default_ceiling` is used only when
    the day's budget row does not exist yet; None means no ceiling."""
    row = _row(call)
    if default_ceiling is not None:
        default_ceiling = _money("default_ceiling", default_ceiling)
    day = row["at"].date()

    def work(tx: WorldTx) -> tuple[str, BudgetState]:
        row_id = new_id(world_id)
        result = tx.run(
            _RECORD,
            row={**row, "id": row_id, "day": day},
            day=day,
            cost=row["cost"],
            default_ceiling=default_ceiling,
            open=OPEN,
            reached=REACHED,
        )
        return result[0]["id"], _budget(result[0])

    return WorldWriter(driver, world_id).transaction(work)


def set_budget_ceiling(driver: Driver, world_id: str, day, ceiling: float | None) -> BudgetState:
    """The operator's ceiling for one world and day; None removes the ceiling."""
    if ceiling is not None:
        ceiling = _money("ceiling", ceiling)

    def work(tx: WorldTx) -> BudgetState:
        rows = tx.run(_SET_CEILING, day=day, ceiling=ceiling, open=OPEN, reached=REACHED)
        return _budget(rows[0])

    return WorldWriter(driver, world_id).transaction(work)


def attach_receipt(driver: Driver, world_id: str, model_call_id: str, receipt: str) -> None:
    """Set the receipt a call led to, once. A second, different receipt is refused."""
    receipt = _code("receipt", receipt)

    def work(tx: WorldTx) -> list[dict]:
        return tx.run(_ATTACH, id=model_call_id, receipt=receipt)

    rows = WorldWriter(driver, world_id).transaction(work)
    if not rows:
        raise ModelCallRefused("no such model call in this world")
    if rows[0]["receipt_reference"] != receipt:
        raise ModelCallRefused("the model call already has another receipt")
