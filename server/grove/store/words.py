"""The clocks in words, and the answer to "did it happen" (D4 "What reads say", D5 spec 1144).

A read returns a claim's clocks so the page, or the AI host, can phrase an answer by what its
source can prove: "last confirmed 3 Mar against a file; re-check was due 3 Jun", "never confirmed,
from a chat message of 26 Oct", "planned for 12 Jul; nothing confirms it happened". A claim past
its horizon is flagged with both dates, never hidden.

The phrases are pure functions of rows and a time, so a test can pin each one without a store.
The two readers, `clock_words` and `occurrence`, only gather those rows through a WorldQuery and
read "now" from the world's clock. A source is named by its connector type in words, never by a
title or a locator, so a phrase can travel to a host without leaking what the source holds. The
code names no kind and no meaning: it reads lifecycle classes, scheduled roles and the derived
source type.
"""

import json
import re
import zoneinfo
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from grove import clock
from grove.store.current import Standing, link_standing, standings
from grove.store.query import WorldQuery
from grove.writer.provenance import connector_type_of
from grove.writer.receipts import SOURCE_WORDS

UNKNOWN = "unknown"
NOT_SET = "not set"
NOTHING_CONFIRMS = "nothing confirms it happened"
ASK_THE_PERSON = "ask_the_person"

# Fixed English names, so a phrase never depends on the process locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# How a step of the ladder reads: what that row's type lets it say about the claim.
_STEP_VERBS = {
    "planned": "planned in",
    "said": "said in",
    "reported_done": "reported in",
    "confirmed": "confirmed by",
}
_TYPE_WORDS = {
    "planned": "planned",
    "said": "said",
    "reported_done": "reported done",
    "confirmed": "confirmed",
}
_DERIVED_WORDS = {"inferred": "inferred from", "rolled_up": "rolled up from"}
_START = "start"
_DONE_ROLE = "done"
_CANCELLED_ROLE = "cancelled"
# The lifecycle classes whose state says whether something took place (D3, D5 rule 3).
_OCCURS = frozenset({"bounded_period", "record"})
# A scheduled value may be a bare ISO date: a calendar day, which has no zone to convert.
_BARE_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


# Pure phrases -----------------------------------------------------------------------------------


def zone_of(timezone: str | None) -> tzinfo:
    """An IANA code as a zone; None is UTC. A bad code raises ValueError: a caller that passes a
    zone means it, so it is never read as UTC behind its back."""
    if timezone is None or timezone == "UTC":
        return UTC
    try:
        return zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError(f"not an IANA timezone: {timezone!r}") from error


def _local(at: datetime, zone: tzinfo) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a phrase reads aware times only")
    return at.astimezone(zone)


def date_words(day: date, *, zone: tzinfo, now: datetime) -> str:
    """`12 Jul` for a calendar day, with the year only when it is not the year of now."""
    text = f"{day.day} {_MONTHS[day.month - 1]}"
    return text if day.year == _local(now, zone).year else f"{text} {day.year}"


def day_words(at: datetime, *, zone: tzinfo, now: datetime) -> str:
    """`3 Mar`, with the year only when it is not the year of now, in the zone."""
    return date_words(_local(at, zone).date(), zone=zone, now=now)


def weekday_words(at: datetime, *, zone: tzinfo, now: datetime) -> str:
    """`Thu 29 Oct`: a date ahead reads better with its weekday."""
    return f"{_WEEKDAYS[_local(at, zone).weekday()]} {day_words(at, zone=zone, now=now)}"


def _relative_day(at: datetime, zone: tzinfo, now: datetime) -> str | None:
    gap = (_local(now, zone).date() - _local(at, zone).date()).days
    return {0: "today", 1: "yesterday"}.get(gap)


def moment_words(at: datetime, *, zone: tzinfo, now: datetime) -> str:
    """`today 09:15`, `yesterday 22:40`, else `26 Oct, 22:40`."""
    clock_time = _local(at, zone).strftime("%H:%M")
    relative = _relative_day(at, zone, now)
    if relative is not None:
        return f"{relative} {clock_time}"
    return f"{day_words(at, zone=zone, now=now)}, {clock_time}"


def _when_words(at: datetime, zone: tzinfo, now: datetime) -> str:
    """`today 09:15` or `yesterday 09:15` near now, else the day alone: an old confirmation is
    read for its date, and its minute adds nothing."""
    relative = _relative_day(at, zone, now)
    if relative is not None:
        return f"{relative} {_local(at, zone).strftime('%H:%M')}"
    return day_words(at, zone=zone, now=now)


def source_words(connector_type: str | None) -> str:
    return SOURCE_WORDS.get(connector_type or "", "a source")


def clock_value_words(
    earliest: datetime | None,
    latest: datetime | None,
    precision: str | None,
    *,
    zone: tzinfo,
    now: datetime,
) -> str:
    """A world clock in words, at the precision it was stored with: `July 2026`, `before 26 Oct`,
    `12 Jul`. A bounded precision is named from the middle of its bounds, so a month stored in one
    zone still reads as that month in a neighbouring one."""
    if precision in (None, UNKNOWN) or (earliest is None and latest is None):
        return UNKNOWN
    if precision == "before":
        return f"before {day_words(latest or earliest, zone=zone, now=now)}"
    if precision == "after":
        return f"after {day_words(earliest or latest, zone=zone, now=now)}"
    low, high = earliest or latest, latest or earliest
    middle = _local(low + (high - low) / 2, zone)
    if precision in ("instant", "minute"):
        return f"{day_words(low, zone=zone, now=now)}, {_local(low, zone).strftime('%H:%M')}"
    if precision == "hour":
        start = _local(low, zone)
        end = start + timedelta(hours=1)
        return f"{day_words(low, zone=zone, now=now)}, {start:%H}:00 to {end:%H}:00"
    if precision == "day":
        return day_words(middle, zone=zone, now=now)
    if precision == "week":
        return f"the week of {day_words(low, zone=zone, now=now)}"
    if precision == "month":
        return f"{_MONTH_NAMES[middle.month - 1]} {middle.year}"
    if precision == "quarter":
        first = (middle.month - 1) // 3 * 3
        return f"{_MONTHS[first]} to {_MONTHS[first + 2]} {middle.year}"
    if precision == "year":
        return str(middle.year)
    return UNKNOWN


def last_confirmed_words(
    last_confirmed: datetime | None,
    *,
    against: str | None,
    provenance_source: str | None,
    provenance_at: datetime | None,
    recheck_by: datetime | None,
    zone: tzinfo,
    now: datetime,
) -> str:
    """D4: `today 09:15, against a fetched page`; `never confirmed, from a chat message of 26 Oct`
    (the creating row's own date); and a lapsed horizon adds `; re-check was due 3 Jun`."""
    if last_confirmed is None:
        text = "never confirmed"
        if provenance_source is not None and provenance_at is not None:
            text += f", from {provenance_source} of {day_words(provenance_at, zone=zone, now=now)}"
        elif provenance_source is not None:
            text += f", from {provenance_source}"
    else:
        text = _when_words(last_confirmed, zone, now)
        if against is not None:
            text += f", against {against}"
    if recheck_by is not None and recheck_by <= now:
        text += f"; re-check was due {day_words(recheck_by, zone=zone, now=now)}"
    return text


def how_words(recheck_how: Any) -> str | None:
    """`by checking a fetched page again` when the horizon names a connector type; `by asking
    you` for the person's own code; None when it names neither."""
    if recheck_how == ASK_THE_PERSON:
        return "by asking you"
    if isinstance(recheck_how, Mapping):
        connector = recheck_how.get("connector")
        if isinstance(connector, str) and connector in SOURCE_WORDS:
            return f"by checking {SOURCE_WORDS[connector]} again"
    return None


def recheck_words(
    recheck_by: datetime | None, *, how: str | None, zone: tzinfo, now: datetime
) -> str:
    """`Thu 29 Oct, by checking a fetched page again`, or `was due 3 Jun` once it has passed."""
    if recheck_by is None:
        return NOT_SET
    if recheck_by <= now:
        return f"was due {day_words(recheck_by, zone=zone, now=now)}"
    text = weekday_words(recheck_by, zone=zone, now=now)
    return f"{text}, {how}" if how else text


def recorded_words(
    recorded: datetime | None, *, source: str | None, zone: tzinfo, now: datetime
) -> str:
    """`26 Oct, 22:40, from a chat message`: the commit time, and the source that created it."""
    if recorded is None:
        return UNKNOWN
    text = moment_words(recorded, zone=zone, now=now)
    return f"{text}, from {source}" if source else text


@dataclass(frozen=True)
class Step:
    """One supporting row as the ladder reads it: its own type and its source in words."""

    source_type: str
    source: str


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def source_type_words(
    derived_source_type: str | None,
    derivation: str | None,
    ladder: Sequence[Step],
    premise_types: Sequence[str] = (),
) -> str:
    """`confirmed (said in a chat message; confirmed by a fetched page)`: the claim's type and how
    its supporting rows raised it, in time order. A claim inferred from other rows reads
    `inferred from 3 confirmed records` and never as confirmed (D5 rule 1)."""
    if derivation in _DERIVED_WORDS:
        counts: dict[str, int] = {}
        for source_type in premise_types:
            counts[source_type] = counts.get(source_type, 0) + 1
        if not counts:
            return f"{_DERIVED_WORDS[derivation]} other records"
        parts = [
            _plural(count, f"{_TYPE_WORDS.get(source_type, source_type)} record")
            for source_type, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]
        listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"{_DERIVED_WORDS[derivation]} {listed}"
    if derived_source_type is None:
        return UNKNOWN
    steps: list[str] = []
    for step in ladder:
        text = f"{_STEP_VERBS.get(step.source_type, step.source_type)} {step.source}"
        if not steps or steps[-1] != text:
            steps.append(text)
    head = _TYPE_WORDS.get(derived_source_type, derived_source_type)
    return f"{head} ({'; '.join(steps)})" if steps else head


# The readers ------------------------------------------------------------------------------------

_ITEM = "MATCH (i:Item {id: $id}) RETURN i"
_LINK = "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l"
_SUPPORTS = (
    "MATCH (e:Evidence) WHERE e.target_id = $id AND e.target_type = $type "
    "AND e.polarity = 'supports' AND e.active = true RETURN e"
)
_EVIDENCE = "MATCH (e:Evidence) WHERE e.id IN $ids RETURN e"
_CONNECTORS = (
    "MATCH (s:Source) WHERE s.id IN $ids MATCH (c:Connector {id: s.connector_id}) "
    "RETURN s.id AS id, c AS c"
)
_VERIFICATION = "MATCH (v:Verification {id: $id}) RETURN v.evidence_pieces AS pieces"
_NEIGHBOURS = (
    "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.active_key IS NOT NULL "
    "AND (l.from_item IN $ids OR l.to_item IN $ids) RETURN l.from_item AS a, l.to_item AS b"
)
_ITEMS = (
    "MATCH (i:Item) WHERE i.id IN $ids OPTIONAL MATCH (k:Kind {id: i.kind}) "
    "RETURN i.id AS id, i.title AS title, i.derived_source_type AS source_type, "
    "i.derivation AS derivation, i.became_true_precision AS start_precision, "
    "k.lifecycle_class AS lifecycle"
)


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _connector_types(query: WorldQuery, source_ids: Iterable[str]) -> dict[str, str]:
    ids = sorted({source_id for source_id in source_ids if source_id})
    if not ids:
        return {}
    return {row["id"]: connector_type_of(row["c"]) for row in query.read(_CONNECTORS, ids=ids)}


def _claim(query: WorldQuery, claim_id: str) -> tuple[str, dict[str, Any]] | None:
    rows = query.read(_ITEM, id=claim_id)
    if rows:
        return "item", rows[0]["i"]
    rows = query.read(_LINK, id=claim_id)
    if rows:
        return "link", rows[0]["l"]
    return None


def _ordered(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Time order: the row's own time, then when it was recorded, then its id."""
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(
        rows,
        key=lambda row: (
            _instant(row.get("source_time")) or far,
            _instant(row.get("recorded")) or far,
            str(row.get("id")),
        ),
    )


def _supports(query: WorldQuery, claim_type: str, claim_id: str) -> list[Mapping[str, Any]]:
    return _ordered(row["e"] for row in query.read(_SUPPORTS, id=claim_id, type=claim_type))


def _against(query: WorldQuery, verification_id: Any) -> str | None:
    """The source of the newest admitted piece of the verification that confirmed the claim: the
    piece whose time last confirmed speaks for."""
    if not isinstance(verification_id, str):
        return None
    rows = query.read(_VERIFICATION, id=verification_id)
    if not rows:
        return None
    pieces = [
        piece
        for piece in _json(rows[0]["pieces"]) or []
        if isinstance(piece, Mapping) and piece.get("admitted")
    ]
    timed = [(at, piece) for piece in pieces if (at := _instant(piece.get("time_spoken_for")))]
    if not timed:
        return None
    newest = max(timed, key=lambda pair: pair[0])[1]
    source_id = newest.get("source_id")
    return source_words(_connector_types(query, [source_id]).get(source_id))


def _premise_types(query: WorldQuery, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    premises = sorted({premise for row in rows for premise in row.get("premises") or []})
    if not premises:
        return []
    return [str(row["e"].get("source_type")) for row in query.read(_EVIDENCE, ids=premises)]


def _state(query: WorldQuery, claim_type: str, row: Mapping[str, Any], at: datetime) -> str:
    if claim_type == "link":
        return link_standing(row, at=at).state
    found = standings(query, [row["id"]]).get(row["id"])
    return found.state if found is not None else UNKNOWN


def clock_words(query: WorldQuery, claim_id: str, *, timezone: str = "UTC") -> dict | None:
    """The clocks of one item or link in words, at the world's clock.now, dates in `timezone`.
    None when the id is no item and no link of this world."""
    zone = zone_of(timezone)
    found = _claim(query, claim_id)
    if found is None:
        return None
    claim_type, row = found
    now = clock.now(row["world_id"])
    supports = _supports(query, claim_type, claim_id)
    provenance_id = row.get("provenance_evidence_id")
    provenance = next((e for e in supports if e.get("id") == provenance_id), None)
    if provenance is None and isinstance(provenance_id, str):
        rows = query.read(_EVIDENCE, ids=[provenance_id])
        provenance = rows[0]["e"] if rows else None
    types = _connector_types(
        query, [*(e.get("source_id") for e in supports), (provenance or {}).get("source_id")]
    )
    provenance_source = (
        source_words(types.get(provenance.get("source_id"))) if provenance is not None else None
    )
    recheck_by = _instant(row.get("recheck_by"))
    derivation = row.get("derivation")
    derived_rows = [e for e in supports if e.get("derivation") == derivation]
    ladder = [
        Step(str(e.get("source_type")), source_words(types.get(e.get("source_id"))))
        for e in supports
        if e.get("derivation") == "observed"
    ]
    return {
        "last confirmed": last_confirmed_words(
            _instant(row.get("last_confirmed")),
            against=_against(query, row.get("confirmed_against")),
            provenance_source=provenance_source,
            provenance_at=_instant((provenance or {}).get("source_time")),
            recheck_by=recheck_by,
            zone=zone,
            now=now,
        ),
        "re-check by": recheck_words(
            recheck_by, how=how_words(_json(row.get("recheck_how"))), zone=zone, now=now
        ),
        "became true": clock_value_words(
            _instant(row.get("became_true_earliest")),
            _instant(row.get("became_true_latest")),
            row.get("became_true_precision"),
            zone=zone,
            now=now,
        ),
        "recorded": recorded_words(
            _instant(row.get("recorded")), source=provenance_source, zone=zone, now=now
        ),
        "source type": source_type_words(
            row.get("derived_source_type"),
            derivation,
            ladder,
            _premise_types(query, derived_rows) if derivation in _DERIVED_WORDS else (),
        ),
        "state": _state(query, claim_type, row, now),
    }


def provenance_timezone(query: WorldQuery, item: Mapping[str, Any]) -> str:
    """The person's timezone on the item's creating row, else UTC. An unreadable code reads as UTC
    here, because a view must still show the item."""
    provenance_id = item.get("provenance_evidence_id")
    if not isinstance(provenance_id, str):
        return "UTC"
    rows = query.read(_EVIDENCE, ids=[provenance_id])
    zone = rows[0]["e"].get("person_timezone") if rows else None
    if not isinstance(zone, str):
        return "UTC"
    try:
        zone_of(zone)
    except ValueError:
        return "UTC"
    return zone


# Did it happen --------------------------------------------------------------------------------


def _bare_day(value: Any) -> date | None:
    if isinstance(value, str) and _BARE_DAY.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _scheduled_instant(value: Any) -> datetime | None:
    """The earliest instant a scheduled value names: a clock's earliest (else latest) bound, an
    ISO time, or the start of a bare day in UTC (only an order is read from it)."""
    if isinstance(value, Mapping):
        return _instant(value.get("earliest")) or _instant(value.get("latest"))
    day = _bare_day(value)
    if day is not None:
        return datetime.combine(day, time(0, 0), UTC)
    return _instant(value)


def _scheduled_words(value: Any, zone: tzinfo, now: datetime) -> str | None:
    """A scheduled value in words. A bare day reads as itself, because a day the calendar named
    is the same day in every zone the person reads it in."""
    day = _bare_day(value)
    if day is not None:
        return date_words(day, zone=zone, now=now)
    if isinstance(value, Mapping):
        words = clock_value_words(
            _instant(value.get("earliest")),
            _instant(value.get("latest")),
            value.get("precision"),
            zone=zone,
            now=now,
        )
        return None if words == UNKNOWN else words
    at = _instant(value)
    return day_words(at, zone=zone, now=now) if at is not None else None


def _newest(rows: Sequence[Mapping[str, Any]], source_type: str) -> Mapping[str, Any] | None:
    typed = [
        row
        for row in rows
        if row.get("source_type") == source_type and row.get("derivation") == "observed"
    ]
    return typed[-1] if typed else None


def _row_words(
    row: Mapping[str, Any], types: Mapping[str, str], zone: tzinfo, now: datetime
) -> str:
    """`a screenshot of 12 Jul`: the row's source in words and its own date."""
    text = source_words(types.get(row.get("source_id")))
    at = _instant(row.get("source_time"))
    return f"{text} of {day_words(at, zone=zone, now=now)}" if at is not None else text


@dataclass(frozen=True)
class _Neighbour:
    id: str
    title: str
    source_type: str | None
    derivation: str | None
    lifecycle: str | None


def _neighbours(query: WorldQuery, ids: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for row in query.read(_NEIGHBOURS, ids=list(ids)):
        found.update((row["a"], row["b"]))
    return found - set(ids)


def _scheduled_start(
    item_id: str, own: Standing, linked: Mapping[str, Standing]
) -> tuple[Any, str | None]:
    """The item's own scheduled start, else the earliest scheduled start of a linked item that
    carries one (a plan), with the id it came from."""
    if _START in own.scheduled:
        return own.scheduled[_START], item_id
    far = datetime.max.replace(tzinfo=UTC)
    held = [
        (_scheduled_instant(standing.scheduled[_START]) or far, other, standing.scheduled[_START])
        for other, standing in linked.items()
        if _START in standing.scheduled
    ]
    if not held:
        return None, None
    _, other, value = min(held)
    return value, other


def _confirmed_records(
    query: WorldQuery, item_id: str, near: Iterable[str]
) -> list[tuple[_Neighbour, datetime | None]]:
    """The confirmed records near the item (a booking linked to its plan), each with the date of
    its first confirming row, oldest first."""
    ids = sorted(set(near) - {item_id})
    if not ids:
        return []
    rows = [
        _Neighbour(
            row["id"], row["title"] or "", row["source_type"], row["derivation"], row["lifecycle"]
        )
        for row in query.read(_ITEMS, ids=ids)
    ]
    records = [
        row
        for row in rows
        if row.lifecycle == "record"
        and row.source_type == "confirmed"
        and row.derivation == "observed"
    ]
    if not records:
        return []
    held = standings(query, [row.id for row in records])
    found = []
    for record in records:
        if record.id not in held or not held[record.id].is_current:
            continue
        confirming = [
            row
            for row in _supports(query, "item", record.id)
            if row.get("source_type") == "confirmed" and row.get("derivation") == "observed"
        ]
        found.append((record, _instant(confirming[0].get("source_time")) if confirming else None))
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(found, key=lambda pair: (pair[1] or far, pair[0].id))


def occurrence(query: WorldQuery, item_id: str, *, timezone: str = "UTC") -> dict:
    """The answer to "did it happen" for a bounded period, a plan, or a record that a plan
    scheduled: {"state", "words", "scheduled", "confirmed_by"}.

    The state is current()'s. A period or a record says yes only once a confirming row gave it its
    start, and a plan only at a done status, which the writer allows only with a confirming row;
    a passing date says nothing (D5 rule 3). A planned answer names the confirmed records near the
    item, such as a booking, because a booking proves the booking and not the trip (spec 1078).
    An id that is no item of this world raises LookupError."""
    zone = zone_of(timezone)
    found = _claim(query, item_id)
    if found is None or found[0] != "item":
        raise LookupError(f"no item {item_id!r} in this world")
    item = found[1]
    now = clock.now(item["world_id"])
    own = standings(query, [item_id])[item_id]
    near = _neighbours(query, [item_id])
    linked = standings(query, sorted(near))
    value, scheduled_from = _scheduled_start(item_id, own, linked)
    planners = sorted(other for other, standing in linked.items() if _START in standing.scheduled)
    near_records = near | (_neighbours(query, planners) if planners else set())
    supports = _supports(query, "item", item_id)
    types = _connector_types(query, [row.get("source_id") for row in supports])
    [kind] = query.read(_ITEMS, ids=[item_id]) or [{"lifecycle": None}]
    occurs_by_state = kind["lifecycle"] in _OCCURS
    derived = item.get("derived_source_type")
    observed = item.get("derivation") == "observed"
    confirming = _newest(supports, "confirmed")
    when = _scheduled_words(value, zone, now) if value is not None else None

    def answer(words: str, confirmed_by: Mapping[str, Any] | None = None) -> dict:
        by = None
        if confirmed_by is not None:
            by = {
                "evidence_id": confirmed_by.get("id"),
                "source": source_words(types.get(confirmed_by.get("source_id"))),
                "at": _instant(confirmed_by.get("source_time")),
            }
        scheduled = None
        if value is not None:
            scheduled = {"value": value, "words": when, "from": scheduled_from}
        return {"state": own.state, "words": words, "scheduled": scheduled, "confirmed_by": by}

    def yes() -> dict:
        if confirming is None:
            return answer("yes")
        return answer(
            f"yes, confirmed against {_row_words(confirming, types, zone, now)}", confirming
        )

    if own.state == "ended":
        if own.status_role == _DONE_ROLE and confirming is not None:
            return yes()
        if own.status_role == _CANCELLED_ROLE:
            return answer(f"no: {item.get('status') or 'cancelled'}")
        if occurs_by_state and derived == "confirmed" and observed:
            if item.get("became_true_precision") not in (None, UNKNOWN):
                return yes()
    elif occurs_by_state and own.is_current and derived == "confirmed" and observed:
        return yes()
    elif own.state == "not_started":
        start = _instant(item.get("became_true_earliest"))
        if start is not None:
            return answer(f"not yet: starts {day_words(start, zone=zone, now=now)}")
    if derived == "reported_done" and observed:
        reported = _newest(supports, "reported_done")
        if reported is not None:
            return answer(f"reported, not confirmed: {_row_words(reported, types, zone, now)}")
    if derived == "said" and observed and value is None:
        said = _newest(supports, "said")
        if said is not None:
            return answer(f"said, not confirmed: {_row_words(said, types, zone, now)}")
    parts = [f"planned for {when}" if when else "planned"]
    for record, at in _confirmed_records(query, item_id, near_records):
        dated = f"confirmed {day_words(at, zone=zone, now=now)}" if at is not None else "confirmed"
        parts.append(f"{record.title}: {dated}")
    parts.append(NOTHING_CONFIRMS)
    return answer("; ".join(parts))
