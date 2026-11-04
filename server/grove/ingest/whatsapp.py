"""An export-only WhatsApp reader, separate from the shared storage contract.

The transcript identifies an artifact, not a chat across exports. A local date is not a
UTC instant. This module preserves those limits rather than filling them from the host.
No provider is registered and no row or model call is created here.
"""

import hashlib
import json
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone

from grove.ingest.archive import MAX_MEMBERS, is_zip, zip_members
from grove.ingest.connector import (
    MAX_MESSAGE_CHARS,
    NotAnExport,
    Upload,
    bounded_id,
    clean_text,
    kept_text,
    message_key,
)

PARSER_VERSION = "whatsapp.export.v1"
CUT_RULE = "silence_90m.export_local.v1"
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_ATTACHMENT_REFS = 1000
# WhatsApp's numeric Android and iOS forms; the data fixes date order, never a locale guess.
_DATE = r"(?P<date>\d{1,4}(?P<sep>[/.-])\d{1,2}(?P=sep)\d{1,4})"
_TIME = (
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?"
    r"(?:[ \t\u00a0\u202f]*(?:[AaPp]\.?[Mm]\.?))?"
    r"(?:[ \t]*(?:Z|[+-]\d{2}:?\d{2}))?)"
)
_HEADER = re.compile(
    r"^[\ufeff\u200e\u200f]*"
    r"(?P<bracket>\[)?(?P<stamp>" + _DATE + r",?[ \t\u00a0\u202f]+" + _TIME + r")"
    r"(?(bracket)\][ \t]*|[ \t]+-[ \t]+)(?P<body>.*)$"
)
_HEADER_START = re.compile(r"^[\ufeff\u200e\u200f\[]*\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4},?\s+\d+:")
_TIME_PARTS = re.compile(
    r"^(\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"(?:[ \t\u00a0\u202f]*([AaPp])\.?[Mm]\.?)?"
    r"(?:[ \t]*(Z|[+-]\d{2}:?\d{2}))?$"
)
_ATTACHED = re.compile(r"<attached: ([^<>\r\n]+)>|^([^\r\n]+) \(file attached\)$", re.M)
_OMITTED = re.compile(r"^(?:<Media omitted>|(?:image|audio|video|document) omitted)$", re.I | re.M)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_VOICE_SUFFIXES = (".opus", ".ogg", ".m4a", ".aac", ".mp3", ".wav", ".amr")


@dataclass(frozen=True, slots=True)
class LocalTimeCandidate:
    """One date-order interpretation. A two-digit year has no inferred century."""

    date_order: str
    year_text: str
    month: int
    day: int
    hour: int
    minute: int
    second: int

    @property
    def local_at(self) -> datetime | None:
        if len(self.year_text) != 4:
            return None
        return datetime(
            int(self.year_text), self.month, self.day, self.hour, self.minute, self.second
        )

    @property
    def minute_token(self) -> tuple:
        return (self.year_text, self.month, self.day, self.hour, self.minute)


@dataclass(frozen=True, slots=True)
class ExportTime:
    raw: str = field(repr=False)
    candidates: tuple[LocalTimeCandidate, ...]
    offset_minutes: int | None = None
    precision: str = "minute"
    timezone_name: None = None

    @property
    def at(self) -> datetime | None:
        """An instant exists only if both the full date and source offset are established."""
        values = {c.local_at for c in self.candidates}
        if self.offset_minutes is None or None in values or len(values) != 1:
            return None
        local = values.pop()
        return local.replace(tzinfo=timezone(timedelta(minutes=self.offset_minutes))).astimezone(
            UTC
        )

    @property
    def uncertainties(self) -> tuple[str, ...]:
        out = []
        if len({(c.year_text, c.month, c.day) for c in self.candidates}) > 1:
            out.append("date_order_ambiguous")
        if any(len(c.year_text) != 4 for c in self.candidates):
            out.append("century_unknown")
        if self.offset_minutes is None:
            out.append("offset_unknown")
        out.append("timezone_unknown")
        return tuple(out)


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    name: str | None = field(repr=False)
    state: str
    member_locator: str | None = None
    sha256: str | None = None
    media_kind: str = "unknown"


@dataclass(frozen=True, slots=True)
class ExportAsset:
    """A bounded archive member in memory. This is not durable evidence retention."""

    name: str = field(repr=False)
    member_locator: str
    sha256: str
    media_kind: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExportMessage:
    key: str
    text: str = field(repr=False)
    text_sha256: str
    time: ExportTime
    sender: str | None = field(repr=False)
    speaker_kind: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    ordinal_in_minute: int
    attachments: tuple[AttachmentRef, ...] = ()
    # The text export does not attest the account owner, bot status of a named sender,
    # forwarded author, or whether a statement happened. No text keyword elevates it.
    sender_identity: None = None
    original_author: None = None
    modality: str = "text"

    @property
    def locator(self) -> str:
        return f"line:{self.line_start}:bytes:{self.byte_start}:{self.byte_end}"


@dataclass(frozen=True, slots=True)
class ExportWindow:
    message_keys: tuple[str, ...]
    start_basis: str
    uncertainties: tuple[str, ...]
    cut_rule: str = CUT_RULE


@dataclass(frozen=True, slots=True)
class WhatsAppExport:
    artifact_sha256: str
    messages: tuple[ExportMessage, ...]
    date_orders: tuple[str, ...]
    order_basis: str
    assets: tuple[ExportAsset, ...] = ()
    transcript_member_locator: str | None = None
    chat_identity: None = None
    identity_basis: str = "transcript_artifact"
    parser_version: str = PARSER_VERSION

    @property
    def windows(self) -> tuple[ExportWindow, ...]:
        return cut_windows(self.messages)


def _safe_name(name: str) -> str:
    bounded_id(name)
    if (
        not name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
        or any(ord(c) < 32 for c in name)
        or any(p in ("", ".", "..") for p in name.split("/"))
    ):
        raise NotAnExport("the archive has an unsafe member or attachment name")
    return name


def _kind(name: str) -> str:
    lower = name.lower()
    if lower.endswith(_IMAGE_SUFFIXES):
        return "image"
    if lower.endswith(_VOICE_SUFFIXES):
        return "voice"
    return "file"


def _time(raw: str, date_text: str, time_text: str, separator: str) -> ExportTime:
    fields = date_text.split(separator)
    first, middle, last = fields
    if len(first) == 4:
        layouts = (("ymd", first, middle, last),)
    elif len(last) in (2, 4):
        layouts = (("dmy", last, middle, first), ("mdy", last, first, middle))
    else:
        raise NotAnExport("the export has an unsupported date representation")
    match = _TIME_PARTS.fullmatch(time_text)
    if match is None:
        raise NotAnExport("the export has an invalid timestamp")
    hour, minute, second = (int(match[i] or "0") for i in (1, 2, 3))
    meridiem, offset = match[4], match[5]
    if meridiem:
        if not 1 <= hour <= 12:
            raise NotAnExport("the export has an invalid timestamp")
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
    if hour > 23 or minute > 59 or second > 59:
        raise NotAnExport("the export has an invalid timestamp")
    offset_minutes = None
    if offset:
        offset_minutes = 0
        if offset != "Z":
            digits = offset[1:].replace(":", "")
            hours, minutes = int(digits[:2]), int(digits[2:])
            if hours > 23 or minutes > 59:
                raise NotAnExport("the export has an invalid timestamp offset")
            offset_minutes = (hours * 60 + minutes) * (-1 if offset[0] == "-" else 1)
    candidates = []
    for order, year, month_text, day_text in layouts:
        month, day = int(month_text), int(day_text)
        try:
            # This is a validity check only. A two-digit year retains an unknown century.
            datetime(int(year) if len(year) == 4 else 2000 + int(year), month, day)
        except ValueError:
            continue
        candidates.append(LocalTimeCandidate(order, year, month, day, hour, minute, second))
    if not candidates:
        raise NotAnExport("the export has an impossible source date")
    return ExportTime(raw, tuple(candidates), offset_minutes, "second" if match[3] else "minute")


def _lines(data: bytes):
    """LF, CRLF and CR lines, with offsets in the unmodified UTF-8 artifact."""
    start = 0
    for match in re.finditer(rb"\r\n|\r|\n", data):
        yield start, match.end(), data[start : match.start()]
        start = match.end()
    if start < len(data):
        yield start, len(data), data[start:]


def _looks_like_transcript(data: bytes, upload: Upload) -> bool:
    for _, _, raw in _lines(data):
        upload.budget.take_record()
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if line.strip("\ufeff\u200e\u200f \t"):
            return _HEADER.fullmatch(line) is not None
    return False


def _attachments(text: str, assets: dict[str, ExportAsset], transcript_name: str | None):
    refs = []
    parent = (
        transcript_name.rsplit("/", 1)[0] + "/"
        if transcript_name and "/" in transcript_name
        else ""
    )
    for match in _ATTACHED.finditer(text):
        if len(refs) >= MAX_ATTACHMENT_REFS:
            raise NotAnExport("the export has too many attachment references")
        name = _safe_name(match[1] or match[2])
        # Resolve only an exact archive-relative name or a path beside the transcript.
        # Basename searches could bind a different chat's same-named attachment.
        choices = {
            assets[key].member_locator: assets[key]
            for key in (name, parent + name)
            if key in assets
        }
        if len(choices) > 1:
            raise NotAnExport("an attachment reference has more than one matching member")
        if choices:
            asset = next(iter(choices.values()))
            refs.append(
                AttachmentRef(name, "present", asset.member_locator, asset.sha256, asset.media_kind)
            )
        else:
            refs.append(AttachmentRef(name, "missing", media_kind=_kind(name)))
    if _OMITTED.search(text):
        refs.append(AttachmentRef(None, "omitted"))
    return tuple(refs)


def _ordered(messages: list[ExportMessage], orders: tuple[str, ...]):
    if all(m.time.at is not None for m in messages):
        return tuple(sorted(messages, key=lambda m: m.time.at)), "source_instant"
    permutations = []
    for order in orders:
        candidates = [next(c for c in m.time.candidates if c.date_order == order) for m in messages]
        if any(c.local_at is None for c in candidates):
            return tuple(messages), "source_order_unresolved"
        permutations.append(
            tuple(sorted(range(len(messages)), key=lambda i: candidates[i].local_at))
        )
    if len(set(permutations)) != 1:
        return tuple(messages), "source_order_unresolved"
    return tuple(messages[i] for i in permutations[0]), "export_local"


def _parse_transcript(
    data: bytes, upload: Upload, assets=(), transcript_name=None, member_locator=None
):
    if not data or len(data) > MAX_TRANSCRIPT_BYTES:
        raise NotAnExport("the transcript is empty or exceeds the format limit")
    upload.budget.take_chat()
    artifact = hashlib.sha256(data).hexdigest()
    rows = []
    assets_by_name = {asset.name: asset for asset in assets}
    attachment_count = 0
    current = None
    orders = {"dmy", "mdy", "ymd"}

    def finish():
        if current is not None:
            rows.append(current)

    for line_no, (start, end, raw) in enumerate(_lines(data), 1):
        upload.budget.take_record()
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise NotAnExport("the export is not UTF-8 text") from None
        header = _HEADER.fullmatch(line)
        if header is None and _HEADER_START.match(line):
            raise NotAnExport("the export has an unsupported timestamp header")
        reference_text = header["body"] if header else line
        if header and ": " in reference_text:
            reference_text = reference_text.partition(": ")[2]
        refs = _attachments(reference_text, assets_by_name, transcript_name)
        attachment_count += len(refs)
        if attachment_count > MAX_ATTACHMENT_REFS:
            raise NotAnExport("the export has too many attachment references")
        if header:
            finish()
            timestamp = _time(header["stamp"], header["date"], header["time"], header["sep"])
            orders.intersection_update(c.date_order for c in timestamp.candidates)
            if not orders:
                raise NotAnExport("the export has inconsistent date formats")
            body = header["body"]
            sender, marker, text = body.partition(": ")
            if not marker:
                sender, text = None, body
            elif not sender:
                raise NotAnExport("the export has an empty sender")
            else:
                bounded_id(sender)
            upload.budget.take_message(len(text))
            current = {
                "time": timestamp,
                "sender": clean_text(sender) if sender is not None else None,
                "speaker_kind": "third_party" if sender is not None else "automated",
                "line_start": line_no,
                "line_end": line_no,
                "byte_start": start,
                "byte_end": end,
                "text": clean_text(text)[: MAX_MESSAGE_CHARS + 1],
                "hash": hashlib.sha256(text.encode()),
                "chars": len(text),
                "attachments": list(refs),
            }
        elif current is None:
            if line.strip("\ufeff\u200e\u200f \t"):
                raise NotAnExport("the export has text before its first timestamp")
        else:
            # A continuation belongs to the previous message. Charge retained text before
            # append; retain at most one extra character to produce the common cut marker.
            more = "\n" + line
            before, after = (
                min(current["chars"], MAX_MESSAGE_CHARS),
                min(current["chars"] + len(more), MAX_MESSAGE_CHARS),
            )
            upload.budget.text_left -= after - before
            if upload.budget.text_left < 0:
                raise NotAnExport("the export holds more than one upload takes: upload it in parts")
            current["attachments"].extend(refs)
            current["chars"] += len(more)
            current["hash"].update(more.encode())
            space = max(0, MAX_MESSAGE_CHARS + 1 - len(current["text"]))
            current["text"] += clean_text(more)[:space]
            current["line_end"], current["byte_end"] = line_no, end
    finish()
    if not rows:
        raise NotAnExport("the file is not a WhatsApp text export")
    final_orders = tuple(sorted(orders))
    ordinals = Counter()
    messages = []
    for row in rows:
        timestamp = replace(
            row["time"],
            candidates=tuple(c for c in row["time"].candidates if c.date_order in orders),
        )
        minute = (tuple(c.minute_token for c in timestamp.candidates), timestamp.offset_minutes)
        ordinal = ordinals[minute]
        ordinals[minute] += 1
        text_hash = row["hash"].hexdigest()
        # A single JSON component avoids separator collisions without changing shared keys.
        # Hash the bounded sender separately so the complete canonical component stays small.
        sender_hash = hashlib.sha256((row["sender"] or "").encode()).hexdigest()
        identity = json.dumps(
            [timestamp.raw, sender_hash, text_hash, ordinal],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        messages.append(
            ExportMessage(
                key=message_key("wa", artifact, identity),
                text=kept_text(row["text"]),
                text_sha256=text_hash,
                time=timestamp,
                sender=row["sender"],
                speaker_kind=row["speaker_kind"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                byte_start=row["byte_start"],
                byte_end=row["byte_end"],
                ordinal_in_minute=ordinal,
                attachments=tuple(row["attachments"]),
            )
        )
    ordered, basis = _ordered(messages, final_orders)
    return WhatsAppExport(artifact, ordered, final_orders, basis, tuple(assets), member_locator)


def parse_export(upload: Upload) -> WhatsAppExport:
    """Read a UTF-8 transcript or a ZIP with exactly one transcript, without extra metadata."""
    if not is_zip(upload):
        data = upload.read_all(MAX_TRANSCRIPT_BYTES)
        upload.budget.take_bytes(len(data))
        return _parse_transcript(data, upload)
    if upload.size > MAX_ARCHIVE_BYTES:
        raise NotAnExport("the archive exceeds the format limit")
    try:
        with zipfile.ZipFile(upload.stream) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise NotAnExport("the archive has too many members")
            names = set()
            for info in infos:
                name = info.filename.rstrip("/") if info.is_dir() else info.filename
                _safe_name(name)
                if name in names or stat.S_IFMT(info.external_attr >> 16) not in (
                    0,
                    stat.S_IFREG,
                    stat.S_IFDIR,
                ):
                    raise NotAnExport("the archive has duplicate or nonregular members")
                names.add(name)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        if isinstance(error, NotAnExport):
            raise
        raise NotAnExport("the file is not a readable zip") from None
    transcript = None
    assets = []
    for index, member in enumerate(
        zip_members(
            upload,
            suffixes=("",),
            max_member_bytes=MAX_ASSET_BYTES,
            max_total_bytes=MAX_ARCHIVE_BYTES,
        )
    ):
        locator = f"member:{index}"
        if member.path.lower().endswith(".txt") and _looks_like_transcript(member.data, upload):
            if transcript is not None:
                raise NotAnExport("the archive must contain exactly one chat transcript")
            transcript = (member.data, member.path, locator)
        else:
            assets.append(
                ExportAsset(
                    member.path,
                    locator,
                    hashlib.sha256(member.data).hexdigest(),
                    _kind(member.path),
                    member.data,
                )
            )
    if transcript is None:
        raise NotAnExport("the archive has no supported chat transcript")
    return _parse_transcript(transcript[0], upload, assets, transcript[1], transcript[2])


def _gap(previous: ExportTime, current: ExportTime) -> tuple[float | None, str]:
    if previous.at is not None and current.at is not None:
        return (current.at - previous.at).total_seconds(), "source_instant"
    differences = []
    for left in previous.candidates:
        for right in current.candidates:
            if left.date_order != right.date_order:
                continue
            if left.local_at is not None and right.local_at is not None:
                differences.append((right.local_at - left.local_at).total_seconds())
            elif (left.year_text, left.month, left.day) == (
                right.year_text,
                right.month,
                right.day,
            ):
                differences.append(
                    (right.hour - left.hour) * 3600
                    + (right.minute - left.minute) * 60
                    + right.second
                    - left.second
                )
            else:
                return None, "unresolved"
    if not differences or len(set(differences)) != 1:
        return None, "unresolved"
    return differences[0], "export_local"


def cut_windows(messages: tuple[ExportMessage, ...]) -> tuple[ExportWindow, ...]:
    """Cut only established gaps over 90 minutes; unknown gaps remain explicitly unresolved.

    Export-local cuts describe the printed wall clock, not elapsed time across an unknown
    timezone transition. Attachments stay on their carrier's key and therefore its window.
    """
    windows = []
    keys = []
    uncertainties = set()
    start_basis = "export_start"
    previous = None
    for message in messages:
        if previous is not None:
            gap, basis = _gap(previous.time, message.time)
            if gap is not None and gap > 90 * 60:
                windows.append(ExportWindow(tuple(keys), start_basis, tuple(sorted(uncertainties))))
                keys, uncertainties, start_basis = [], set(), basis
            elif gap is None or gap < 0:
                uncertainties.add("silence_gap_unknown")
        uncertainties.update(message.time.uncertainties)
        keys.append(message.key)
        previous = message
    if keys:
        windows.append(ExportWindow(tuple(keys), start_basis, tuple(sorted(uncertainties))))
    return tuple(windows)
