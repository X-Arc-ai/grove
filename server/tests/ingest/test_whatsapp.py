"""Synthetic export-only WhatsApp inputs; no source facts come from the host."""

import hashlib
import io
import stat
import zipfile
from datetime import UTC, datetime

import pytest

from grove.ingest import NotAnExport, ReadBudget, Upload, whatsapp
from grove.ingest.whatsapp import parse_export


def upload(data, **limits):
    if isinstance(data, str):
        data = data.encode()
    return Upload(io.BytesIO(data), len(data), ReadBudget(**limits) if limits else None)


def archive(*members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in members:
            target.writestr(name, content)
    return out.getvalue()


def test_export_only_keeps_unknown_chat_timezone_and_instant():
    result = parse_export(upload("13/04/2026, 10:20 - Orla: I choose the orchard.\n"))
    message = result.messages[0]
    assert result.chat_identity is None
    assert result.identity_basis == "transcript_artifact"
    assert result.date_orders == ("dmy",)
    assert message.time.candidates[0].local_at == datetime(2026, 4, 13, 10, 20)
    assert message.time.at is None
    assert message.time.offset_minutes is None
    assert message.time.timezone_name is None
    assert message.time.uncertainties == ("offset_unknown", "timezone_unknown")
    assert message.sender == "Orla"
    assert message.speaker_kind == "third_party"
    assert message.sender_identity is None


def test_locale_is_detected_from_all_dates_without_a_host_setting():
    result = parse_export(
        upload("04/05/2026, 10:00 - Orla: A choice.\n04/13/2026, 10:00 - Cai: A later statement.\n")
    )
    assert result.date_orders == ("mdy",)
    assert [m.time.candidates[0].local_at for m in result.messages] == [
        datetime(2026, 4, 5, 10),
        datetime(2026, 4, 13, 10),
    ]


def test_ambiguous_dates_keep_both_interpretations():
    result = parse_export(upload("03/04/2026, 09:00 - Orla: A note."))
    assert result.date_orders == ("dmy", "mdy")
    assert {c.local_at for c in result.messages[0].time.candidates} == {
        datetime(2026, 4, 3, 9),
        datetime(2026, 3, 4, 9),
    }
    assert "date_order_ambiguous" in result.messages[0].time.uncertainties
    assert result.messages[0].time.at is None


def test_two_digit_year_never_uses_a_century_pivot():
    result = parse_export(upload("[13.04.26, 9:15:03 PM] Orla: A choice."))
    time = result.messages[0].time
    assert time.raw == "13.04.26, 9:15:03 PM"
    assert time.candidates[0].year_text == "26"
    assert time.candidates[0].local_at is None
    assert time.candidates[0].hour == 21
    assert "century_unknown" in time.uncertainties
    assert result.order_basis == "source_order_unresolved"


def test_year_first_and_explicit_offset_can_establish_an_instant():
    result = parse_export(upload("[2026-04-13, 12:20:03+02:00] Orla: A note."))
    time = result.messages[0].time
    assert result.date_orders == ("ymd",)
    assert time.offset_minutes == 120
    assert time.at == datetime(2026, 4, 13, 10, 20, 3, tzinfo=UTC)
    assert time.timezone_name is None
    assert result.order_basis == "source_instant"


def test_ios_narrow_space_and_12_hour_clock():
    result = parse_export(upload("\u200e[13/04/2026, 12:03\u202fAM] Orla: A note."))
    assert result.messages[0].time.candidates[0].hour == 0


def test_continuations_keep_original_byte_and_line_offsets():
    first = "\ufeff13/04/2026, 10:00 - Orla: Café.\r\nA continuation.\r\n\r\n".encode()
    second = "13/04/2026, 10:02 - Cai: 🌳.\r\n".encode()
    result = parse_export(upload(first + second))
    one, two = result.messages
    assert one.text == "Café.\nA continuation.\n"
    assert one.line_start == 1 and one.line_end == 3
    assert one.byte_start == 0 and one.byte_end == len(first)
    assert one.locator == f"line:1:bytes:0:{len(first)}"
    assert two.line_start == two.line_end == 4
    assert two.byte_start == len(first) and two.byte_end == len(first + second)
    assert result.artifact_sha256 == hashlib.sha256(first + second).hexdigest()


def test_same_artifact_reimports_identically_and_zip_wrapper_does_not_rekey():
    data = b"13/04/2026, 10:00 - Orla: The choice.\n"
    one = parse_export(upload(data))
    again = parse_export(upload(data))
    zipped = parse_export(upload(archive(("export.txt", data))))
    assert one == again
    assert zipped.artifact_sha256 == one.artifact_sha256
    assert zipped.messages == one.messages
    assert zipped.transcript_member_locator == "member:0"


def test_distinct_exports_do_not_silently_share_a_chat_or_message_namespace():
    one = parse_export(upload("13/04/2026, 10:00 - Orla: One.\n"))
    two = parse_export(upload("\n13/04/2026, 10:00 - Orla: One.\n"))
    assert one.artifact_sha256 != two.artifact_sha256
    assert one.messages[0].key != two.messages[0].key
    assert one.chat_identity is None and two.chat_identity is None
    assert one.messages[0].text_sha256 == two.messages[0].text_sha256


def test_repeated_equal_messages_in_one_minute_have_distinct_keys():
    result = parse_export(
        upload(
            "13/04/2026, 10:00:00 - Orla: A choice.\n"
            "13/04/2026, 10:00:30 - Orla: A choice.\n"
            "13/04/2026, 10:00:30 - Orla: A choice.\n"
        )
    )
    assert [m.ordinal_in_minute for m in result.messages] == [0, 1, 2]
    assert len({m.key for m in result.messages}) == 3


def test_local_time_sort_keeps_source_locators_and_stable_ties():
    result = parse_export(
        upload(
            "13/04/2026, 12:00 - Orla: Last.\n"
            "13/04/2026, 09:00 - Cai: First.\n"
            "13/04/2026, 09:00 - Orla: Second.\n"
        )
    )
    assert [m.text for m in result.messages] == ["First.", "Second.", "Last."]
    assert [m.line_start for m in result.messages] == [2, 3, 1]
    assert result.order_basis == "export_local"


def test_conflicting_date_order_sorts_preserve_source_order_as_unresolved():
    result = parse_export(
        upload(
            "04/03/2026, 10:00 - Orla: First source line.\n"
            "03/04/2026, 10:00 - Orla: Second source line.\n"
        )
    )
    assert [m.line_start for m in result.messages] == [1, 2]
    assert result.order_basis == "source_order_unresolved"
    assert len(result.windows) == 1
    assert "silence_gap_unknown" in result.windows[0].uncertainties


def test_local_windows_split_only_above_90_minutes_and_keep_local_basis():
    result = parse_export(
        upload(
            "13/04/2026, 09:00 - Orla: First.\n"
            "13/04/2026, 10:30 - Orla: At the limit.\n"
            "13/04/2026, 12:01 - Orla: New window.\n"
        )
    )
    assert [len(w.message_keys) for w in result.windows] == [2, 1]
    assert result.windows[1].start_basis == "export_local"
    assert "offset_unknown" in result.windows[1].uncertainties
    assert result.windows[1].cut_rule == "silence_90m.export_local.v1"


def test_aware_windows_use_elapsed_time_across_offset_change():
    result = parse_export(
        upload("2026-11-01, 01:00-04:00 - Orla: First.\n2026-11-01, 01:45-05:00 - Orla: Second.\n")
    )
    assert [len(w.message_keys) for w in result.windows] == [1, 1]
    assert result.windows[1].start_basis == "source_instant"


def test_unknown_century_across_days_does_not_invent_a_gap():
    result = parse_export(upload("13/04/26, 08:00 - Orla: First.\n14/04/26, 12:00 - Orla: Second."))
    assert len(result.windows) == 1
    assert "silence_gap_unknown" in result.windows[0].uncertainties


def test_named_bot_and_forwarded_words_never_establish_owner_or_original_author():
    result = parse_export(
        upload(
            "13/04/2026, 10:00 - Delivery Bot: Delivered.\n"
            "13/04/2026, 10:01 - Orla: Forwarded from Cai: Done.\n"
            "13/04/2026, 10:02 - Messages are encrypted.\n"
        )
    )
    assert [m.speaker_kind for m in result.messages] == ["third_party", "third_party", "automated"]
    assert all(m.sender_identity is None and m.original_author is None for m in result.messages)
    assert "Done." in result.messages[1].text


def test_zip_attachment_links_retain_assets_and_carrier_window():
    data = (
        "13/04/2026, 10:00 - Orla: <attached: note.opus>\n"
        "13/04/2026, 10:01 - Orla: receipt.png (file attached)\nIs this the receipt?\n"
    )
    result = parse_export(
        upload(
            archive(
                ("chat/export.txt", data),
                ("chat/note.opus", b"synthetic audio"),
                ("chat/receipt.png", b"synthetic image"),
            )
        )
    )
    assert [m.attachments[0].media_kind for m in result.messages] == ["voice", "image"]
    assert all(m.attachments[0].state == "present" for m in result.messages)
    assert [a.data for a in result.assets] == [b"synthetic audio", b"synthetic image"]
    assert (
        result.messages[0].attachments[0].sha256 == hashlib.sha256(b"synthetic audio").hexdigest()
    )
    assert len(result.windows) == 1
    assert result.windows[0].message_keys == tuple(m.key for m in result.messages)
    assert "Is this the receipt?" in result.messages[1].text


def test_missing_and_omitted_media_are_explicit_not_empty_success():
    result = parse_export(
        upload(
            "13/04/2026, 10:00 - Orla: <attached: note.opus>\n"
            "13/04/2026, 10:01 - Orla: <Media omitted>\n"
        )
    )
    assert result.messages[0].attachments[0].state == "missing"
    assert result.messages[1].attachments[0].state == "omitted"
    assert result.messages[0].attachments[0].member_locator is None


@pytest.mark.parametrize(
    "name",
    ["../escape.png", "/escape.png", "dir/../escape.png", "C:/escape.png", "dir\\escape.png"],
)
def test_archive_paths_cannot_escape_or_alias_members(name):
    data = archive(("export.txt", "13/04/2026, 10:00 - Orla: Note."), (name, b"image"))
    with pytest.raises(NotAnExport, match="unsafe"):
        parse_export(upload(data))


def test_attachment_path_is_checked_even_when_no_archive_exists():
    with pytest.raises(NotAnExport, match="unsafe"):
        parse_export(upload("13/04/2026, 10:00 - Orla: <attached: ../secret>"))


def test_attachment_does_not_resolve_by_basename_in_another_directory():
    result = parse_export(
        upload(
            archive(
                ("chat/export.txt", "13/04/2026, 10:00 - Orla: <attached: receipt.png>"),
                ("other/receipt.png", b"image"),
            )
        )
    )
    assert result.messages[0].attachments[0].state == "missing"


def test_two_exact_attachment_candidates_are_refused():
    data = archive(
        ("chat/export.txt", "13/04/2026, 10:00 - Orla: <attached: receipt.png>"),
        ("receipt.png", b"one"),
        ("chat/receipt.png", b"two"),
    )
    with pytest.raises(NotAnExport, match="more than one"):
        parse_export(upload(data))


def test_symlink_zip_member_is_refused():
    info = zipfile.ZipInfo("receipt.png")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(NotAnExport, match="nonregular"):
        parse_export(
            upload(
                archive(
                    ("export.txt", "13/04/2026, 10:00 - Orla: Note."),
                    (info, b"target"),
                )
            )
        )


def test_archive_requires_exactly_one_transcript():
    data = b"13/04/2026, 10:00 - Orla: Note."
    with pytest.raises(NotAnExport, match="exactly one"):
        parse_export(upload(archive(("one.txt", data), ("two.txt", data))))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an export",
        b"\xff",
        b"13/04/2026, 10:00 - Orla: bad\xff",
        b"31/02/2026, 10:00 - Orla: Impossible.",
        b"13/04/2026, 25:00 - Orla: Impossible.",
        b"13/04/2026, 00:00 PM - Orla: Impossible.",
        b"13/04/2026, 10:00+25:00 - Orla: Impossible.",
        b"13/04/2026, 10:00 - Orla: One.\n04/13/2026, 11:00 - Orla: Two.",
    ],
)
def test_invalid_exports_refuse_with_generic_errors(data):
    with pytest.raises(NotAnExport) as raised:
        parse_export(upload(data))
    assert "Orla" not in str(raised.value)
    assert "Impossible" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "limits",
    [
        {"max_bytes": 1},
        {"max_records": 0},
        {"max_chats": 0},
        {"max_messages": 0},
        {"max_text": 2},
    ],
)
def test_plain_reader_charges_shared_budget(limits):
    with pytest.raises(NotAnExport):
        parse_export(upload("13/04/2026, 10:00 - Orla: Note.", **limits))


def test_continuations_charge_the_text_budget():
    with pytest.raises(NotAnExport):
        parse_export(upload("13/04/2026, 10:00 - Orla: Hi\nMore text.", max_text=4))


def test_zip_members_charge_one_shared_byte_budget():
    data = archive(("export.txt", "13/04/2026, 10:00 - Orla: Note."), ("image.png", b"x" * 200))
    with pytest.raises(NotAnExport):
        parse_export(upload(data, max_bytes=100))


def test_zip_blank_transcript_probe_charges_records():
    data = archive(("blank.txt", b"\n" * 100), ("export.txt", "13/04/2026, 10:00 - Orla: Note."))
    with pytest.raises(NotAnExport):
        parse_export(upload(data, max_records=10))


def test_sender_is_bounded_before_sender_hashing():
    with pytest.raises(NotAnExport, match="1024"):
        parse_export(upload(f"13/04/2026, 10:00 - {'a' * 1025}: Note."))


def test_small_export_limit_refuses_before_hash_or_chat_retention(monkeypatch):
    monkeypatch.setattr(whatsapp, "MAX_TRANSCRIPT_BYTES", 2)
    source = upload("13/04/2026, 10:00 - Orla: Note.")
    before = source.budget.chats_left
    with pytest.raises(NotAnExport):
        parse_export(source)
    assert source.budget.chats_left == before


def test_repr_does_not_expose_source_text_or_sender():
    result = parse_export(upload("13/04/2026, 10:00 - Orla: Private source sentence."))
    assert "Private source" not in repr(result)
    assert "Orla" not in repr(result)


def test_timestamp_precision_is_not_silently_upgraded():
    result = parse_export(
        upload("13/04/2026, 10:00Z - Orla: First.\n13/04/2026, 10:00:05Z - Orla: Second.")
    )
    assert [m.time.precision for m in result.messages] == ["minute", "second"]


def test_attachment_after_capped_text_still_has_its_carrier_reference():
    data = "13/04/2026, 10:00 - Orla: " + "word " * 25_000 + "\n<attached: receipt.png>"
    result = parse_export(upload(archive(("export.txt", data), ("receipt.png", b"synthetic"))))
    assert result.messages[0].text.endswith("[cut: the message is longer than Grove keeps]")
    assert result.messages[0].attachments[0].state == "present"


def test_attachment_count_has_a_bound_before_retention(monkeypatch):
    monkeypatch.setattr(whatsapp, "MAX_ATTACHMENT_REFS", 1)
    with pytest.raises(NotAnExport, match="too many attachment"):
        parse_export(upload("13/04/2026, 10:00 - Orla: <attached: one.png>\n<attached: two.png>"))


def test_malformed_timestamp_header_cannot_be_swallowed_as_a_continuation():
    with pytest.raises(NotAnExport, match="timestamp header"):
        parse_export(
            upload(
                "13/04/2026, 10:00 - Orla: A note.\n13/04/2026, 100:00 - Cai: A malformed message."
            )
        )


@pytest.mark.parametrize(
    ("local", "offset"),
    [(datetime(1, 1, 1, 0, 0), 60), (datetime(9999, 12, 31, 23, 59), -60)],
)
def test_export_time_utc_overflow_is_a_generic_typed_refusal(local, offset):
    candidate = whatsapp.LocalTimeCandidate(
        "ymd",
        f"{local.year:04d}",
        local.month,
        local.day,
        local.hour,
        local.minute,
        local.second,
    )
    time = whatsapp.ExportTime("synthetic-private-stamp", (candidate,), offset)
    with pytest.raises(NotAnExport, match="outside the supported UTC range") as raised:
        _ = time.at
    assert "synthetic-private-stamp" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("stamp", ["0001-01-01, 00:00+01:00", "9999-12-31, 23:59-01:00"])
def test_parse_export_refuses_unrepresentable_utc_source_time(stamp):
    with pytest.raises(NotAnExport, match="outside the supported UTC range") as raised:
        parse_export(upload(f"{stamp} - Orla: A synthetic note."))
    assert stamp not in str(raised.value)
    assert "Orla" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("stamp", ["0001-01-01, 00:00Z", "9999-12-31, 23:59Z"])
def test_representable_utc_boundary_remains_accepted(stamp):
    result = parse_export(upload(f"{stamp} - Orla: A synthetic note."))
    assert result.messages[0].time.at is not None
