"""Pure D7 coverage and answer decisions over an authorized, complete capture.

ConnectorState is a whitelist: no auth reference, scope, settings, watermark, source text
or unreviewed failure message is accepted or returned. Its outcome is the catalogue's
safe reason code. The adapter owns D14 filtering and supplies completeness explicitly.
An incomplete disclosure/inventory can never justify global absence, even when every
visible connector is fresh. No hidden connector identifier or count is required.

All dates are captured aware datetimes. `now` is supplied, never read. None for an
expected interval means an upload, with freshness bounded by its last successful upload.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from grove.store.enums import ENUMS
from grove.store.scope import check_world_id


class CoverageError(ValueError):
    """A typed refusal containing only a fixed safe code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _at(value: object) -> datetime:
    try:
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except (ValueError, OverflowError, TypeError) as exc:
        raise CoverageError("invalid_coverage_time") from exc


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageError("invalid_coverage_input")
    return value


def _codes(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise CoverageError("invalid_coverage_input")
    return tuple(sorted({_text(value) for value in values}))


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise CoverageError("invalid_coverage_input")
    return value


@dataclass(frozen=True)
class ConnectorState:
    world_id: str
    connector_id: str
    source_kind: str
    last_outcome: str | None
    expected_read_interval: int | None
    last_successful_read: datetime | None = None
    last_attempted_read: datetime | None = None
    oldest_unprocessed_item: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.connector_id)
        if self.source_kind not in ENUMS["connector_type"]:
            raise CoverageError("invalid_coverage_kind")
        if self.last_outcome is not None and self.last_outcome not in ENUMS["connector_outcome"]:
            raise CoverageError("invalid_coverage_outcome")
        interval = self.expected_read_interval
        if interval is not None and (type(interval) is not int or interval <= 0):
            raise CoverageError("invalid_coverage_interval")
        for value in (
            self.last_successful_read,
            self.last_attempted_read,
            self.oldest_unprocessed_item,
        ):
            if value is not None:
                _at(value)


@dataclass(frozen=True)
class SourceCoverage:
    connector_id: str
    source_kind: str
    last_outcome: str | None
    expected_read_interval: int | None
    last_successful_read: datetime | None
    last_attempted_read: datetime | None
    oldest_unprocessed_item: datetime | None
    behind_seconds: float | None
    reasons: tuple[str, ...]
    since: datetime | None


@dataclass(frozen=True)
class CoverageStatement:
    sources: tuple[SourceCoverage, ...]
    not_connected: tuple[str, ...]
    incomplete: bool
    since: datetime | None

    @property
    def fresh(self) -> bool:
        return not self.incomplete and not any(source.reasons for source in self.sources)


def evaluate_coverage(
    world_id: str,
    connectors: Sequence[ConnectorState],
    *,
    now: datetime,
    question_at: datetime | None = None,
    inventory_complete: bool,
    disclosure_complete: bool,
    not_connected: Sequence[str] = (),
) -> CoverageStatement:
    """Evaluate every supplied source; unknown gap dates dominate known gap dates.

    Current questions allow the configured polling interval. Historical questions require
    successful coverage through `question_at`. Denied, failed and kept-out outcomes stay
    gaps regardless of timestamps; an old buggy success stamp cannot turn them into success.
    Any pending backlog blocks absence. Missing source kinds are declared, not guessed.
    """
    try:
        check_world_id(world_id)
    except (ValueError, TypeError) as exc:
        raise CoverageError("invalid_coverage_world") from exc
    captured_at = _at(now)
    reference_at = _at(question_at) if question_at is not None else captured_at
    if reference_at > captured_at:
        raise CoverageError("invalid_coverage_time")
    incomplete = not (_boolean(inventory_complete) & _boolean(disclosure_complete))
    missing = _codes(not_connected)
    sources: list[SourceCoverage] = []
    seen: set[str] = set()
    for row in connectors:
        if not isinstance(row, ConnectorState) or row.world_id != world_id:
            raise CoverageError("invalid_coverage_world")
        if row.connector_id in seen:
            raise CoverageError("duplicate_coverage_source")
        seen.add(row.connector_id)
        if row.source_kind in missing:
            raise CoverageError("inconsistent_coverage_inventory")
        for value in (row.last_successful_read, row.last_attempted_read):
            if value is not None and value > captured_at:
                raise CoverageError("invalid_coverage_time")
        success = row.last_successful_read
        reasons: list[str] = []
        dates: list[datetime] = []
        if row.last_outcome in ("denied", "failed", "kept_out"):
            reasons.append(row.last_outcome)
        elif row.last_outcome is None:
            reasons.append("unknown_outcome")
        elapsed = None if success is None else max(0.0, (reference_at - success).total_seconds())
        interval = row.expected_read_interval if question_at is None else 0
        behind = None if elapsed is None else max(0.0, elapsed - (interval or 0))
        if success is None:
            reasons.append("unknown_success")
        elif behind:
            reasons.append("behind")
        if reasons and success is not None:
            dates.append(success)
        if row.oldest_unprocessed_item is not None:
            reasons.append("unprocessed")
            dates.append(row.oldest_unprocessed_item)
        since = min(dates) if dates and success is not None else None
        sources.append(
            SourceCoverage(
                row.connector_id,
                row.source_kind,
                row.last_outcome,
                row.expected_read_interval,
                success,
                row.last_attempted_read,
                row.oldest_unprocessed_item,
                behind,
                tuple(reasons),
                since,
            )
        )
    sources.sort(key=lambda source: source.connector_id)
    gaps = [source for source in sources if source.reasons]
    since = (
        min(source.since for source in gaps)
        if gaps and not incomplete and all(source.since is not None for source in gaps)
        else None
    )
    return CoverageStatement(tuple(sources), missing, incomplete, since)


@dataclass(frozen=True)
class AnswerDecision:
    answer: str
    returned: int
    total: int
    truncated: bool


def decide_answer(
    coverage: CoverageStatement,
    *,
    total: int,
    returned: int,
    eligibility_complete: bool,
    ambiguous: bool = False,
) -> AnswerDecision:
    """Use the complete eligible match count, never a page count, to decide absence.

    Ambiguity remains explicit even when its candidate list needs pagination; `truncated`
    and `total` still report that limit. Incomplete eligibility is an error, not absence.
    """
    if not isinstance(coverage, CoverageStatement):
        raise CoverageError("invalid_coverage_input")
    if not _boolean(eligibility_complete):
        raise CoverageError("incomplete_eligibility")
    _boolean(ambiguous)
    if type(total) is not int or type(returned) is not int or not 0 <= returned <= total:
        raise CoverageError("invalid_coverage_count")
    if ambiguous and total < 2:
        raise CoverageError("invalid_identity_ambiguity")
    truncated = total > returned
    if ambiguous:
        answer = "ambiguous"
    elif truncated:
        answer = "truncated"
    elif total:
        answer = "found"
    else:
        answer = "no_record" if coverage.fresh else "not_seen_since"
    return AnswerDecision(answer, returned, total, truncated)


@dataclass(frozen=True)
class ItemCoverageGap:
    since: datetime | None
    connector_ids: tuple[str, ...]
    not_connected: tuple[str, ...]
    incomplete: bool


def item_coverage_gap(
    coverage: CoverageStatement,
    *,
    contributed_connector_ids: Sequence[str] = (),
    recheck_connector_ids: Sequence[str] = (),
    recheck_missing_kinds: Sequence[str] = (),
    references_complete: bool,
) -> ItemCoverageGap | None:
    """Select gaps only through explicit provenance/recheck references, never free text.

    A missing connector reference could be excluded by D14: return a generic incomplete
    gap without its identifier. Explicit unconnected kinds must be in the safe inventory.
    """
    if not isinstance(coverage, CoverageStatement):
        raise CoverageError("invalid_coverage_input")
    ids = set(_codes(contributed_connector_ids)) | set(_codes(recheck_connector_ids))
    missing = _codes(recheck_missing_kinds)
    if not set(missing) <= set(coverage.not_connected):
        raise CoverageError("unknown_coverage_reference")
    by_id = {source.connector_id: source for source in coverage.sources}
    incomplete = not _boolean(references_complete) or coverage.incomplete or not ids <= by_id.keys()
    relevant = [by_id[key] for key in sorted(ids & by_id.keys()) if by_id[key].reasons]
    if not relevant and not missing and not incomplete:
        return None
    since = (
        min(source.since for source in relevant)
        if relevant
        and not missing
        and not incomplete
        and all(s.since is not None for s in relevant)
        else None
    )
    return ItemCoverageGap(since, tuple(row.connector_id for row in relevant), missing, incomplete)
