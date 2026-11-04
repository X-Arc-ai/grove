"""Google Calendar v3 reads: bounded pages, stable sync query, and immutable event revisions."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from grove.ingest import (
    ConnectorSpec,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    ReadBudget,
    bounded_id,
    clean_text,
    in_time_order,
    message_key,
    utc_from_iso,
)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
API_BASE = "https://www.googleapis.com/calendar/v3"
MAX_PAGES = 100
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 10000
EVENT_FIELDS = (
    "summary",
    "description",
    "location",
    "start",
    "end",
    "organizer",
    "attendees",
    "status",
    "updated",
    "recurringEventId",
    "originalStartTime",
)


class CalendarRefused(Exception):
    """A provider failure, with no credential, URL, event text, or response attached."""

    def __init__(self, reason: str = "could_not_read"):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CalendarRead:
    chats: list[ParsedChat]
    sync_token: str = field(repr=False)
    versions: dict[str, dict] = field(repr=False)
    reset: bool = False


def identifier(value: object) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise CalendarRefused("invalid_response")
    try:
        return bounded_id(value)
    except NotAnExport:
        raise CalendarRefused("invalid_response") from None


def _json(http: httpx.Client, url: str, access_token: str, params: dict) -> dict:
    try:
        with http.stream(
            "GET",
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
            follow_redirects=False,
        ) as response:
            if response.status_code == 410:
                raise CalendarRefused("expired_sync_token")
            if response.status_code in (401, 403):
                raise CalendarRefused("denied")
            if response.status_code != 200:
                raise CalendarRefused()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_PAGE_BYTES:
                    raise CalendarRefused("read_limit")
            body = json.loads(data)
    except (httpx.HTTPError, ValueError, RecursionError):
        raise CalendarRefused() from None
    if not isinstance(body, dict):
        raise CalendarRefused("invalid_response")
    return body


def _event_time(start: object, zone: str) -> datetime | None:
    if not isinstance(start, dict):
        return None
    if "dateTime" in start:
        aware = utc_from_iso(start["dateTime"])
        if aware is not None:
            return aware
        try:
            naive = datetime.fromisoformat(start["dateTime"])
            local = naive.replace(tzinfo=ZoneInfo(start.get("timeZone", zone)))
            if naive.tzinfo is not None or local.utcoffset() != local.replace(fold=1).utcoffset():
                return None
            return local
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return None
    value = start.get("date")
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _revision(event: dict, previous: dict) -> tuple[str, bool]:
    if event.get("etag"):
        return message_key("etag", identifier(event["etag"])), False
    if event.get("status") != "cancelled":
        raise CalendarRefused("invalid_response")
    # Google does not guarantee an etag on a deletion. Repeat its tombstone until a new
    # live revision appears; retain the previous index during a 410 reconciliation.
    if previous.get("tombstone"):
        return previous["version"], True
    return message_key("cancel", previous.get("version", "first")), True


def event_chat(
    calendar_id: str,
    event: dict,
    zone: str,
    budget: ReadBudget,
    changed_fields: list[str] | None = None,
    revision: str | None = None,
) -> ParsedChat:
    """An entry/revision is one episode. The source retains schedule and change time separately.

    The observation time is the scheduled entry time, never the last-modified timestamp.
    A cancellation lacking start retains an unknown event time; it cannot prove an occurrence.
    """
    event_id = identifier(event.get("id"))
    revision = revision or _revision(event, {})[0]
    key = message_key(
        "gcal",
        message_key("id", calendar_id),
        message_key("id", event_id),
        revision,
    )
    # Bound components before hashing; the intermediate fingerprint avoids separator collisions.
    chat_id = message_key("cal", message_key("id", calendar_id), message_key("id", event_id))
    status = event.get("status", "confirmed")
    if status not in ("confirmed", "tentative", "cancelled"):
        raise CalendarRefused("invalid_response")
    at = _event_time(event.get("start"), zone)
    if at is None and status != "cancelled":
        raise CalendarRefused("invalid_response")
    body = {name: event[name] for name in EVENT_FIELDS if name in event}
    body["status"] = status
    body["calendar_timezone"] = zone
    body["changed_fields"] = list(EVENT_FIELDS) if changed_fields is None else changed_fields
    body["evidence_rule"] = (
        "A calendar status is a schedule status; it does not confirm occurrence."
    )
    text = clean_text(json.dumps(body, ensure_ascii=False, sort_keys=True))
    organizer = event.get("organizer")
    sender = None
    if isinstance(organizer, dict):
        sender = clean_text(organizer.get("displayName") or organizer.get("email")) or None
    budget.take_chat()
    budget.take_message(len(text))
    messages = in_time_order([ParsedMessage(key, text, at, "third_party", sender=sender)])
    return ParsedChat(chat_id, tuple(messages))


def read_calendar(
    http: httpx.Client,
    calendar_id: str,
    access_token: str,
    *,
    sync_token: str | None = None,
    versions: dict[str, dict] | None = None,
    base_url: str = API_BASE,
    namespace: str | None = None,
) -> CalendarRead:
    """Read only the selected calendar. A 410 restarts once; no old evidence is deleted.

    Persist the returned cursor only after every event commits. A failed later page returns
    nothing, so a caller cannot confuse a partial read with an empty successful calendar.
    """
    calendar_id = identifier(calendar_id)
    key_namespace = identifier(namespace) if namespace else calendar_id
    reset = False
    for attempt in range(2):
        params = {"singleEvents": "true", "showDeleted": "true", "maxResults": "250"}
        if sync_token:
            params["syncToken"] = identifier(sync_token)
        seen_pages: set[str] = set()
        seen_events: set[str] = set()
        next_versions = {} if reset else dict(versions or {})
        chats: list[ParsedChat] = []
        budget = ReadBudget(
            max_bytes=MAX_PAGES * MAX_PAGE_BYTES, max_chats=MAX_EVENTS, max_messages=MAX_EVENTS
        )
        try:
            for _ in range(MAX_PAGES):
                body = _json(
                    http,
                    f"{base_url}/calendars/{quote(calendar_id, safe='')}/events",
                    access_token,
                    params,
                )
                events = body.get("items")
                zone = body.get("timeZone", "UTC")
                if not isinstance(events, list) or not isinstance(zone, str):
                    raise CalendarRefused("invalid_response")
                try:
                    ZoneInfo(zone)
                except (ValueError, ZoneInfoNotFoundError):
                    raise CalendarRefused("invalid_response") from None
                for event in events:
                    if not isinstance(event, dict):
                        raise CalendarRefused("invalid_response")
                    event_id = identifier(event.get("id"))
                    opaque_id = message_key("cal", event_id)
                    if opaque_id in seen_events:
                        raise CalendarRefused("invalid_response")
                    seen_events.add(opaque_id)
                    if len(seen_events) > MAX_EVENTS:
                        raise CalendarRefused("read_limit")
                    previous = next_versions.get(opaque_id, (versions or {}).get(opaque_id, {}))
                    opaque_version, tombstone = _revision(event, previous)
                    fields = {
                        name: hashlib.sha256(
                            json.dumps(event.get(name), sort_keys=True).encode()
                        ).hexdigest()
                        for name in EVENT_FIELDS
                    }
                    if previous.get("version") != opaque_version:
                        changed = [
                            name
                            for name in EVENT_FIELDS
                            if previous.get("fields", {}).get(name) != fields[name]
                        ]
                        chats.append(
                            event_chat(key_namespace, event, zone, budget, changed, opaque_version)
                        )
                    next_versions[opaque_id] = {
                        "version": opaque_version,
                        "fields": fields,
                        "tombstone": tombstone,
                    }
                    if len(next_versions) > MAX_EVENTS:
                        raise CalendarRefused("read_limit")
                page = body.get("nextPageToken")
                if page:
                    page = identifier(page)
                    if page in seen_pages:
                        raise CalendarRefused("invalid_response")
                    seen_pages.add(page)
                    params["pageToken"] = page
                else:
                    token = identifier(body.get("nextSyncToken"))
                    chats.sort(
                        key=lambda chat: (
                            chat.messages[0].at or datetime.min.replace(tzinfo=UTC),
                            chat.chat_id,
                        )
                    )
                    return CalendarRead(chats, token, next_versions, reset)
            raise CalendarRefused("read_limit")
        except CalendarRefused as error:
            if error.reason != "expired_sync_token" or not sync_token or attempt:
                raise
            sync_token, reset = None, True
        except NotAnExport:
            raise CalendarRefused("read_limit") from None
    raise CalendarRefused()


def _not_upload(_upload):
    raise NotAnExport("calendar is read only through its authorized connector")


SPEC = ConnectorSpec(
    "google_calendar",
    "calendar",
    "google_calendar.v1",
    "event_revision.v1",
    "gcal",
    _not_upload,
    cut_rule="event_revision.v1",
)
