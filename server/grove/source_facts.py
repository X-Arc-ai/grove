"""Bounded, immutable source facts for internal text-export adapters.

These are source descriptions, not evidence authority. Decoding a description does not
register a connector or authorize a write. No raw timestamp, filename or display name is
carried here. The source instant stays in the containing message's ``at`` field.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_FACT_BYTES = 2048
MAX_POSITION = (1 << 63) - 1
WHATSAPP_TEXT = ("whatsapp", "whatsapp.export.v1")
DOCUMENT_TEXT = ("document", "document.text.v1")
DOCUMENT_PDF = ("document", "document.pdf.v1")
DOCUMENT_DOCX = ("document", "document.docx.v1")
DOCUMENT_PPTX = ("document", "document.pptx.v1")
_LOCATOR_KINDS = {
    WHATSAPP_TEXT: "line_bytes",
    DOCUMENT_TEXT: "bytes",
    DOCUMENT_PDF: "page",
    DOCUMENT_DOCX: "block",
    DOCUMENT_PPTX: "slide_block",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _integer(value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("source fact integer is outside its range")


def _code(value: object, allowed: tuple[str, ...]) -> None:
    if type(value) is not str or value not in allowed:
        raise ValueError("invalid source fact code")


def _keys(value: object, expected: tuple[str, ...]) -> dict:
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("source facts require the exact fields")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class LocalParts:
    year: int
    year_width: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    precision: str

    def __post_init__(self) -> None:
        _integer(self.year_width, 2, 4)
        if self.year_width not in (2, 4):
            raise ValueError("source year width must be two or four")
        _integer(self.year, 0 if self.year_width == 2 else 1, 99 if self.year_width == 2 else 9999)
        for value, low, high in (
            (self.month, 1, 12),
            (self.day, 1, 31),
            (self.hour, 0, 23),
            (self.minute, 0, 59),
            (self.second, 0, 59),
        ):
            _integer(value, low, high)
        _code(self.precision, ("minute", "second"))
        if self.precision == "minute" and self.second != 0:
            raise ValueError("minute precision cannot contain seconds")
        # A possible leap century validates a short year; it is never retained as its year.
        try:
            datetime(
                self.year if self.year_width == 4 else 2000 + self.year,
                self.month,
                self.day,
                self.hour,
                self.minute,
                self.second,
            )
        except ValueError:
            raise ValueError("invalid source calendar components") from None

    def local_datetime(self) -> datetime | None:
        if self.year_width == 2:
            return None
        return datetime(self.year, self.month, self.day, self.hour, self.minute, self.second)


@dataclass(frozen=True, slots=True, repr=False)
class SourceLocator:
    kind: str
    line_start: int | None = None
    line_end: int | None = None
    byte_start: int | None = None
    byte_end: int | None = None
    page: int | None = None
    block: int | None = None
    slide: int | None = None

    def __post_init__(self) -> None:
        _code(self.kind, ("line_bytes", "bytes", "page", "block", "slide_block"))
        if self.kind in ("block", "slide_block"):
            _integer(self.block, 1, MAX_POSITION)
            if self.kind == "slide_block":
                _integer(self.slide, 1, MAX_POSITION)
            elif self.slide is not None:
                raise ValueError("block locator cannot contain a slide")
            if any(
                v is not None
                for v in (self.line_start, self.line_end, self.byte_start, self.byte_end, self.page)
            ):
                raise ValueError("office locator cannot contain byte or page positions")
            return
        if self.block is not None or self.slide is not None:
            raise ValueError("legacy locator cannot contain office positions")
        if self.kind == "page":
            _integer(self.page, 1, MAX_POSITION)
            if any(
                v is not None
                for v in (self.line_start, self.line_end, self.byte_start, self.byte_end)
            ):
                raise ValueError("page locator cannot contain line or byte ranges")
            return
        if self.page is not None:
            raise ValueError("byte locator cannot contain a page")
        _integer(self.byte_start, 0, MAX_POSITION)
        _integer(self.byte_end, 1, MAX_POSITION)
        if self.byte_start >= self.byte_end:
            raise ValueError("source byte range must be nonempty and ordered")
        if self.kind == "line_bytes":
            _integer(self.line_start, 1, MAX_POSITION)
            _integer(self.line_end, 1, MAX_POSITION)
            if self.line_start > self.line_end:
                raise ValueError("source line range must be ordered")
        elif self.line_start is not None or self.line_end is not None:
            raise ValueError("byte locator cannot contain line positions")

    def check_extent(
        self,
        *,
        byte_count=None,
        line_count=None,
        page_count=None,
        block_count=None,
        slide_count=None,
    ) -> None:
        """Called by a parser while the actual artifact is still available."""
        self.__post_init__()
        if self.kind in ("block", "slide_block"):
            _integer(block_count, 1, MAX_POSITION)
            if self.block > block_count:
                raise ValueError("source block exceeds the artifact")
            if self.kind == "slide_block":
                _integer(slide_count, 1, MAX_POSITION)
                if self.slide > slide_count:
                    raise ValueError("source slide exceeds the artifact")
        elif self.kind == "page":
            _integer(page_count, 1, MAX_POSITION)
            if self.page > page_count:
                raise ValueError("source page exceeds the artifact")
        else:
            _integer(byte_count, 1, MAX_POSITION)
            if self.byte_end > byte_count:
                raise ValueError("source byte range exceeds the artifact")
            if self.kind == "line_bytes":
                _integer(line_count, 1, MAX_POSITION)
                if self.line_end > line_count:
                    raise ValueError("source line range exceeds the artifact")


@dataclass(frozen=True, slots=True, repr=False)
class SourceTimeFacts:
    status: str
    date_order_candidates: tuple[str, ...] = ()
    local_candidates: tuple[LocalParts, ...] = ()
    source_offset_minutes: int | None = None
    source_timezone: str | None = None

    def __post_init__(self) -> None:
        _code(self.status, ("instant", "local", "ambiguous", "absent"))
        if type(self.date_order_candidates) is not tuple or len(self.date_order_candidates) > 3:
            raise ValueError("source date orders must be a bounded tuple")
        for order in self.date_order_candidates:
            _code(order, ("dmy", "mdy", "ymd"))
        if len(set(self.date_order_candidates)) != len(self.date_order_candidates):
            raise ValueError("source date orders must be unique")
        if type(self.local_candidates) is not tuple or len(self.local_candidates) > 2:
            raise ValueError("source local candidates must be a bounded tuple")
        for candidate in self.local_candidates:
            if type(candidate) is not LocalParts:
                raise ValueError("source local candidate must be typed")
            candidate.__post_init__()
        if len(set(self.local_candidates)) != len(self.local_candidates):
            raise ValueError("source local candidates must be unique")
        if self.source_offset_minutes is not None:
            _integer(self.source_offset_minutes, -1439, 1439)
        if self.source_timezone is not None:
            if type(self.source_timezone) is not str or not 1 <= len(self.source_timezone) <= 128:
                raise ValueError("invalid source timezone")
            try:
                ZoneInfo(self.source_timezone)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("invalid source timezone") from None
        if self.status == "absent":
            if (
                self.local_candidates
                or self.date_order_candidates
                or self.source_offset_minutes is not None
                or self.source_timezone is not None
            ):
                raise ValueError("absent source time cannot contain time components")
            return
        if not self.local_candidates or not self.date_order_candidates:
            raise ValueError("source time requires local candidates and date orders")
        ambiguous = len(self.local_candidates) != 1 or self.local_candidates[0].year_width == 2
        if ambiguous:
            if self.status != "ambiguous":
                raise ValueError("unresolved source date must remain ambiguous")
        else:
            expected = "instant" if self._instant() is not None else "local"
            if self.status != expected:
                raise ValueError("source time status disagrees with its components")

    def _instant(self) -> datetime | None:
        local = self.local_candidates[0].local_datetime()
        if local is None:
            return None
        offset = self.source_offset_minutes
        zone = ZoneInfo(self.source_timezone) if self.source_timezone else None
        try:
            if zone is None:
                return (
                    None
                    if offset is None
                    else local.replace(tzinfo=timezone(timedelta(minutes=offset))).astimezone(UTC)
                )
            instants = set()
            for fold in (0, 1):
                zoned = local.replace(tzinfo=zone, fold=fold)
                instant = zoned.astimezone(UTC)
                if instant.astimezone(zone).replace(tzinfo=None) != local:
                    continue  # A nonexistent wall time does not establish an instant.
                if offset is None or zoned.utcoffset() == timedelta(minutes=offset):
                    instants.add(instant)
            if offset is not None and not instants:
                raise ValueError("source offset conflicts with its timezone")
            return next(iter(instants)) if len(instants) == 1 else None
        except (OverflowError, OSError):
            raise ValueError("source timestamp is outside the supported UTC range") from None

    def check_at(self, at: datetime | None) -> None:
        self.__post_init__()
        if self.status != "instant":
            if at is not None:
                raise ValueError("unresolved source time requires a null instant")
        else:
            if type(at) is not datetime or at.utcoffset() is None:
                raise ValueError("retained instant must be an aware datetime")
            try:
                normalized = at.astimezone(UTC)
            except (OverflowError, OSError):
                raise ValueError("retained instant is outside the supported UTC range") from None
            if normalized != self._instant():
                raise ValueError("retained instant disagrees with source facts")


@dataclass(frozen=True, slots=True, repr=False)
class TextSourceFacts:
    artifact_sha256: str
    locator: SourceLocator
    time: SourceTimeFacts
    contract: str = "text_export.v1"
    identity_basis: str = "export_digest"

    def __post_init__(self) -> None:
        _code(self.contract, ("text_export.v1",))
        _code(self.identity_basis, ("export_digest",))
        if (
            type(self.artifact_sha256) is not str
            or len(self.artifact_sha256) != 64
            or _DIGEST.fullmatch(self.artifact_sha256) is None
        ):
            raise ValueError("source artifact digest must be lowercase SHA-256")
        if type(self.locator) is not SourceLocator or type(self.time) is not SourceTimeFacts:
            raise ValueError("source locator and time must be typed")
        self.locator.__post_init__()
        self.time.__post_init__()
        if self.encoded_size > MAX_FACT_BYTES:
            raise ValueError("source facts exceed the encoded byte limit")

    def as_row(self) -> dict:
        # JSON round-trip changes immutable tuples into JSON arrays, without shared state.
        return json.loads(self.canonical_json())

    def canonical_json(self) -> str:
        row = asdict(self)
        if self.locator.kind not in ("block", "slide_block"):
            del row["locator"]["block"]
            del row["locator"]["slide"]
        return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def encoded_size(self) -> int:
        return len(self.canonical_json().encode("utf-8"))

    def check_at(self, at: datetime | None) -> None:
        self.__post_init__()
        self.time.check_at(at)

    @classmethod
    def from_json(cls, value: str | bytes) -> "TextSourceFacts":
        """Strict bounded decoding for future callers; this supplies no producer authority."""
        if type(value) not in (str, bytes) or len(value) > MAX_FACT_BYTES:
            raise ValueError("source facts exceed the encoded byte limit")
        try:
            if type(value) is str and len(value.encode("utf-8")) > MAX_FACT_BYTES:
                raise ValueError("source facts exceed the encoded byte limit")
            row = json.loads(value, object_pairs_hook=_unique_object)
            row = _keys(row, ("artifact_sha256", "locator", "time", "contract", "identity_basis"))
            locator = SourceLocator(
                **_keys(
                    row["locator"],
                    ("kind", "line_start", "line_end", "byte_start", "byte_end", "page")
                    + (
                        ("block", "slide")
                        if isinstance(row["locator"], dict)
                        and row["locator"].get("kind") in ("block", "slide_block")
                        else ()
                    ),
                )
            )
            time = dict(
                _keys(
                    row["time"],
                    (
                        "status",
                        "date_order_candidates",
                        "local_candidates",
                        "source_offset_minutes",
                        "source_timezone",
                    ),
                )
            )
            if (
                type(time["local_candidates"]) is not list
                or len(time["local_candidates"]) > 2
                or type(time["date_order_candidates"]) is not list
                or len(time["date_order_candidates"]) > 3
            ):
                raise ValueError("source time candidates must be bounded arrays")
            time["local_candidates"] = tuple(
                LocalParts(
                    **_keys(
                        candidate,
                        (
                            "year",
                            "year_width",
                            "month",
                            "day",
                            "hour",
                            "minute",
                            "second",
                            "precision",
                        ),
                    )
                )
                for candidate in time["local_candidates"]
            )
            time["date_order_candidates"] = tuple(time["date_order_candidates"])
            return cls(
                row["artifact_sha256"],
                locator,
                SourceTimeFacts(**time),
                row["contract"],
                row["identity_basis"],
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("invalid encoded source facts") from None


def _unique_object(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate source fact field")
        out[key] = value
    return out


def check_text_transport(identity: object, facts: TextSourceFacts, at: datetime | None) -> None:
    """Only the exact registered adapter tuples carry typed facts through the Screener."""
    if (
        type(identity) is not tuple
        or len(identity) != 2
        or any(type(part) is not str for part in identity)
        or identity not in _LOCATOR_KINDS
    ):
        raise ValueError("source facts require an approved internal adapter tuple")
    if type(facts) is not TextSourceFacts:
        raise ValueError("source facts must be typed")
    facts.check_at(at)
    if facts.locator.kind != _LOCATOR_KINDS[identity]:
        raise ValueError("source locator disagrees with its adapter")
