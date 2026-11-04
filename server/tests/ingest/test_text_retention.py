"""Invented artifacts and actual bounded Screener rules; no database fixtures."""

import ast
import io
import json
import subprocess
import sys
import zipfile
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from grove.ingest import text_retention as retention
from grove.ingest.connector import Upload, chat_locator, kept_text
from grove.ingest.documents import parse_text
from grove.ingest.text_retention import (
    DOCUMENT_IDENTITY,
    WHATSAPP_IDENTITY,
    DraftScreeningRules,
    TextCountsDraft,
    TextIdentity,
    TextPartDraft,
    TextRetentionError,
    prepare_text_delta,
)
from grove.ingest.whatsapp import parse_export
from grove.screening import Message, Screener, ScreeningConfig
from grove.source_facts import WHATSAPP_TEXT, SourceLocator


def rules(**changes):
    return DraftScreeningRules(
        **(
            dict(rename_entries=(), denylist_terms=(), own_identifiers=(), sensitive_classes=())
            | changes
        )
    )


def draft(
    data=b"An invented note.", identity=DOCUMENT_IDENTITY, stored=frozenset(), **rule_changes
):
    return prepare_text_delta(
        identity, data, already_stored=stored, screening_rules=rules(**rule_changes)
    )


def transcript(*times, texts=None, sender="Nera"):
    return "".join(
        f"2026-04-13, {at} - {sender}: {text}\n"
        for at, text in zip(times, texts or ["An invented note."] * len(times), strict=True)
    ).encode()


def all_rows(delta):
    return [row for part in delta.parts for row in json.loads(part.content_json)]


def logical_cuts(delta):
    return [
        (cut.first_message_key, cut.last_message_key, cut.start_time, cut.end_time, cut.cut_rule)
        for cut in delta.cuts
    ]


def test_exact_serializer_matches_real_screened_source_and_nested_nulls():
    data = transcript("10:00Z", texts=['A quote " and a slash \\ plus é and 😀.'])
    parsed = parse_export(Upload(io.BytesIO(data), len(data))).as_chat()
    delta = draft(data, WHATSAPP_IDENTITY)
    locator = chat_locator("wa", parsed.chat_id)
    screened = Screener(ScreeningConfig()).screen_source(
        locator,
        [
            Message(m.key, m.sender, m.text, m.at, m.speaker_kind, m.modality, m.source_facts)
            for m in parsed.messages
        ],
        source_identity=WHATSAPP_TEXT,
    )
    assert delta.parts[0].content_json == screened.content()
    assert delta.parts[0].encoded_bytes == len(screened.content().encode())
    row = all_rows(delta)[0]
    assert set(row) == {"key", "sender", "text", "at", "speaker_kind", "modality", "source_facts"}
    assert row["at"] == "2026-04-13T10:00:00+00:00"
    assert row["speaker_kind"] is None and row["source_facts"]["locator"]["page"] is None
    assert row["source_facts"]["time"]["source_timezone"] is None
    legacy = Screener(ScreeningConfig()).screen_source("loc", [Message("key", None, "plain")])
    assert legacy.content() == '[{"at": null, "key": "key", "sender": null, "text": "plain"}]'


def test_fixed_markdown_mode_original_keys_and_no_document_time_or_speaker():
    data = b"First paragraph.\n\nSecond paragraph.\n\n# Section\nThird paragraph.\n"
    delta = draft(data)
    parsed = parse_text(
        Upload(io.BytesIO(data), len(data)), markdown=True, source_at=None
    ).as_chat()
    plain = parse_text(Upload(io.BytesIO(data), len(data)), markdown=False).as_chat()
    assert len(parsed.messages) == 2 and len(plain.messages) != 2
    assert delta.delta_keys == tuple(m.key for m in parsed.messages)
    assert [row["text"] for row in all_rows(delta)] == [m.text for m in parsed.messages]
    assert delta.status == "draft_only" and delta.identity == DOCUMENT_IDENTITY
    assert delta.cuts[0].cut_rule == "document.export.v1"
    assert delta.cuts[0].start_time is None and delta.cuts[0].end_time is None
    assert delta.newest_at is None
    for row in all_rows(delta):
        assert row["at"] is row["speaker_kind"] is row["sender"] is None
        assert row["source_facts"]["time"] == {
            "date_order_candidates": [],
            "local_candidates": [],
            "source_offset_minutes": None,
            "source_timezone": None,
            "status": "absent",
        }
    assert delta.counts == TextCountsDraft(1, 1, 2, 2, 2, 2, 2)


def test_repeat_and_partial_delta_keep_original_order_and_zero_counts():
    data = transcript("11:00Z", "10:00Z", "10:30Z")
    original = draft(data, WHATSAPP_IDENTITY)
    assert [row["at"] for row in all_rows(original)] == [
        "2026-04-13T11:00:00+00:00",
        "2026-04-13T10:00:00+00:00",
        "2026-04-13T10:30:00+00:00",
    ]
    repeated = draft(data, WHATSAPP_IDENTITY, stored=frozenset(original.delta_keys))
    assert repeated.status == "empty_delta"
    assert repeated.counts == TextCountsDraft(0, 0, 0, 0, 0, 0, 0)
    assert repeated.parts == repeated.cuts == repeated.delta_keys == repeated.retained_keys == ()
    assert repeated.newest_at is None
    partial = draft(data, WHATSAPP_IDENTITY, stored=frozenset(original.delta_keys[1:2]))
    assert partial.retained_keys == (original.delta_keys[0], original.delta_keys[2])
    assert partial.counts.messages_read == 2


def test_selected_transcript_bytes_define_identity_but_zip_input_refuses():
    data = transcript("10:00Z")
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("invented/chat.txt", data)
    with zipfile.ZipFile(io.BytesIO(target.getvalue())) as archive:
        selected = archive.read("invented/chat.txt")
    assert draft(selected, WHATSAPP_IDENTITY) == draft(data, WHATSAPP_IDENTITY)
    with pytest.raises(TextRetentionError):
        draft(target.getvalue(), WHATSAPP_IDENTITY)
    overlap = draft(data + transcript("11:00Z"), WHATSAPP_IDENTITY)
    first = draft(data, WHATSAPP_IDENTITY)
    assert first.artifact_sha256 != overlap.artifact_sha256
    assert first.external_locator != overlap.external_locator
    assert set(first.retained_keys).isdisjoint(overlap.retained_keys)


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04text",
        b"PK\x05\x06text",
        b"PK\x07\x08text",
        b"%PDF-fake",
        b"GIF89afake",
        b"RIFFfake",
        b"OggSfake",
        b"\x89PNGfake",
        b"\xff\xd8\xfffake",
        b"\x1f\x8bfake",
    ],
)
def test_binary_signatures_refuse_before_any_parser(monkeypatch, data):
    def forbidden(*args, **kwargs):
        pytest.fail("binary input reached parser")

    monkeypatch.setattr(retention, "parse_export", forbidden)
    monkeypatch.setattr(retention, "parse_text", forbidden)
    for identity in (WHATSAPP_IDENTITY, DOCUMENT_IDENTITY):
        with pytest.raises(TextRetentionError):
            draft(data, identity)


@pytest.mark.parametrize("data", [bytearray(b"note"), "note", b"x" * (16 * 1024 * 1024 + 1)])
def test_artifact_type_and_size_refuse_before_bytesio(monkeypatch, data):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input allocated BytesIO")

    monkeypatch.setattr(retention.io, "BytesIO", forbidden)
    with pytest.raises(TextRetentionError):
        draft(data)


@pytest.mark.parametrize("data", [b"", b"\xff", b" ", b"not a transcript"])
def test_invalid_transcript_has_a_closed_error_without_context(data):
    with pytest.raises(TextRetentionError) as error:
        draft(data, WHATSAPP_IDENTITY)
    assert str(error.value) == "invalid_text_draft"
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    ("times", "groups"),
    [
        (("10:00Z", "11:30Z"), 1),
        (("10:00Z", "11:30:01Z"), 2),
        (("11:00Z", "10:00Z", "11:30Z"), 1),
        (("11:00Z", "10:00Z", "11:31Z"), 2),
    ],
)
def test_exact_90_minute_forward_gap_and_backward_time(times, groups):
    delta = draft(transcript(*times), WHATSAPP_IDENTITY)
    assert len(delta.cuts) == groups
    assert all(c.cut_rule == "whatsapp.export_90m.v1" for c in delta.cuts)
    assert delta.cuts[0].start_time == datetime(2026, 4, 13, 10, tzinfo=UTC)
    if times[0] == "11:00Z" and groups == 2:
        assert delta.cuts[0].end_time == datetime(2026, 4, 13, 11, tzinfo=UTC)


def test_untimed_mixed_source_has_null_bounds_and_union_counts():
    delta = draft(transcript("10:00Z", "10:30", "13:00Z"), WHATSAPP_IDENTITY)
    assert len(delta.cuts) == 1 and delta.cuts[0].cut_rule == "whatsapp.export_untimed.v1"
    assert delta.cuts[0].start_time is None and delta.cuts[0].end_time is None
    assert delta.newest_at == datetime(2026, 4, 13, 13, tzinfo=UTC)
    assert delta.counts == TextCountsDraft(1, 1, 3, 3, 1, 3, 3)
    assert all(row["speaker_kind"] is None for row in all_rows(delta))


def test_real_rules_rename_mask_contacts_pay_and_drop_with_sender_context():
    data = transcript(
        "10:00Z",
        "10:30Z",
        "11:00Z",
        texts=[
            "Nera met Solen. Write to nilo@invented.example. My salary is 5000 USD.",
            "The forbidden orchard is closed.",
            "An ordinary note.",
        ],
    )
    delta = draft(
        data,
        WHATSAPP_IDENTITY,
        rename_entries=(("Nera", "Person A"), ("Solen", "Person B")),
        denylist_terms=("forbidden orchard",),
        sensitive_classes=("pay_amount",),
    )
    rows = all_rows(delta)
    assert len(rows) == 2 and delta.counts.messages_read == 3
    assert rows[0]["sender"] == "Person A"
    assert rows[0]["text"] == "Person A met Person B. Write to [email 1]. My salary is [amount]."
    assert dict(delta.parts[0].screening_counts) == {
        "contact_email": 1,
        "denylisted": 1,
        "pay_amount": 1,
        "renamed": 4,
    }
    dropped = draft(
        transcript("10:00Z", texts=["482910"], sender="Verification"), WHATSAPP_IDENTITY
    )
    assert dropped.status == "kept_out" and dropped.parts[0].screening_counts == (
        ("one_time_code", 1),
    )


def test_identical_rename_bytes_still_count_and_own_contact_stays():
    delta = draft(
        b"Nera uses nilo@invented.example.",
        rename_entries=(("Nera", "Nera"),),
        own_identifiers=("nilo@invented.example",),
    )
    assert all_rows(delta)[0]["text"] == "Nera uses nilo@invented.example."
    assert dict(delta.parts[0].screening_counts)["renamed"] >= 1
    assert "contact_email" not in dict(delta.parts[0].screening_counts)


def test_all_dropped_delta_preserves_diagnostic_part_but_no_episode():
    data = transcript("10:00Z", "12:00Z", texts=["Forbidden orchard."] * 2)
    delta = draft(data, WHATSAPP_IDENTITY, denylist_terms=("forbidden orchard",))
    assert delta.status == "kept_out"
    assert delta.retained_keys == delta.cuts == () and delta.newest_at is None
    assert delta.parts == (TextPartDraft("[]", (), 2, (("denylisted", 2),)),)
    assert delta.counts == TextCountsDraft(1, 0, 2, 0, 0, 0, 0)
    assert (
        draft(
            data,
            WHATSAPP_IDENTITY,
            stored=frozenset(delta.delta_keys),
            denylist_terms=("forbidden orchard",),
        ).status
        == "empty_delta"
    )


def test_preliminary_empty_groups_keep_counts_and_do_not_enter_cuts(monkeypatch):
    data = transcript("10:00Z", "10:01Z", "10:02Z", texts=["Forbidden orchard.", "Keep.", "Keep."])
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", 110)
    delta = draft(data, WHATSAPP_IDENTITY, denylist_terms=("forbidden orchard",))
    assert len(delta.parts) == 3
    assert delta.parts[0].content_json == "[]"
    assert delta.parts[0].screening_counts == (("denylisted", 1),)
    assert delta.cuts[0].part_indexes == (1, 2)


def test_postscreen_parts_leave_logical_cuts_and_attach_receipt_only_once(monkeypatch):
    data = transcript("10:00Z", "10:30Z", "13:00Z", texts=["Solen."] * 3)
    original = draft(data, WHATSAPP_IDENTITY, rename_entries=(("Solen", "Person B"),))
    max_row = max(
        len(json.dumps([row], sort_keys=True, ensure_ascii=False).encode())
        for row in all_rows(original)
    )
    monkeypatch.setattr(retention, "MAX_PART_BYTES", max_row)
    split = draft(data, WHATSAPP_IDENTITY, rename_entries=(("Solen", "Person B"),))
    assert len(split.parts) == 3
    assert logical_cuts(split) == logical_cuts(original)
    assert split.cuts[0].part_indexes == (0, 1) and split.cuts[1].part_indexes == (2,)
    assert split.parts[0].screening_counts == (("renamed", 3),)
    assert split.parts[1].screening_counts == split.parts[2].screening_counts == ()


def test_preliminary_splits_do_not_change_logical_cuts(monkeypatch):
    data = transcript("10:00Z", "10:30Z", "13:00Z")
    original = draft(data, WHATSAPP_IDENTITY)
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", 110)
    split = draft(data, WHATSAPP_IDENTITY)
    assert len(split.parts) == 3
    assert logical_cuts(split) == logical_cuts(original)


@pytest.mark.parametrize(
    "changes",
    [
        {"rename_entries": []},
        {"rename_entries": (("Nera", "A"),) * 33},
        {"rename_entries": (["Nera", "A"],)},
        {"rename_entries": (("Nera", "A", "extra"),)},
        {"rename_entries": (("Nera", "é" * 33),)},
        {"rename_entries": (("x" * 129, "A"),)},
        {"rename_entries": (("Nera", "\ud800"),)},
        {"denylist_terms": ("x",) * 33},
        {"denylist_terms": (True,)},
        {"own_identifiers": ("x",) * 33},
        {"own_identifiers": frozenset()},
        {"sensitive_classes": ["pay_amount"]},
        {"sensitive_classes": ("credential",)},
        {"sensitive_classes": ("pay_amount", "pay_amount")},
        {
            "rename_entries": tuple(("😀" * 128, "A" * 64) for _ in range(32)),
            "denylist_terms": ("😀" * 128,) * 32,
        },
    ],
)
def test_rule_shapes_scalars_and_aggregate_budget_refuse(changes):
    with pytest.raises(TextRetentionError, match="^invalid_screening_rules$") as error:
        rules(**changes)
    assert error.value.__context__ is None


def test_huge_replacement_refuses_before_matcher_or_screener_construction(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        pytest.fail("oversized rules reached a matcher")

    monkeypatch.setattr(retention, "RenameMap", forbidden)
    monkeypatch.setattr(retention, "Denylist", forbidden)
    monkeypatch.setattr(retention, "Screener", forbidden)
    with pytest.raises(TextRetentionError, match="^invalid_screening_rules$"):
        draft(rename_entries=(("Nera", "x" * 1_000_000),))
    damaged = rules()
    object.__setattr__(damaged, "rename_entries", (("Nera", "x" * 1_000_000),))
    with pytest.raises(TextRetentionError, match="^invalid_screening_rules$"):
        prepare_text_delta(
            DOCUMENT_IDENTITY, b"Nera", already_stored=frozenset(), screening_rules=damaged
        )
    assert calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"rename_entries": (("Nera", ""),)},
        {"rename_entries": (("Nera", "A"), ("nera", "B"))},
        {"denylist_terms": ("!!!",)},
    ],
)
def test_existing_config_errors_are_mapped_without_input(changes):
    with pytest.raises(TextRetentionError) as error:
        draft(**changes)
    assert str(error.value) == "invalid_screening_rules" and error.value.__context__ is None


def test_repeated_small_matches_expand_with_real_screener_within_exact_bytes():
    label = "L" * 64
    delta = draft(("Q " * 1000).encode(), rename_entries=(("Q", label),))
    assert all_rows(delta)[0]["text"] == (label + " ") * 999 + label
    assert delta.parts[0].screening_counts == (("renamed", 1000),)
    assert delta.parts[0].encoded_bytes == len(delta.parts[0].content_json.encode())


def test_single_prescreen_row_refuses_before_screen_source(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        pytest.fail("oversized group reached screen_source")

    monkeypatch.setattr(Screener, "screen_source", forbidden)
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft(b"word " * 7000)
    assert calls == []


def test_kept_text_cut_marker_is_preserved_without_second_truncation():
    data = b"x" * 100_001
    delta = draft(data)
    assert all_rows(delta)[0]["text"] == kept_text(data.decode())
    assert all_rows(delta)[0]["text"] == "[cut: the message is longer than Grove keeps]"


def test_single_postscreen_row_refuses_exact_byte_limit(monkeypatch):
    delta = draft('é " \\ 😀'.encode())
    monkeypatch.setattr(retention, "MAX_PART_BYTES", delta.parts[0].encoded_bytes - 1)
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft('é " \\ 😀'.encode())


def test_real_postscreen_escape_expansion_refuses_four_million_byte_row():
    # The real RenameMap permits this bounded label. JSON escaping expands NULs sixfold.
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft(("Q " * 12_000).encode(), rename_entries=(("Q", "A" + "\x00" * 63),))


def test_exact_prescreen_string_and_fact_group_bounds(monkeypatch):
    data = b"# A\nFirst.\n# B\nSecond."
    normal = draft(data)
    rows = all_rows(normal)
    units = len(normal.external_locator) + sum(len(row["key"]) + len(row["text"]) for row in rows)
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", units)
    assert len(draft(data).parts) == 1
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", units - 1)
    assert len(draft(data).parts) == 2
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", 31_250)
    parsed = parse_text(Upload(io.BytesIO(data), len(data)), markdown=True).as_chat()
    fact_bytes = sum(m.source_facts.encoded_size for m in parsed.messages)
    monkeypatch.setattr(retention, "MAX_GROUP_FACT_BYTES", fact_bytes)
    assert len(draft(data).parts) == 1
    monkeypatch.setattr(retention, "MAX_GROUP_FACT_BYTES", fact_bytes - 1)
    assert len(draft(data).parts) == 2


def test_parts_and_total_byte_limits_refuse_before_success(monkeypatch):
    data = b"# A\nFirst.\n# B\nSecond.\n# C\nThird."
    normal = draft(data)
    size = normal.parts[0].encoded_bytes
    monkeypatch.setattr(retention, "MAX_TOTAL_BYTES", size)
    assert draft(data).parts == normal.parts
    monkeypatch.setattr(retention, "MAX_TOTAL_BYTES", size - 1)
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft(data)
    monkeypatch.setattr(retention, "MAX_TOTAL_BYTES", 16_000_000)
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", 90)
    monkeypatch.setattr(retention, "MAX_PARTS", 2)
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft(data)
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", 31_250)
    max_row = max(
        len(json.dumps([row], sort_keys=True, ensure_ascii=False).encode())
        for row in all_rows(normal)
    )
    monkeypatch.setattr(retention, "MAX_PART_BYTES", max_row)
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft(data)


def test_parser_message_limit_is_real_and_no_partial_prefix_escapes(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        pytest.fail("overflow reached screen_source")

    monkeypatch.setattr(Screener, "screen_source", forbidden)
    data = "".join(f"# Section {i}\nNote.\n" for i in range(1001)).encode()
    with pytest.raises(TextRetentionError):
        draft(data)
    assert calls == []


def test_exact_64_part_boundary_and_65th_group_refusal(monkeypatch):
    monkeypatch.setattr(retention, "MAX_SCREEN_UNITS", 85)
    data = "".join(f"# {i}\nNote.\n" for i in range(64)).encode()
    accepted = draft(data)
    assert len(accepted.parts) == 64 and len(accepted.cuts) == 1
    assert accepted.cuts[0].part_indexes == tuple(range(64))
    with pytest.raises(TextRetentionError, match="^text_retention_limit$"):
        draft(data + b"# 64\nNote.\n")


def test_stored_key_limits_precede_parser_and_hashing(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid stored keys reached parser")

    monkeypatch.setattr(retention, "_parse", forbidden)
    for stored in (
        set(),
        frozenset(range(2)),
        frozenset({"x" * 1025}),
        frozenset({"\ud800"}),
        frozenset(str(i) for i in range(1001)),
        frozenset(f"{i:03d}" + "😀" * 1021 for i in range(251)),
    ):
        with pytest.raises(TextRetentionError):
            draft(stored=stored)


def test_exact_stored_key_boundary_is_accepted():
    assert draft(stored=frozenset({"k" * 1024})).status == "draft_only"


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "claude_ai"},
        {"connector_type": "past_ai_conversations"},
        {"parser_version": "document.v1"},
        {"key_prefix": "wa"},
        {"document": 1},
    ],
)
def test_identity_constructor_refuses_any_altered_field(change):
    with pytest.raises(TextRetentionError):
        replace(DOCUMENT_IDENTITY, **change)


def test_no_caller_screener_or_nonexact_identity_is_accepted():
    for config in (Screener(ScreeningConfig()), ScreeningConfig(), {}, None):
        with pytest.raises(TextRetentionError):
            prepare_text_delta(
                DOCUMENT_IDENTITY, b"note", already_stored=frozenset(), screening_rules=config
            )

    class DerivedIdentity(TextIdentity):
        pass

    with pytest.raises(TextRetentionError):
        DerivedIdentity("notes_files_releases", "document", "document.text.v1", "doc", True)
    with pytest.raises(TextRetentionError):
        draft(identity=("notes_files_releases", "document", "document.text.v1", "doc", True))


def test_frozen_records_safe_reprs_and_nested_receipts():
    delta = draft(b"Nera.", rename_entries=(("Nera", "Person A"),))
    for record in (delta, delta.identity, delta.counts, delta.parts[0], delta.cuts[0], rules()):
        assert repr(record) == type(record).__name__ + "()"
        assert not hasattr(record, "__dict__")
        name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, name, None)
    assert type(delta.parts[0].screening_counts) is tuple
    assert type(delta.parts[0].screening_counts[0]) is tuple
    with pytest.raises(TypeError):
        delta.parts[0].screening_counts[0][1] = 4
    assert not hasattr(delta, "__dict__") and not hasattr(delta, "content")


@pytest.mark.parametrize(
    "counts",
    [
        (("renamed", True),),
        (("renamed", 0),),
        (("unknown", 1),),
        (("renamed", 1), ("denylisted", 1)),
        (("renamed", 1), ("renamed", 1)),
        {"renamed": 1},
        (["renamed", 1],),
    ],
)
def test_receipt_constructor_refuses_mutable_duplicate_unsorted_or_invalid_counts(counts):
    part = draft().parts[0]
    with pytest.raises(TextRetentionError):
        replace(part, screening_counts=counts)


@pytest.mark.parametrize(
    "change",
    [
        {"parts": True},
        {"episodes": 0},
        {"messages_kept": 2},
        {"unknown_time_messages": 2},
        {"held_source_messages": 0},
    ],
)
def test_count_record_refuses_invalid_relationships(change):
    with pytest.raises(TextRetentionError):
        replace(draft().counts, **change)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "speaker",
        "modality",
        "fact_digest",
        "time",
        "fact_extra",
        "fact_oversized",
    ],
)
def test_part_constructor_rejects_invalid_derived_rows(mutation):
    part = draft().parts[0]
    rows = json.loads(part.content_json)
    row = rows[0]
    if mutation == "missing":
        del row["speaker_kind"]
    elif mutation == "extra":
        row["extra"] = "not allowed"
    elif mutation == "speaker":
        row["speaker_kind"] = "person"
    elif mutation == "modality":
        row["modality"] = "speech"
    elif mutation == "fact_digest":
        row["source_facts"]["artifact_sha256"] = "wrong"
    elif mutation == "time":
        row["at"] = "2026-04-13T10:00:00+00:00"
    elif mutation == "fact_extra":
        row["source_facts"]["extra"] = None
    elif mutation == "fact_oversized":
        row["source_facts"]["artifact_sha256"] = "x" * 3000
    content = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    with pytest.raises(TextRetentionError) as error:
        TextPartDraft(content, part.message_keys, len(content.encode()), ())
    assert error.value.__context__ is None


def test_part_constructor_refuses_duplicate_fields_noncanonical_json_and_wrong_size():
    part = draft().parts[0]
    duplicate = part.content_json.replace('"at": null', '"at": null, "at": null')
    for content, size in (
        (duplicate, len(duplicate.encode())),
        (part.content_json + " ", part.encoded_bytes + 1),
        (part.content_json, part.encoded_bytes - 1),
    ):
        with pytest.raises(TextRetentionError):
            TextPartDraft(content, part.message_keys, size, ())
    for counts in ((), (("renamed", 1),)):
        with pytest.raises(TextRetentionError):
            TextPartDraft("[]", (), 2, counts)


def test_json_allocation_guards_precede_decode(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unbounded JSON reached decoder")

    monkeypatch.setattr(retention.json, "loads", forbidden)
    for text in ("[" * 9, "[" + "[]," * 64_001 + "[]]", "x" * 4_000_001):
        with pytest.raises(TextRetentionError):
            TextPartDraft(text, (), len(text), (("denylisted", 1),))


def test_full_partition_refuses_missing_duplicate_reversed_or_wrong_part_endpoints():
    delta = draft(transcript("10:00Z", "10:30Z", "13:00Z"), WHATSAPP_IDENTITY)
    cut = delta.cuts[0]
    invalid = (
        (),
        (cut, cut),
        (replace(cut, first_message_key="absent"), delta.cuts[1]),
        (
            replace(
                cut, first_message_key=cut.last_message_key, last_message_key=cut.first_message_key
            ),
            delta.cuts[1],
        ),
        (replace(cut, part_indexes=(1,)), delta.cuts[1]),
        (replace(cut, end_time=datetime(2026, 4, 13, 12, tzinfo=UTC)), delta.cuts[1]),
    )
    for cuts in invalid:
        with pytest.raises(TextRetentionError):
            replace(delta, cuts=cuts)
    with pytest.raises(TextRetentionError):
        replace(cut, part_indexes=(True,))
    with pytest.raises(TextRetentionError):
        replace(cut, part_indexes=(0, 0))
    with pytest.raises(TextRetentionError):
        replace(cut, start_time=None)


def test_delta_constructor_refuses_forged_identity_counts_status_and_order():
    delta = draft(b"# A\nFirst.\n# B\nSecond.")
    for changes in (
        {"identity": WHATSAPP_IDENTITY},
        {"artifact_sha256": "b" * 64},
        {"external_locator": "changed"},
        {"retained_keys": delta.retained_keys[::-1]},
        {"delta_keys": delta.delta_keys[:1]},
        {"delta_keys": delta.delta_keys[::-1]},
        {"parts": delta.parts * 2},
        {"parts": list(delta.parts)},
        {"counts": replace(delta.counts, unknown_time_messages=1)},
        {"newest_at": datetime(2026, 4, 13, tzinfo=UTC)},
        {"status": "admitted"},
    ):
        with pytest.raises(TextRetentionError):
            replace(delta, **changes)


def test_empty_receipt_cannot_claim_more_dropped_messages_than_delta():
    delta = draft(b"Forbidden orchard.", denylist_terms=("forbidden orchard",))
    part = replace(delta.parts[0], screening_counts=(("denylisted", 2),))
    with pytest.raises(TextRetentionError):
        replace(delta, parts=(part,))


@pytest.mark.parametrize(
    "change", ["locator", "key", "order", "facts", "time", "duplicate", "sender"]
)
def test_screener_output_is_revalidated_before_a_draft_escapes(monkeypatch, change):
    original = Screener.screen_source

    def modified(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        messages = result.messages
        if change == "locator":
            object.__setattr__(result, "locator", "changed")
        elif change == "key":
            object.__setattr__(result, "messages", (replace(messages[0], key="changed"),))
        elif change == "order":
            object.__setattr__(result, "messages", messages[::-1])
        elif change == "duplicate":
            object.__setattr__(result, "messages", (messages[0], messages[0]))
        elif change == "facts":
            object.__setattr__(messages[0], "source_facts", None)
        elif change == "time":
            object.__setattr__(messages[0], "at", datetime(2020, 1, 1, tzinfo=UTC))
        elif change == "sender":
            object.__setattr__(messages[0], "sender", "Invented sender")
        return result

    monkeypatch.setattr(Screener, "screen_source", modified)
    with pytest.raises(TextRetentionError):
        draft(b"# A\nFirst.\n# B\nSecond.")


def test_no_environment_loader_is_used(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("environment configuration used")

    monkeypatch.setattr(ScreeningConfig, "from_environment", forbidden)
    monkeypatch.setenv("GROVE_ENV", "production")
    monkeypatch.setenv("GROVE_RENAME_MAP", "/never/read/this/path")
    assert draft().status == "draft_only"


@pytest.mark.parametrize("defect", ["digest", "mixed_tuple", "order", "duplicate"])
def test_complete_parser_output_is_checked_even_for_stored_nonmatches(monkeypatch, defect):
    data = b"# A\nFirst.\n# B\nSecond."
    parsed = parse_text(Upload(io.BytesIO(data), len(data)), markdown=True)
    stored = frozenset(part.key for part in parsed.parts)
    first, last = parsed.parts
    if defect == "digest":
        last = replace(last, source_facts=replace(last.source_facts, artifact_sha256="b" * 64))
    elif defect == "mixed_tuple":
        last = replace(
            last,
            source_facts=replace(
                last.source_facts,
                locator=SourceLocator("line_bytes", 3, 4, last.byte_start, last.byte_end),
            ),
        )
    elif defect == "duplicate":
        last = replace(last, key=first.key)
    parts = (last, first) if defect == "order" else (first, last)
    monkeypatch.setattr(
        retention, "parse_text", lambda *args, **kwargs: replace(parsed, parts=parts)
    )
    with pytest.raises(TextRetentionError):
        draft(data, stored=stored)


def test_ordinary_fresh_import_preserves_exact_four_provider_objects_and_behavior():
    code = """
import io
import grove.ingest as ingest
expected = ('chatgpt', 'claude_ai', 'claude_code', 'codex')
assert ingest.providers() == expected
before = tuple(ingest.spec_for(name) for name in expected)
def behavior():
    return tuple((s.key_prefix, ingest.message_key(s.key_prefix, 'invented'),
                  ingest.chat_locator(s.key_prefix, 'invented'), s.cut([])) for s in before)
prior = behavior()
import grove.ingest.text_retention
assert ingest.providers() == expected
assert all(ingest.spec_for(name) is obj for name, obj in zip(expected, before))
assert behavior() == prior
assert ingest.spec_for('whatsapp') is None
assert ingest.spec_for('document') is None
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_new_modules_have_no_production_consumer_or_forbidden_direct_import():
    root = Path(retention.__file__).parents[1]
    owned = {root / "source_fact_values.py", root / "ingest" / "text_retention.py"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
        if path in owned:
            assert not any(
                name.startswith(
                    (
                        "grove.writer",
                        "grove.api",
                        "grove.runner",
                        "grove.store.connection",
                        "neo4j",
                        "grove.models",
                    )
                )
                for name in modules
            )
        else:
            assert "grove.source_fact_values" not in modules
            assert "grove.ingest.text_retention" not in modules
