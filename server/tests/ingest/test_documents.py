"""Synthetic document inputs, real PDF extraction, and bounded reader failures."""

import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from grove.ingest import NotAnExport, ReadBudget, Upload, documents, message_key, one_episode
from grove.ingest.documents import parse_pdf, parse_text


def upload(data, **limits):
    budget = ReadBudget(**limits) if limits else None
    return Upload(io.BytesIO(data), len(data), budget)


def pdf_bytes(*texts, created=None, encrypted=False, compressed=False):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in texts:
        page = writer.add_blank_page(width=240, height=320)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        if text is None:
            # One synthetic black pixel, with no text layer: a scanned page must remain.
            data = b"q 20 0 0 20 10 10 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x00 EI Q"
        else:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            data = f"BT /F1 12 Tf 20 20 Td ({escaped}) Tj ET".encode("ascii")
        stream.set_data(data)
        page[NameObject("/Contents")] = writer._add_object(
            stream.flate_encode() if compressed else stream
        )
    if created is not None:
        writer.add_metadata({"/CreationDate": created})
    if encrypted:
        writer.encrypt("synthetic-password")
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def test_utf8_offsets_are_original_bytes_before_newline_and_nul_cleanup():
    first = "Orla choisit le café.\r\nThe first line.\x00\r\n".encode()
    second = "A new note: 🌳.\r\n".encode()
    data = b"\xef\xbb\xbf" + first + b"\r\n" + second
    result = parse_text(upload(data))

    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert [part.text for part in result.parts] == [
        "Orla choisit le café.\nThe first line.",
        "A new note: 🌳.",
    ]
    assert [(part.byte_start, part.byte_end) for part in result.parts] == [
        (3, 3 + len(first)),
        (3 + len(first) + 2, len(data)),
    ]
    assert result.parts[0].locator == f"bytes:3:{3 + len(first)}"
    assert result.source_at is None


def test_markdown_sections_keep_blank_lines_and_fenced_headings():
    first = b"# Orchard\n\nChoose trees.\n```text\n# Not a heading\n```\n\n"
    second = b"## Budget\nKeep the total.\n"
    result = parse_text(upload(first + second), markdown=True)

    assert [part.text for part in result.parts] == [first.decode().strip(), second.decode().strip()]
    assert result.parts[1].byte_start == len(first)
    assert result.parts[1].key == message_key("doc", result.sha256, "bytes", len(first))


def test_text_reimport_identity_and_a_changed_file_get_distinct_keys():
    data = b"A first note.\n\nA second note."
    one = parse_text(upload(data))
    again = parse_text(upload(data))
    changed = parse_text(upload(data.replace(b"second", b"third")))
    assert one == again
    assert {part.key for part in one.parts}.isdisjoint(part.key for part in changed.parts)
    chat = one.as_chat(speaker_kind="person", sender="Orla")
    assert chat.chat_id == one.sha256
    assert len(one_episode(chat.messages)) == 1
    assert len(chat.messages) == 2
    assert all(
        message.sender == "Orla" and message.speaker_kind == "person" for message in chat.messages
    )


def test_source_time_preserves_its_offset_and_absence_stays_absent():
    at = datetime(2026, 2, 3, 10, tzinfo=timezone(timedelta(hours=2)))
    result = parse_text(upload(b"The note."), source_at=at)
    assert result.source_at == at
    assert result.source_at.utcoffset() == timedelta(hours=2)
    assert result.as_chat(speaker_kind="third_party").messages[0].at is at
    with pytest.raises(NotAnExport, match="timezone"):
        parse_text(upload(b"The note."), source_at=datetime(2026, 2, 3))


@pytest.mark.parametrize("data", [b"", b" \r\n\t", b"bad\xfftext"])
def test_empty_or_invalid_utf8_is_refused(data):
    with pytest.raises(NotAnExport):
        parse_text(upload(data))


@pytest.mark.parametrize(
    ("data", "limits"),
    [
        (b"a note", {"max_bytes": 2}),
        (b"a note", {"max_chats": 0}),
        (b"a\n\nb", {"max_messages": 1}),
        (b"a note", {"max_text": 2}),
        (b"\n\n\n\n", {"max_records": 2}),
    ],
)
def test_text_reader_charges_each_budget_before_retention(data, limits):
    with pytest.raises(NotAnExport, match="upload"):
        parse_text(upload(data, **limits))


def test_overlong_text_keeps_the_existing_explicit_cut_marker():
    result = parse_text(upload(b"word " * 30_000))
    assert result.parts[0].text.endswith("[cut: the message is longer than Grove keeps]")
    assert len(result.parts[0].text) < 100_100


def test_pdf_pages_have_real_text_keys_and_one_document_episode():
    data = pdf_bytes("Orla chose the orchard.", "Cai approved the plan.", compressed=True)
    result = parse_pdf(upload(data))
    assert [part.text for part in result.parts] == [
        "Orla chose the orchard.",
        "Cai approved the plan.",
    ]
    assert [part.locator for part in result.parts] == ["page:1", "page:2"]
    assert result.parts[1].key == message_key("doc", hashlib.sha256(data).hexdigest(), "page", 2)
    assert parse_pdf(upload(data)) == result
    assert result.source_at is None
    assert len(one_episode(result.as_chat(speaker_kind="third_party").messages)) == 1


def test_scanned_pdf_page_remains_pending_and_cannot_disappear_from_chat():
    result = parse_pdf(upload(pdf_bytes("First page.", None, "Last page.")))
    assert len(result.parts) == 3
    assert result.pending_image_pages == (2,)
    assert result.parts[1].text == ""
    assert result.parts[1].locator == "page:2"
    with pytest.raises(NotAnExport, match="image reading"):
        result.as_chat(speaker_kind="third_party")


def test_pdf_creation_time_is_used_only_when_aware_and_explicit_source_time_wins():
    data = pdf_bytes("A note.", created="D:20260103120000+02'00'")
    result = parse_pdf(upload(data))
    assert result.source_at == datetime(2026, 1, 3, 10, tzinfo=UTC)
    assert result.source_at.utcoffset() == timedelta(hours=2)
    at = datetime(2025, 12, 31, 8, tzinfo=UTC)
    assert parse_pdf(upload(data), source_at=at).source_at == at
    assert parse_pdf(upload(pdf_bytes("A note.", created="D:20260103120000"))).source_at is None
    assert parse_pdf(upload(pdf_bytes("A note.", created="invalid date"))).source_at is None


@pytest.mark.parametrize(
    "data", [b"not a PDF", b"%PDF-broken", pdf_bytes("Hidden.", encrypted=True)]
)
def test_invalid_and_encrypted_pdfs_are_generic_refusals(data):
    with pytest.raises(NotAnExport) as raised:
        parse_pdf(upload(data))
    assert "Hidden" not in str(raised.value)
    assert "synthetic-password" not in str(raised.value)


def test_pdf_charges_page_records_and_messages():
    data = pdf_bytes("A page.", None)
    with pytest.raises(NotAnExport, match="upload"):
        parse_pdf(upload(data, max_messages=1))
    with pytest.raises(NotAnExport, match="upload"):
        parse_pdf(upload(data, max_records=1))


def test_pdf_worker_timeout_discards_provider_details(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("untrusted-path", 20, stderr=b"private document")

    monkeypatch.setattr(documents.subprocess, "run", timeout)
    with pytest.raises(NotAnExport, match="could not complete") as raised:
        parse_pdf(upload(b"%PDF-test"))
    assert "private" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "result", [{}, {"pages": []}, {"pages": [None]}, {"pages": ["ok"], "created_at": 4}]
)
def test_pdf_worker_result_is_validated(monkeypatch, result):
    def reply(*args, **kwargs):
        kwargs["stdout"].write(json.dumps(result).encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(documents.subprocess, "run", reply)
    with pytest.raises(NotAnExport, match="invalid result"):
        parse_pdf(upload(b"%PDF-test"))


def test_pdf_worker_uses_isolation_empty_environment_and_suppressed_stderr(monkeypatch):
    def reply(command, **kwargs):
        assert command[1] == "-I"
        assert command[2].endswith("/_pdf_worker.py")
        assert kwargs["env"] == {}
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["timeout"] == documents.PDF_TIMEOUT_SECONDS
        kwargs["stdout"].write(b'{"pages":["A page."],"created_at":null}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(documents.subprocess, "run", reply)
    assert parse_pdf(upload(b"%PDF-test")).parts[0].text == "A page."


def test_pdf_decompression_limit_refuses_a_small_compressed_bomb():
    data = pdf_bytes("x" * (4 * 1024 * 1024 + 1), compressed=True)
    assert len(data) < 10_000
    with pytest.raises(NotAnExport, match="document limits"):
        parse_pdf(upload(data))


def test_document_read_limit_refuses_before_any_chat_is_retained(monkeypatch):
    monkeypatch.setattr(documents, "MAX_DOCUMENT_BYTES", 4)
    source = upload(b"a longer file")
    before = source.budget.chats_left
    with pytest.raises(NotAnExport, match="larger"):
        parse_text(source)
    assert source.budget.chats_left == before
