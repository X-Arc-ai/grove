"""Pure D7 identity matching over a complete, authorized capture.

The adapter must supply only same-world, D3-eligible, D14-disclosable items and aliases.
IdentityField values are explicitly authorized names, nicknames, handles or role phrases;
they are not arbitrary properties, search terms, history text, evidence or speaker labels.
In particular, a Voice Transcribe label is not an IdentityField. This boundary does not
implement D14 or establish that a caller's inputs satisfy it.

Every candidate is evaluated before selection. Exact IDs win; otherwise all exact phrases
and trigram/short-edit candidates remain. Results contain IDs and scores, never matched text.
No clock, database, model, I/O or retained cache enters this module.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from grove.store.enums import ENUMS
from grove.store.normalise import normalise_phrase
from grove.store.scope import check_world_id
from grove.writer.neardup import CANDIDATE_THRESHOLD, trigrams


class IdentityError(ValueError):
    """A safe structural refusal without captured values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityError("invalid_identity_input")
    return value


def _at(value: object) -> datetime:
    try:
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except (ValueError, OverflowError, TypeError) as exc:
        raise IdentityError("invalid_identity_time") from exc


def _bounds(start: object, end: object) -> None:
    if start is not None:
        _at(start)
    if end is not None:
        _at(end)
    if start is not None and end is not None and start >= end:
        raise IdentityError("invalid_identity_time")


@dataclass(frozen=True)
class IdentityField:
    kind: str
    value: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("name", "nickname", "handle", "role"):
            raise IdentityError("invalid_identity_field")
        _text(self.value)
        _bounds(self.valid_from, self.valid_to)


@dataclass(frozen=True)
class IdentityItem:
    world_id: str
    item_id: str
    title: str
    fields: tuple[IdentityField, ...] = ()

    def __post_init__(self) -> None:
        _text(self.item_id)
        _text(self.title)
        if not isinstance(self.fields, (tuple, list)) or any(
            not isinstance(value, IdentityField) for value in self.fields
        ):
            raise IdentityError("invalid_identity_field")
        object.__setattr__(self, "fields", tuple(self.fields))


@dataclass(frozen=True)
class IdentityAlias:
    world_id: str
    target_id: str
    phrase: str
    status: str
    target_type: str = "item"
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.target_id)
        _text(self.phrase)
        if self.status not in ENUMS["registry_status"]:
            raise IdentityError("invalid_identity_alias")
        if self.target_type not in ENUMS["alias_target_type"]:
            raise IdentityError("invalid_identity_alias")
        _bounds(self.valid_from, self.valid_to)


@dataclass(frozen=True)
class IdentityCandidate:
    item_id: str
    score: float
    classification: str


@dataclass(frozen=True)
class IdentityResult:
    answer: str
    candidates: tuple[IdentityCandidate, ...]


def _active(start: datetime | None, end: datetime | None, at: datetime) -> bool:
    return (start is None or start <= at) and (end is None or at < end)


def _short_distance(left: str, right: str, bound: int) -> int:
    """Banded optimal-string-alignment distance, including one adjacent transposition."""
    if abs(len(left) - len(right)) > bound:
        return bound + 1
    previous = {column: column for column in range(min(len(right), bound) + 1)}
    before_previous: dict[int, int] = {}
    missing = bound + 1
    for row, char in enumerate(left, 1):
        current = {0: row} if row <= bound else {}
        for column in range(max(1, row - bound), min(len(right), row + bound) + 1):
            value = min(
                previous.get(column, missing) + 1,
                current.get(column - 1, missing) + 1,
                previous.get(column - 1, missing) + (char != right[column - 1]),
            )
            if (
                row > 1
                and column > 1
                and char == right[column - 2]
                and left[row - 2] == right[column - 1]
            ):
                value = min(value, before_previous.get(column - 2, missing) + 1)
            current[column] = value
        before_previous, previous = previous, current
    return min(previous.get(len(right), missing), missing)


def resolve_identity(
    world_id: str,
    query: str,
    items: Sequence[IdentityItem],
    aliases: Sequence[IdentityAlias],
    *,
    at: datetime,
    complete: bool,
) -> IdentityResult:
    """Resolve all eligible identities at `at`; role intervals are [valid_from, valid_to).

    `complete` certifies an exhaustive authorized candidate set, not an early top-k page.
    Non-item, retired/split and out-of-time aliases cannot match. Dangling aliases cannot
    create candidates. S34 normalization never implicitly transliterates scripts.
    """
    try:
        check_world_id(world_id)
    except (ValueError, TypeError) as exc:
        raise IdentityError("invalid_identity_world") from exc
    selected_at = _at(at)
    if type(complete) is not bool or not complete:
        raise IdentityError("incomplete_identity_input")
    _text(query)
    normalized = normalise_phrase(query)
    if not normalized:
        raise IdentityError("invalid_identity_query")
    rows: dict[str, IdentityItem] = {}
    for item in items:
        if not isinstance(item, IdentityItem) or item.world_id != world_id:
            raise IdentityError("invalid_identity_world")
        if item.item_id in rows:
            raise IdentityError("duplicate_identity_item")
        rows[item.item_id] = item
    phrases: dict[str, list[tuple[str, str]]] = {}
    for item in rows.values():
        phrases[item.item_id] = [(item.title, "exact_title")]
        phrases[item.item_id].extend(
            (field.value, "exact_field")
            for field in item.fields
            if _active(field.valid_from, field.valid_to, selected_at)
        )
    for alias in aliases:
        if not isinstance(alias, IdentityAlias) or alias.world_id != world_id:
            raise IdentityError("invalid_identity_world")
        if (
            alias.target_type == "item"
            and alias.target_id in rows
            and alias.status in ("canonical", "provisional")
            and _active(alias.valid_from, alias.valid_to, selected_at)
        ):
            phrases[alias.target_id].append((alias.phrase, "exact_alias"))
    # Validate the whole supplied set even when an ID is an immediate match.
    if query in rows:
        return IdentityResult("found", (IdentityCandidate(query, 1.0, "exact_id"),))
    exact: dict[str, IdentityCandidate] = {}
    close: dict[str, IdentityCandidate] = {}
    query_grams = trigrams(normalized)
    for item_id, values in phrases.items():
        for phrase, classification in values:
            candidate = normalise_phrase(phrase)
            if not candidate:
                continue
            if candidate == normalized:
                # An exact alias remains identifiable for the RRF alias boost.
                if item_id not in exact or classification == "exact_alias":
                    exact[item_id] = IdentityCandidate(item_id, 1.0, classification)
                continue
            # Initials and one/two-character labels require an explicit exact identity.
            # Shared padded trigrams alone must not turn a voice label into a person.
            if min(len(normalized), len(candidate)) < 3:
                continue
            candidate_grams = trigrams(candidate)
            score = len(query_grams & candidate_grams) / len(query_grams | candidate_grams)
            # Names permit one edit, or two from eight characters; adjacent swaps count as one.
            bound = 2 if min(len(normalized), len(candidate)) >= 8 else 1
            distance = _short_distance(normalized, candidate, bound)
            typo = distance <= bound
            if score >= CANDIDATE_THRESHOLD or typo:
                if typo:
                    score = max(score, 1 - distance / max(len(normalized), len(candidate)))
                if item_id not in close or score > close[item_id].score:
                    close[item_id] = IdentityCandidate(item_id, score, "fuzzy")
    candidates = tuple(sorted((close | exact).values(), key=lambda hit: (-hit.score, hit.item_id)))
    answer = "ambiguous" if len(candidates) > 1 else "found" if candidates else "unmatched"
    return IdentityResult(answer, candidates)
