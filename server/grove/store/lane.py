"""The verification lane's pool and its batch (D4, "The verification lane"; Appendix C).

The lane is how Grove pays what the horizons say it owes. This module only reads: it says which
claims are due and in what order the clean-up agent takes them. Each disposition goes through the
writer's verify, so nothing here can move a clock.

The pool, from declared fields only (no kind or meaning is named in code):

- a claim about now whose re-check by has passed or is missing: open work (a work item at a
  non-terminal status role), a bounded period that has begun and not ended, and an active link of
  a meaning of temporal class state. Beliefs are left out: one live read cannot confirm a
  regularity, so the consolidation agent re-tests them (D9, D12).
- a record that holds a measured figure (a property of temporal class state) past its horizon.
  It is a claim about now too, and it ranks after the others.
- a head marked revision due: a claim with a last confirmed and an active evidence row on it that
  arrived after that confirmation. "After" is read against the moment of the confirmation (the
  verification row that confirmed_against names), not against the time the evidence spoke for:
  verify itself writes its pieces as evidence rows at the commit, and those rows are newer than
  the source time they confirm, so a comparison with last confirmed would mark every confirmation
  revision due at once and the lane would chase its own work.

The order of a batch: revision-due heads first; then claims about now before records that hold a
figure; lapsed horizons before missing ones; load-bearing claims before leaves, by the count of
active consequence links into the claim; ties by the oldest confirmation, a claim never confirmed
first, by its recorded time. A third of each batch is kept for the claims unconfirmed longest that
the ranked part left out, so a leaf behind a crowd of always-due load-bearing claims still comes
up: each offer either confirms the claim (it becomes young) or defers it (it leaves the pool), so
every claim is offered within ceil(pool / reserved) passes.
"""

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from grove.store.current import TERMINAL_ROLES, has_ended
from grove.store.primitives import KIND_BELIEF
from grove.store.query import WorldQuery
from grove.writer.registry import Registry

# Appendix C: "a resource cap on prompt size, not a quota".
BATCH_SIZE = 12
# Appendix C: a third of each batch, for the claims unconfirmed longest.
RESERVED_SHARE = 3

REVISION_DUE = "revision_due"
HORIZON_LAPSED = "horizon_lapsed"
HORIZON_MISSING = "horizon_missing"

ABOUT_NOW = "about_now"
FIGURE = "figure"
# A revision-due head that is not a claim about now (a lasting thing, a permanent link).
HEAD = "head"
_CLASS_ORDER = {ABOUT_NOW: 0, FIGURE: 1, HEAD: 2}

_UNKNOWN = "unknown"
_EARLIEST = datetime.min.replace(tzinfo=UTC)

_ITEMS = (
    "MATCH (i:Item) WHERE i.end_class IS NULL AND i.kind <> $belief "
    "RETURN i.id AS id, i.kind AS kind, i.status_role AS status_role, i.end_class AS end_class, "
    "i.became_true_precision AS became_true_precision, "
    "i.stopped_being_true_precision AS stopped_being_true_precision, "
    "i.recheck_by AS recheck_by, i.last_confirmed AS last_confirmed, "
    "i.confirmed_against AS confirmed_against, i.recorded AS recorded, "
    "i.properties AS properties"
)
_LINKS = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.active_key IS NOT NULL AND l.end_class IS NULL "
    "RETURN l.id AS id, l.meaning AS meaning, l.recheck_by AS recheck_by, "
    "l.last_confirmed AS last_confirmed, l.confirmed_against AS confirmed_against, "
    "l.recorded AS recorded"
)
_CONSEQUENCES_IN = (
    "MATCH (:Item)-[l:LINK]->(b:Item) WHERE l.active_key IS NOT NULL AND l.meaning IN $meanings "
    "RETURN b.id AS id, count(l) AS n"
)
_NEWEST_EVIDENCE = (
    "MATCH (e:Evidence) WHERE e.active = true AND e.target_type IN ['item', 'link'] "
    "AND e.target_id IN $ids "
    "RETURN e.target_id AS id, max(e.recorded) AS newest"
)
_CONFIRMED_AT = "MATCH (v:Verification) WHERE v.id IN $ids RETURN v.id AS id, v.at AS at"


@dataclass(frozen=True)
class Due:
    """One claim the lane owes a check, with the fields its rank is read from.

    `reasons` holds revision_due, horizon_lapsed or horizon_missing; `claim_class` is about_now,
    figure or head; `reserved` is set on a batch entry that the reserved slice chose."""

    claim_id: str
    claim_type: str
    reasons: tuple[str, ...]
    claim_class: str
    recheck_by: datetime | None
    last_confirmed: datetime | None
    recorded: datetime | None
    consequence_links_in: int
    reserved: bool = False

    @property
    def revision_due(self) -> bool:
        return REVISION_DUE in self.reasons

    @property
    def load_bearing(self) -> bool:
        return self.consequence_links_in > 0


def _aware(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError("the lane reads at an aware time from the clock provider")
    return at


def is_due(recheck_by: datetime | None, at: datetime) -> bool:
    """A horizon that has passed, or none at all."""
    return recheck_by is None or recheck_by <= at


def _horizon_reason(recheck_by: datetime | None, at: datetime) -> str | None:
    if recheck_by is None:
        return HORIZON_MISSING
    return HORIZON_LAPSED if recheck_by <= at else None


def _figure_names(registry: Registry) -> set[tuple[str, str]]:
    """(kind id, property name) of every declared property of temporal class state."""
    return {
        (row["kind"], row["name"])
        for row in registry.property_names
        if row.get("temporal_class") == "state" and row.get("kind") and row.get("name")
    }


def _item_class(row: dict[str, Any], registry: Registry, figures: set) -> str | None:
    """ABOUT_NOW or FIGURE for a claim about now, else None. The same field-based cases as the
    writer's create-time rule, read from the head row."""
    if has_ended(row):
        return None
    kind = registry.kinds.get(row["kind"]) or {}
    lifecycle = kind.get("lifecycle_class")
    if lifecycle == "work_with_state":
        role = row.get("status_role")
        return ABOUT_NOW if role is not None and role not in TERMINAL_ROLES else None
    if lifecycle == "bounded_period":
        begun = row.get("became_true_precision") not in (None, _UNKNOWN)
        return ABOUT_NOW if begun and row.get("stopped_being_true_precision") is None else None
    if lifecycle == "record":
        names = json.loads(row.get("properties") or "{}")
        return FIGURE if any((row["kind"], name) in figures for name in names) else None
    return None


def _meaning(registry: Registry, meaning_id: str) -> dict[str, Any]:
    try:
        return registry.meaning(meaning_id)
    except KeyError:
        return {}


def _confirmation_key(claim: Due) -> tuple:
    # Never confirmed sorts first, oldest recorded first; then the oldest confirmation.
    if claim.last_confirmed is None:
        return (0, claim.recorded or _EARLIEST, claim.claim_id)
    return (1, claim.last_confirmed, claim.claim_id)


def _horizon_order(claim: Due) -> int:
    if HORIZON_LAPSED in claim.reasons:
        return 0
    if HORIZON_MISSING in claim.reasons:
        return 1
    return 2


def rank_key(claim: Due) -> tuple:
    """The engine's order, as one sort key (D4)."""
    return (
        0 if claim.revision_due else 1,
        _CLASS_ORDER[claim.claim_class],
        _horizon_order(claim),
        -claim.consequence_links_in,
        _confirmation_key(claim),
    )


def pool(query: WorldQuery, *, at: datetime) -> list[Due]:
    """Every claim the lane owes a check at `at`, in rank order."""
    at = _aware(at)
    registry = Registry.load(query)
    figures = _figure_names(registry)
    found: dict[str, dict[str, Any]] = {}
    for row in query.read(_ITEMS, belief=KIND_BELIEF):
        if has_ended(row):
            continue
        found[row["id"]] = {"type": "item", "class": _item_class(row, registry, figures), **row}
    for row in query.read(_LINKS):
        state = _meaning(registry, row["meaning"]).get("temporal_class") == "state"
        found[row["id"]] = {"type": "link", "class": ABOUT_NOW if state else None, **row}

    confirmed = [row["id"] for row in found.values() if row.get("last_confirmed") is not None]
    newest = {
        row["id"]: row["newest"]
        for row in (query.read(_NEWEST_EVIDENCE, ids=confirmed) if confirmed else [])
    }
    against = sorted(
        {found[i]["confirmed_against"] for i in confirmed if found[i].get("confirmed_against")}
    )
    confirmed_at = {
        row["id"]: row["at"] for row in (query.read(_CONFIRMED_AT, ids=against) if against else [])
    }
    consequence = sorted(
        meaning_id
        for meaning_id, meaning in registry.meanings.items()
        if meaning.get("carries_consequences")
    )
    links_in = {
        row["id"]: row["n"]
        for row in (query.read(_CONSEQUENCES_IN, meanings=consequence) if consequence else [])
    }

    due = []
    for claim_id, row in found.items():
        reasons = []
        moment = confirmed_at.get(row.get("confirmed_against")) or row.get("last_confirmed")
        arrived = newest.get(claim_id)
        if moment is not None and arrived is not None and arrived > moment:
            reasons.append(REVISION_DUE)
        claim_class = row["class"]
        if claim_class is not None:
            horizon = _horizon_reason(row.get("recheck_by"), at)
            if horizon is not None:
                reasons.append(horizon)
        if not reasons:
            continue
        due.append(
            Due(
                claim_id=claim_id,
                claim_type=row["type"],
                reasons=tuple(reasons),
                claim_class=claim_class or HEAD,
                recheck_by=row.get("recheck_by"),
                last_confirmed=row.get("last_confirmed"),
                recorded=row.get("recorded"),
                consequence_links_in=links_in.get(claim_id, 0) if row["type"] == "item" else 0,
            )
        )
    return sorted(due, key=rank_key)


def reserved_for(size: int) -> int:
    """The reserved slice of a batch of this size: a third, rounded down."""
    return size // RESERVED_SHARE


def select(claims: Iterable[Due], *, size: int, reserved: int | None = None) -> list[Due]:
    """A batch from due claims: the ranked part, then the reserved slice, which takes the claims
    unconfirmed longest among those the ranked part left out. `reserved` defaults to a third of
    `size`; 0 turns the slice off (the starvation the slice exists to prevent)."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError("a batch holds at least one claim")
    keep = reserved_for(size) if reserved is None else reserved
    if isinstance(keep, bool) or not isinstance(keep, int) or not 0 <= keep <= size:
        raise ValueError("the reserved slice runs from 0 to the batch size")
    ranked: Sequence[Due] = sorted(claims, key=rank_key)
    if len(ranked) <= size:
        return list(ranked)
    head = list(ranked[: size - keep])
    rest = sorted(ranked[size - keep :], key=_confirmation_key)
    return head + [replace(claim, reserved=True) for claim in rest[:keep]]


def batch(
    query: WorldQuery, *, at: datetime, size: int = BATCH_SIZE, reserved: int | None = None
) -> list[Due]:
    """The claims the clean-up agent takes in one pass at `at`."""
    return select(pool(query, at=at), size=size, reserved=reserved)


def passes_bound(pool_size: int, size: int = BATCH_SIZE) -> int:
    """The passes within which every claim of a pool is offered, when each offer confirms or
    defers the claim: each pass the reserved slice takes the oldest claims not yet offered."""
    keep = reserved_for(size)
    if keep < 1:
        raise ValueError("a batch this small has no reserved slice")
    return math.ceil(pool_size / keep)
