"""Legacy encoding compatibility and complete-file refusal, without a live store."""

import copy
import json
import tracemalloc
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from grove.authority import legacy
from grove.authority.codec import PARTITIONS, DecoderBudget, decode_manifest, scan_journal
from grove.authority.records import AuthorityCodecRefused
from grove.store.catalogue import TABLES
from grove.writer.forget import validate_ledger_rows
from grove.writer.forget_ledger import _json, _name
from grove.writer.forget_plan import PLAN_KEYS, TEXT, validate_plan
from tests.privacy.test_authority_codec import Journal, canonical

WORLD = "t_legacy_00000000-0000-4000-8000-000000000001"
AT = datetime(2026, 11, 4, 12, 30, 0, 123456, tzinfo=UTC)
H = "a" * 64


def plan():
    return {key: 2 if key == "version" else [] for key in PLAN_KEYS}


def row(index=1, *, planned=True):
    result = {
        "id": f"00000000-0000-4000-8000-{index:012x}",
        "world_id": WORLD,
        "target_type": "item",
        "internal_ids": ["item-1"],
        "locators": [],
        "requested_by": "owner",
        "approved_at": AT,
        "backups_clear_after": AT.date() + timedelta(days=30),
    }
    result["receipt"] = _json(
        {
            "receipt_id": result["id"],
            "counts": {"items": 1},
            "backups_clear_after": result["backups_clear_after"],
            "downloaded_exports": "out_of_reach",
        }
    )
    if planned:
        result["plan"] = _json(plan())
    return result


def envelope(rows):
    return {
        "format": legacy.FORMAT,
        "world_id": WORLD,
        "count": len(rows),
        "rows": rows,
        "sha256": sha256(_json(rows).encode()).hexdigest(),
    }


def payload(rows):
    return _json(envelope(rows)).encode()


def decode(raw, budget=None):
    return legacy.decode_legacy(raw, world_id=WORLD, budget=budget or DecoderBudget())


def refused(raw, code=None, *, budget=None):
    owner = budget or DecoderBudget()
    before = owner.usage
    with pytest.raises(AuthorityCodecRefused) as caught:
        decode(raw, owner)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert str(caught.value) == caught.value.code
    assert owner.usage == before
    if code:
        assert caught.value.code == code


def test_empty_binding_and_immutable_exact_original_file():
    raw = payload([])
    result = decode(raw)
    assert result.binding.count == 0
    assert result.binding.file_digest == sha256(raw).hexdigest()
    assert result.binding.rows_digest == sha256(b"[]").hexdigest()
    assert result._data.obj is raw
    assert type(result._offsets) is bytes
    with pytest.raises(FrozenInstanceError):
        result.binding.count = 2


@pytest.mark.parametrize("planned", [False, True])
def test_actual_writer_encoding_and_exact_row_plan_hashes(planned):
    original = row(planned=planned)
    validate_ledger_rows(WORLD, [original])
    raw = payload([original])
    result = decode(raw)
    binding = legacy.legacy_row(result, 0)
    assert binding.id == original["id"]
    assert binding.row_digest == sha256(_json(original).encode()).hexdigest()
    assert binding.plan_version == ("v2" if planned else "legacy")
    assert binding.plan_digest == (sha256(_json(plan()).encode()).hexdigest() if planned else None)
    assert binding.extension_digest is None
    with pytest.raises(FrozenInstanceError):
        binding.id = "other"


def test_legacy_escape_unicode_surrogate_and_datetime_representations_are_not_rewritten():
    original = row()
    original["requested_by"] = 'owner-"\\é😀'
    original["internal_ids"] = ["a\nb", "\ud800", "é😀"]
    original["approved_at"] = AT.astimezone(timezone(timedelta(hours=5, minutes=30)))
    source = plan()
    source["mark_values"] = ['é😀"\\', "\ud800"]
    validate_plan(source)
    original["plan"] = _json(source)
    validate_ledger_rows(WORLD, [original])
    binding = legacy.legacy_row(decode(payload([original])), 0)
    assert binding.row_digest == sha256(_json(original).encode()).hexdigest()
    assert binding.plan_digest == sha256(_json(source).encode()).hexdigest()


def test_alternate_json_layout_and_raw_utf8_only_change_complete_file_digest():
    original = json.loads(_json(row()))
    original["requested_by"] = "owner-é😀"
    value = envelope([original])
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    result = decode(raw)
    assert result.binding.file_digest == sha256(raw).hexdigest()
    assert result.binding.rows_digest == value["sha256"]
    assert legacy.legacy_row(result, 0).row_digest == sha256(_json(original).encode()).hexdigest()


def test_tagged_dates_and_object_plan_are_valid_without_row_normalization():
    original = row()
    original["approved_at"] = {"$t": "instant", "v": AT.isoformat()}
    original["backups_clear_after"] = {"$t": "date", "v": date(2026, 12, 4).isoformat()}
    original["plan"] = plan()
    validate_ledger_rows(WORLD, [original])
    assert (
        legacy.legacy_row(decode(payload([original])), 0).row_digest
        == sha256(_json(original).encode()).hexdigest()
    )


def test_minimal_historical_external_row_is_supported():
    original = {"id": "legacy-1", "target_type": "named_value"}
    assert legacy.legacy_row(decode(payload([original])), 0).plan_version == "legacy"


def test_complete_published_catalogue_and_plan_keys_match_frozen_profile():
    expected = {
        table.name: tuple(frozenset(u.props) - {"world_id"} for u in table.uniques)
        for table in TABLES
        if table.scope == "world"
        and table.name
        not in {"change_entries", "forget_ledger", "forget_approvals", "history_values"}
    }
    assert dict(legacy.DELETE_KEYS) == expected
    assert legacy.WORLD_TABLES == {table.name for table in TABLES if table.scope == "world"}
    assert legacy.ITEM_FIELDS == TEXT["items"]
    assert set(legacy._PLAN[1]) == PLAN_KEYS


def test_every_published_plan_selector_and_span_mode():
    source = plan()
    for table, alternatives in legacy.DELETE_KEYS.items():
        for keys in alternatives:
            source["delete"].append({"table": table, "key": dict.fromkeys(keys, "synthetic-id")})
    source["hashes"] = [H]
    source["predicates"] = [{"length": 4, "sha256": H}]
    source["locators"] = ["sha256:" + H]
    source["history_fields"] = [{"id": "v1", "field": x} for x in legacy.ITEM_FIELDS]
    source["item_fields"] = copy.deepcopy(source["history_fields"])
    source["evidence_values"] = [
        {"id": "v1", "field": x} for x in ("active", "end_reason", "ended_at")
    ]
    base = {"source_id": "source-1", "source_key": "sha256:" + H}
    source["source_spans"] = [
        dict(base, mode="whole"),
        dict(base, mode="messages", message_keys=[H]),
        dict(base, mode="line", start=1, end=1, content_hash=H),
        dict(base, mode="char", start=0, end=1),
    ]
    source["source_ids"] = ["source-1"]
    validate_plan(source)
    original = row()
    original.update(target_type="source_span", plan=_json(source))
    assert (
        legacy.legacy_row(decode(payload([original])), 0).plan_digest
        == sha256(_json(source).encode()).hexdigest()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "grove.forget-ledger.v2"),
        ("world_id", "other"),
        ("count", True),
        ("count", 1.0),
        ("count", "1"),
        ("count", -1),
        ("count", 2),
        ("count", 9223372036854775808),
        ("count", 10**30),
        ("sha256", "b" * 64),
        ("unknown", {"deep": [1, 2]}),
    ],
)
def test_bad_envelope_refuses_complete_file(field, value):
    obj = envelope([row()])
    obj[field] = value
    refused(_json(obj).encode())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(id=""),
        lambda r: r.pop("id"),
        lambda r: r.update(world_id="other"),
        lambda r: r.update(unknown="private"),
        lambda r: r.update(target_type="other"),
        lambda r: r.update(internal_ids=[1]),
        lambda r: r.update(locators="not-array"),
        lambda r: r.update(requested_by=2),
        lambda r: r.update(approved_at="2026-11-04T12:00:00"),
        lambda r: r.update(approved_at=None),
        lambda r: r.update(backups_clear_after=1),
        lambda r: r.update(approved_at={"$t": "instant", "v": AT.isoformat(), "x": 1}),
        lambda r: r.update(receipt='{"private":1}'),
    ],
)
def test_invalid_late_row_never_returns_successful_prefix(mutate):
    last = row(2)
    mutate(last)
    refused(payload([row(), last]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 3),
        ("version", 4),
        ("version", True),
        ("version", 2.0),
        ("hashes", ["no"]),
        ("locators", [H]),
        ("mark_values", ["bad\nidentifier"]),
        ("predicates", [{"length": 0, "sha256": H}]),
        ("predicates", [{"length": True, "sha256": H}]),
        ("delete", [{"table": "history_values", "key": {"id": "v"}}]),
        ("delete", [{"table": "items", "key": {"name": "v"}}]),
        ("history_fields", [{"id": "v", "field": "world_id"}]),
        ("evidence_values", [{"id": "v", "field": "content"}]),
        ("source_ids", ["absent"]),
        (
            "source_spans",
            [{"mode": "whole", "source_id": "s", "source_key": "sha256:" + H, "start": 1}],
        ),
        (
            "source_spans",
            [{"mode": "char", "source_id": "s", "source_key": "sha256:" + H, "start": 1, "end": 1}],
        ),
        (
            "source_spans",
            [
                {
                    "mode": "messages",
                    "source_id": "s",
                    "source_key": "sha256:" + H,
                    "message_keys": [],
                }
            ],
        ),
    ],
)
def test_invalid_v2_fields_and_unsupported_versions_refuse(field, value):
    source = plan()
    source[field] = value
    original = row()
    original["plan"] = _json(source)
    refused(payload([original]))


def test_duplicate_rows_keys_and_nested_keys_refuse_even_with_matching_digest():
    refused(payload([row(), row()]), "identity")
    raw = payload([row()])
    refused(raw.replace(b'"count":1', b'"count":0,"count":1'), "duplicate_key")
    refused(
        raw.replace(b'"requested_by":"owner"', b'"requested_by":"other","requested_by":"owner"'),
        "duplicate_key",
    )
    source = row()
    source["plan"] = _json(plan()).replace('"version":2', '"version":2,"version":2')
    refused(payload([source]), "duplicate_key")


@pytest.mark.parametrize(
    "raw", [b"", b"[]", b"{}", b"{", b"\xef\xbb\xbf{}", b'{"count":NaN}', b'{"count":Infinity}']
)
def test_malformed_encoding_and_nonfinite_values_refuse(raw):
    refused(raw)


def test_invalid_utf8_trailing_data_and_truncation_refuse():
    raw = payload([row()])
    refused(raw.replace(b"owner", b"\xff"))
    refused(raw + b"false")
    refused(raw[:-1])


def test_input_size_scalar_array_and_operation_bounds():
    refused(b" " * (legacy.MAX_FILE + 1), "byte_limit")
    original = row()
    original["internal_ids"] = ["x" * 1025]
    refused(payload([original]), "token_limit")
    original["internal_ids"] = ["x"] * (legacy.MAX_ARRAY + 1)
    refused(payload([original]), "node_limit")
    refused(payload([row()]), "cancelled", budget=DecoderBudget(max_operations=40))


def test_small_limit_controls_prove_node_depth_row_and_count_guards(monkeypatch):
    raw = payload([row()])
    for name, value, code in (
        ("MAX_NODES", 10, "node_limit"),
        ("MAX_DEPTH", 2, "depth_limit"),
        ("MAX_ROW", 16, "noncanonical"),
        ("MAX_ROWS", 0, "node_limit"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(legacy, name, value)
            refused(raw, code)


def test_each_shared_allocation_partition_is_enforced(monkeypatch):
    for part in ("scratch", "index", "encoder"):
        budget = DecoderBudget()
        reservation = budget.reserve(part, PARTITIONS[part])
        refused(payload([row()]), "allocation_limit", budget=budget)
        reservation.release()


def test_early_refusals_do_not_hold_input_in_public_exception_frame():
    raw = payload([row()])
    budget = DecoderBudget()
    budget.cancel()
    with pytest.raises(AuthorityCodecRefused) as caught:
        decode(raw, budget)
    trace = caught.value.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_name == "decode_legacy":
            assert trace.tb_frame.f_locals["data"] is None
            assert trace.tb_frame.f_locals["root"] is None
        trace = trace.tb_next


def test_receipt_fields_are_closed_and_exactly_typed():
    for field, value in (
        ("counts", {"items": True}),
        ("counts", {"unknown": 1}),
        ("receipt_id", "other"),
        ("backups_clear_after", None),
        ("downloaded_exports", "deleted"),
    ):
        original = row()
        receipt = json.loads(original["receipt"])
        receipt[field] = value
        original["receipt"] = _json(receipt)
        refused(payload([original]))


def test_combined_budget_reservations_fail_before_admission_and_keep_prior_inputs():
    budget = DecoderBudget()
    fixture = Journal()
    journal = scan_journal(fixture.raw(), budget=budget)
    manifest = decode_manifest(canonical(fixture.manifest()), budget=budget)
    result = decode(payload([row()]), budget)
    assert len(budget._inputs) == 3
    assert journal is not None and manifest is not None and result.binding.count == 1
    before = budget.usage
    refused(payload([row(2), row(2)]), "identity", budget=budget)
    assert before == budget.usage
    remaining = PARTITIONS["input"] - dict(budget.usage)["input"]
    reservation = budget.reserve("input", remaining)
    refused(payload([]), "allocation_limit", budget=budget)
    reservation.release()
    budget.close()
    assert not budget._inputs
    with pytest.raises(AuthorityCodecRefused, match="cancelled"):
        legacy.legacy_row(result, 0)


@pytest.mark.parametrize("value", [bytearray(b"{}"), memoryview(bytearray(b"{}")), "{}", [], None])
def test_no_mutable_buffers_or_coercion(value):
    refused(value, "input_type")


def test_complete_underlying_buffer_is_counted_for_a_valid_view():
    raw = payload([])
    complete = b"prefix" + raw + b"suffix"
    budget = DecoderBudget()
    result = decode(memoryview(complete)[6:-6], budget)
    assert result._data.obj is complete
    assert dict(budget.usage)["input"] == len(complete) + 64


def test_representative_allocations_are_measured_not_maximum_conformance(capsys):
    raw = payload([row(i) for i in range(1, 17)])
    tracemalloc.start()
    budget = DecoderBudget()
    result = decode(raw, budget)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak + len(raw) < 4 * 1024 * 1024
    assert result.binding.count == 16
    with capsys.disabled():
        print(
            f"LEGACY_SAMPLE rows=16 input_bytes={len(raw)} traced_peak={peak} "
            f"including_input={peak + len(raw)}"
        )


def test_representative_combined_reader_allocation_sample(capsys):
    fixture = Journal()
    raw_journal, raw_manifest, raw_legacy = (
        fixture.raw(),
        canonical(fixture.manifest()),
        payload([row(i) for i in range(1, 17)]),
    )
    tracemalloc.start()
    budget = DecoderBudget()
    journal = scan_journal(raw_journal, budget=budget)
    manifest = decode_manifest(raw_manifest, budget=budget)
    ledger = decode(raw_legacy, budget)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    total_input = len(raw_journal) + len(raw_manifest) + len(raw_legacy)
    assert len(budget._inputs) == 3
    assert journal is not None and manifest is not None and ledger.binding.count == 16
    assert peak + total_input < 8 * 1024 * 1024
    with capsys.disabled():
        print(
            f"LEGACY_COMBINED_SAMPLE input_bytes={total_input} traced_peak={peak} "
            f"including_input={peak + total_input}"
        )


def test_filename_matches_published_encoder_reference():
    from grove.authority.legacy_reader import ledger_name

    assert ledger_name(WORLD) == _name(WORLD)


def test_legacy_row_span_is_exact_and_bounded():
    original = row()
    original["requested_by"] = "synthetic-\u00e9"
    raw = payload([original, row(2)])
    budget = DecoderBudget()
    ledger = decode(raw, budget)
    spans = [legacy.legacy_row_span(ledger, i) for i in range(2)]
    assert raw[slice(*spans[0])] == _json(original).encode()
    assert raw[slice(*spans[1])] == _json(row(2)).encode()
    assert spans[0][1] < spans[1][0]
    budget.close()
    with pytest.raises(AuthorityCodecRefused, match="cancelled"):
        legacy.legacy_row_span(ledger, 0)


@pytest.mark.parametrize("index", [True, False, -1, 1, 2**63, "0", None])
def test_legacy_row_span_invalid_or_closed_refuses(index):
    ledger = decode(payload([row()]))
    with pytest.raises(AuthorityCodecRefused) as caught:
        legacy.legacy_row_span(ledger, index)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert str(caught.value) in ("input_type", "scalar")
