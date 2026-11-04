"""Internal document readers (A.4): stable file identity, source offsets and one document.

These readers neither register a provider nor write a source. The caller must screen their
text before storage. Pages with no extracted text remain explicit image-reading work; a PDF
reader cannot prove that such a page is blank. No timestamp comes from a clock or upload.
"""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from grove.ingest.connector import (
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    ReadBudget,
    Upload,
    clean_text,
    kept_text,
    message_key,
)
from grove.source_facts import DOCUMENT_TEXT, SourceLocator, SourceTimeFacts, TextSourceFacts

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_PDF_OUTPUT_BYTES = 8 * 1024 * 1024
PDF_TIMEOUT_SECONDS = 20
_HEADING = re.compile(rb" {0,3}#{1,6}[ \t]+")
_FENCE = re.compile(rb" {0,3}(`{3,}|~{3,})")
_LINE = re.compile(rb"[^\r\n]*(?:\r\n|\r|\n|$)")


@dataclass(frozen=True)
class DocumentPart:
    key: str
    text: str = field(repr=False)
    byte_start: int | None = None
    byte_end: int | None = None
    page: int | None = None
    needs_image: bool = False
    source_facts: TextSourceFacts | None = field(default=None, repr=False)

    @property
    def locator(self) -> str:
        """An offset into the original bytes, or a one-based PDF page number."""
        if self.page is not None:
            return f"page:{self.page}"
        return f"bytes:{self.byte_start}:{self.byte_end}"


@dataclass(frozen=True)
class ParsedDocument:
    sha256: str
    parts: tuple[DocumentPart, ...]
    source_at: datetime | None = None
    parser_version: str = "document.v1"
    episode_rule: str = "document.v1"

    @property
    def pending_image_pages(self) -> tuple[int, ...]:
        return tuple(part.page for part in self.parts if part.needs_image)

    def as_chat(self, *, speaker_kind: str | None = None, sender: str | None = None) -> ParsedChat:
        """Keep all document sections in one chat. Pending image pages cannot disappear."""
        if self.pending_image_pages:
            raise NotAnExport("the document has pages that still need image reading")
        if self.parser_version == DOCUMENT_TEXT[1]:
            if speaker_kind is not None or sender is not None:
                raise NotAnExport("text source facts cannot use caller-supplied speaker metadata")
            if any(part.source_facts is None for part in self.parts):
                raise NotAnExport("the document lacks validated text source facts")
            return ParsedChat(
                f"export_digest:{self.sha256}",
                tuple(
                    ParsedMessage(part.key, part.text, None, None, source_facts=part.source_facts)
                    for part in self.parts
                ),
            )
        return ParsedChat(
            self.sha256,
            tuple(
                ParsedMessage(part.key, part.text, self.source_at, speaker_kind, sender=sender)
                for part in self.parts
            ),
            self.source_at,
            self.source_at,
        )


def _source_time(value: datetime | None) -> datetime | None:
    if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
        raise NotAnExport("the document time must include a timezone")
    return value


def _read(upload: Upload) -> bytes:
    data = upload.read_all(MAX_DOCUMENT_BYTES)
    upload.budget.take_bytes(len(data))
    if not data:
        raise NotAnExport("the document is empty")
    return data


def _sections(data: bytes, markdown: bool, budget: ReadBudget):
    """Yield original byte spans. Markdown headings outside fences start new sections."""
    start = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    cursor = start
    fence: bytes | None = None
    for found in _LINE.finditer(data, start):
        line = found[0]
        if not line:
            continue
        budget.take_record()
        marker = _FENCE.match(line) if markdown else None
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[:1] == fence[:1] and len(value) >= len(fence):
                fence = None
        blank = not markdown and not line.strip(b" \t\r\n")
        boundary = (markdown and fence is None and _HEADING.match(line) is not None) or blank
        if boundary and cursor > start:
            yield start, cursor
            start = cursor
        cursor += len(line)
        if blank:
            start = cursor
    if cursor > start:
        yield start, cursor


def parse_text(
    upload: Upload, *, markdown: bool = False, source_at: datetime | None = None
) -> ParsedDocument:
    """Read UTF-8 notes/text or Markdown. Offsets precede newline and NUL normalization."""
    source_at = _source_time(source_at)
    data = _read(upload)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise NotAnExport("the document is not UTF-8 text") from None
    digest = hashlib.sha256(data).hexdigest()
    upload.budget.take_chat()
    parts = []
    for start, end in _sections(data, markdown, upload.budget):
        text = clean_text(data[start:end].decode("utf-8")).strip()
        if not text:
            continue
        upload.budget.take_message(len(text))
        locator = SourceLocator("bytes", byte_start=start, byte_end=end)
        locator.check_extent(byte_count=len(data))
        parts.append(
            DocumentPart(
                message_key("doc", digest, "bytes", start),
                kept_text(text),
                start,
                end,
                source_facts=TextSourceFacts(digest, locator, SourceTimeFacts("absent")),
            )
        )
    if not parts:
        raise NotAnExport("the document contains no text")
    return ParsedDocument(digest, tuple(parts), source_at, DOCUMENT_TEXT[1], "document.export.v1")


def _pdf_pages(data: bytes) -> dict:
    """Run the untrusted PDF reader separately with bounded output and a wall-clock limit."""
    worker = Path(__file__).with_name("_pdf_worker.py")
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(worker)],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=PDF_TIMEOUT_SECONDS,
                env={},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise NotAnExport("the PDF reader could not complete") from None
        if result.returncode != 0 or output.tell() > MAX_PDF_OUTPUT_BYTES:
            raise NotAnExport("the PDF cannot be read within the document limits")
        output.seek(0)
        try:
            response = json.loads(output.read(MAX_PDF_OUTPUT_BYTES + 1))
        except (ValueError, UnicodeDecodeError):
            raise NotAnExport("the PDF reader returned an invalid result") from None
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("pages"), list)
        or not 0 < len(response["pages"]) <= 1000
        or any(not isinstance(page, str) for page in response["pages"])
        or sum(len(page) for page in response["pages"]) > 1_000_000
        or response.get("created_at") is not None
        and not isinstance(response["created_at"], str)
    ):
        raise NotAnExport("the PDF reader returned an invalid result")
    return response


def parse_pdf(upload: Upload, *, source_at: datetime | None = None) -> ParsedDocument:
    """Read each PDF page; retain no-text pages for image extraction by the caller."""
    source_at = _source_time(source_at)
    data = _read(upload)
    if not data.startswith(b"%PDF-"):
        raise NotAnExport("the document is not a PDF")
    result = _pdf_pages(data)
    digest = hashlib.sha256(data).hexdigest()
    upload.budget.take_chat()
    parts = []
    for page, text in enumerate(result["pages"], start=1):
        upload.budget.take_record()
        text = clean_text(text).strip()
        upload.budget.take_message(len(text))
        parts.append(
            DocumentPart(
                message_key("doc", digest, "page", page),
                kept_text(text),
                page=page,
                needs_image=not text,
            )
        )
    # This is the PDF's declared creation time, not proof of the document's assertions.
    try:
        declared = (
            datetime.fromisoformat(result["created_at"]) if result.get("created_at") else None
        )
        declared = _source_time(declared)
    except (ValueError, TypeError):
        declared = None
    return ParsedDocument(digest, tuple(parts), source_at or declared)
