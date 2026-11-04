"""Immediate pure source checks; run with --noconftest, without a database."""

import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove.cleanup.access import CleanupRefused
from grove.cleanup.legwork import LegworkAttemptV1, NoSourceDeferralV1, validate_deferral
from grove.cleanup.question_intents import (
    CuratorQuestionIntentV1,
    IntentPartV1,
    ResolutionTransferV1,
    validate_accounting,
    validate_transfer,
)
from grove.cleanup.question_lifecycle import (
    MAX_ANSWER_BYTES,
    MAX_RECORD_BYTES,
    MAX_VERSION,
    AnswerCommandV1,
    PlanV1,
    QuestionApplicationV1,
    QuestionBindingV1,
    SourceBindingV1,
    TargetV1,
    answer_text,
    canonical_json,
    decode_json,
    validate_coverage,
)
from grove.cleanup.runtime import require_runtime
from grove.store.catalogue import B1_GROUPS, table

AT = datetime(2026, 11, 4, tzinfo=UTC)


def source(key="m1"):
    return SourceBindingV1("source1", 1, "episode1", 2, key, f"key:{key}", AT)


def target():
    return TargetV1("item", "item1", 3)


def plan(action="confirm", obligations=("work1",)):
    return PlanV1(
        "plan1", action, obligations, (target(),), "payload1" if action == "apply_batch" else None
    )


def binding():
    return QuestionBindingV1("question1", 1, "topic1", (target(),), (plan(),), ("legwork1",))


def command():
    return AnswerCommandV1(
        "request1", "question1", 1, "plan1", "choose_option", source(), "person1"
    )


def application():
    return QuestionApplicationV1(
        "application1",
        "request1",
        "question1",
        1,
        "person1",
        source(),
        "plan1",
        "commit1",
        "changes1",
        ("work1",),
        (),
        (),
        (("item", "item1", 3, 4, "op1"),),
    )


def legwork():
    return LegworkAttemptV1(
        "legwork1", "work1", "run1", target(), "no_match", ("scope1",), 1, "authority1", "reason1"
    )


def deferral():
    return NoSourceDeferralV1(
        "attempt1",
        "work1",
        "run1",
        target(),
        ("legwork1",),
        "reason1",
        AT + timedelta(days=1),
        "check_sources",
    )


def part(number=1):
    return IntentPartV1(
        f"disposition{number}",
        f"work{number}",
        source(f"m{number}"),
        f"waiting{number}",
        f"payload{number}",
        1,
        "unresolved",
    )


def intent():
    return CuratorQuestionIntentV1(
        "intent1",
        "world1",
        "run1",
        "request1",
        "execution1",
        1,
        "group1",
        (part(), part(2)),
        (target(),),
        ("legwork1",),
    )


def transfer():
    return ResolutionTransferV1(
        "transfer1", "application1", "question1", 1, source(), ("work1",), (target(),)
    )


FACTORIES = (
    target,
    source,
    plan,
    binding,
    command,
    application,
    legwork,
    deferral,
    part,
    intent,
    transfer,
)


@pytest.mark.parametrize("factory", FACTORIES)
def test_every_record_is_frozen_and_round_trips(factory):
    record = factory()
    first = fields(record)[0].name
    with pytest.raises(FrozenInstanceError):
        setattr(record, first, "changed")
    assert not hasattr(record, "__dict__")
    wire = record.to_json()
    assert type(wire) is dict
    assert type(record).from_json(wire) == record
    encoded = canonical_json(record)
    assert decode_json(type(record), encoded) == record
    assert decode_json(type(record), encoded.encode()) == record
    assert encoded == json.dumps(wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    wire[first] = "changed"
    assert getattr(record, first) != "changed"
    with pytest.raises(CleanupRefused):
        type(record).from_json({**record.to_json(), "unknown": "x"})
    missing = record.to_json()
    missing.pop(first)
    with pytest.raises(CleanupRefused):
        type(record).from_json(missing)


def test_nested_types_and_detachment_in_both_directions():
    wire = intent().to_json()
    decoded = CuratorQuestionIntentV1.from_json(wire)
    wire["parts"][0]["source"]["source_id"] = "changed"
    wire["targets"].clear()
    assert decoded == intent()
    assert type(decoded.parts) is tuple
    assert type(decoded.parts[0]) is IntentPartV1
    assert type(decoded.parts[0].source) is SourceBindingV1
    assert type(decoded.targets[0]) is TargetV1
    assert type(decoded.parts[0].source.source_time) is datetime
    assert type(binding().to_json()["plans"][0]["targets"]) is list
    assert type(QuestionBindingV1.from_json(binding().to_json()).plans[0]) is PlanV1
    evidence = replace(legwork(), result="evidence_found", source_refs=(source(),))
    assert type(LegworkAttemptV1.from_json(evidence.to_json()).source_refs[0]) is SourceBindingV1
    assert type(QuestionApplicationV1.from_json(application().to_json()).effects[0]) is tuple


@pytest.mark.parametrize("value", [True, False, 0, -1, MAX_VERSION + 1, 1.0, "1", None])
def test_versions_refuse_bools_and_overflow(value):
    with pytest.raises(CleanupRefused):
        replace(target(), version=value)
    with pytest.raises(CleanupRefused):
        TargetV1.from_json({**target().to_json(), "version": value})
    with pytest.raises(CleanupRefused):
        replace(intent(), execution_version=value)
    with pytest.raises(CleanupRefused):
        replace(part(), payload_version=value)


@pytest.mark.parametrize("value", ["", "x" * 129, "has space", "secret\nvalue", "\ud800", [], True])
def test_structural_references_are_closed_and_bounded(value):
    with pytest.raises(CleanupRefused):
        replace(target(), id=value)


def test_exact_bounds_and_aware_times():
    assert replace(target(), id="x" * 128, version=MAX_VERSION)
    assert replace(source(), locator="x" * 256)
    with pytest.raises(CleanupRefused):
        replace(source(), locator="x" * 257)
    with pytest.raises(CleanupRefused):
        replace(source(), source_time=AT.replace(tzinfo=None))
    with pytest.raises(CleanupRefused):
        replace(deferral(), next_check="2026-11-05T00:00:00+00:00")
    with pytest.raises(CleanupRefused):
        SourceBindingV1.from_json({**source().to_json(), "source_time": "2026-11-04"})
    offset = AT.astimezone(timezone(timedelta(hours=2)))
    assert canonical_json(replace(source(), source_time=offset)) == canonical_json(source())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda: replace(binding(), targets=[target()]),
        lambda: replace(binding(), plans=[plan()]),
        lambda: replace(binding(), legwork_ids=["legwork1"]),
        lambda: replace(plan(), targets=(target().to_json(),)),
        lambda: replace(command(), source=source().to_json()),
        lambda: replace(application(), effects=(["item", "item1", 3, 4, "op1"],)),
        lambda: replace(intent(), parts=[part()]),
        lambda: replace(legwork(), source_refs=(source().to_json(),)),
    ],
)
def test_mutable_or_untyped_python_children_refuse(mutate):
    with pytest.raises(CleanupRefused):
        mutate()


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind":"item","kind":"link","id":"x","version":1,"slot":"claim"}',
        '{"kind":"item","id":"x","version":true,"slot":"claim"}',
        '{"kind":"item","id":"x","version":NaN,"slot":"claim"}',
        '{"kind":"item","id":"x","version":Infinity,"slot":"claim"}',
        '{"kind":"item","id":"x","version":1.5,"slot":"claim"}',
        '{"kind":"item","id":"x","version":9223372036854775808,"slot":"claim"}',
        b"\xff",
        b"\xef\xbb\xbf{}",
        "[" * 2000,
    ],
)
def test_malformed_json_refuses(raw):
    with pytest.raises(CleanupRefused):
        decode_json(TargetV1, raw)


def test_nested_duplicate_keys_refuse():
    raw = canonical_json(command()).replace(
        '"source_version":1', '"source_version":1,"source_version":2'
    )
    with pytest.raises(CleanupRefused, match="duplicate_key"):
        decode_json(AnswerCommandV1, raw)


@pytest.mark.parametrize(
    "cls,size", [(AnswerCommandV1, MAX_ANSWER_BYTES), (QuestionBindingV1, MAX_RECORD_BYTES)]
)
def test_byte_limits_precede_parser(cls, size, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversize input reached parser")

    monkeypatch.setattr(json, "loads", forbidden)
    for raw in (b"x" * (size + 1), "x" * (size + 1), "é" * (size // 2 + 1)):
        with pytest.raises(CleanupRefused, match="too_large"):
            decode_json(cls, raw)


def test_direct_dict_bounds_precede_encoding(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversize input reached encoder")

    monkeypatch.setattr(json, "dumps", forbidden)
    with pytest.raises(CleanupRefused):
        TargetV1.from_json({**target().to_json(), "id": "x" * MAX_RECORD_BYTES})
    with pytest.raises(CleanupRefused):
        replace(intent(), parts=(part(),) * 101)


def test_aggregate_bound_precedes_record_hashing(monkeypatch):
    original = binding()
    obligations = tuple(f"w{i:03d}" + "x" * 124 for i in range(100))
    plans = tuple(
        replace(plan(), plan_id=f"plan{i}", obligation_ids=obligations) for i in range(20)
    )

    def forbidden(_):
        pytest.fail("oversize binding reached record hashing")

    monkeypatch.setattr(PlanV1, "__hash__", forbidden)
    with pytest.raises(CleanupRefused, match="too_large"):
        replace(original, plans=plans)


def test_collection_caps_and_duplicate_targets():
    with pytest.raises(CleanupRefused):
        replace(plan(), obligation_ids=tuple(f"work{i}" for i in range(101)))
    with pytest.raises(CleanupRefused):
        replace(plan(), targets=tuple(replace(target(), id=f"item{i}") for i in range(21)))
    with pytest.raises(CleanupRefused):
        replace(binding(), legwork_ids=tuple(f"legwork{i}" for i in range(51)))
    with pytest.raises(CleanupRefused):
        replace(binding(), plans=tuple(replace(plan(), plan_id=f"p{i}") for i in range(21)))
    with pytest.raises(CleanupRefused):
        replace(plan(), targets=(target(), replace(target(), version=4)))
    with pytest.raises(CleanupRefused):
        replace(binding(), plans=(replace(plan(), targets=(replace(target(), version=4),)),))


@pytest.mark.parametrize(
    "action,batch",
    [
        ("arbitrary", None),
        ("confirm", "payload1"),
        ("apply_batch", None),
        ("return_to_sources", "payload1"),
    ],
)
def test_closed_actions_and_payload_reference(action, batch):
    with pytest.raises(CleanupRefused):
        replace(plan(), action=action, batch_value_id=batch)


def test_exact_partial_coverage_and_transfer_semantics():
    two = replace(
        binding(), plans=(plan(), replace(plan(), plan_id="plan2", obligation_ids=("work2",)))
    )
    partial = replace(application(), pending_ids=("work2",))
    validate_coverage(two, partial)
    with pytest.raises(CleanupRefused):
        validate_coverage(two, application())
    with pytest.raises(CleanupRefused):
        validate_coverage(two, replace(partial, effects=(("item", "other", 3, 4, "op1"),)))
    for action in ("return_to_sources", "resolution_transfer"):
        transferred = replace(
            application(),
            resolved_ids=(),
            pending_ids=("work1",),
            transferred_ids=("work1",),
            effects=(),
        )
        bound = replace(binding(), plans=(plan(action),))
        validate_coverage(bound, transferred)
        with pytest.raises(CleanupRefused, match="cannot_resolve"):
            validate_coverage(bound, application())
        with pytest.raises(CleanupRefused, match="cannot_resolve"):
            validate_coverage(bound, replace(transferred, effects=application().effects))
    with pytest.raises(CleanupRefused):
        replace(application(), pending_ids=("work1",))
    with pytest.raises(CleanupRefused):
        replace(application(), effects=(("item", "item1", 4, 4, "op1"),))


@pytest.mark.parametrize(
    "changes",
    [
        {"obligation_id": "work1"},
        {"disposition_id": "disposition1"},
        {"source": source()},
        {"source": replace(source("m2"), source_version=3)},
        {"source": replace(source("m2"), episode_version=3)},
    ],
)
def test_per_message_and_obligation_identity_is_never_coalesced(changes):
    with pytest.raises(CleanupRefused):
        replace(intent(), parts=(part(), replace(part(2), **changes)))


@pytest.mark.parametrize("value", ["extracted", "saved", "resolved", "rejected", "revoked"])
def test_intent_never_accepts_false_completion(value):
    with pytest.raises(CleanupRefused):
        replace(part(), disposition=value)


def test_exact_disjoint_per_message_response():
    validate_accounting(intent(), ("disposition1",), ("disposition2",))
    validate_accounting(intent(), (), ("disposition1", "disposition2"))
    for accepted, refused in [
        (("disposition1",), ()),
        (("disposition1", "disposition2"), ("disposition1",)),
        (("disposition1",), ("foreign",)),
    ]:
        with pytest.raises(CleanupRefused):
            validate_accounting(intent(), accepted, refused)


def test_prospective_transfer_preserves_pending_original_work():
    original = intent()
    app = replace(
        application(),
        resolved_ids=(),
        pending_ids=("work2",),
        transferred_ids=("work1",),
        effects=(),
    )
    validate_transfer(original, transfer(), app)
    assert original == intent()
    assert all(p.disposition == "unresolved" for p in original.parts)
    assert not app.resolved_ids and not app.effects
    for bad in (
        replace(transfer(), obligation_ids=("work2",)),
        replace(transfer(), application_id="other"),
        replace(transfer(), source=replace(source(), source_version=2)),
        replace(transfer(), candidate_targets=(replace(target(), version=4),)),
    ):
        with pytest.raises(CleanupRefused):
            validate_transfer(original, bad, app)
    with pytest.raises(CleanupRefused):
        validate_transfer(original, transfer(), application())


def test_checked_no_source_schedule():
    validate_deferral(
        deferral(), (legwork(),), attempted_at=AT, latest_check=AT + timedelta(days=2)
    )
    for result in ("permission_required", "unavailable", "incomplete", "evidence_found"):
        checked = replace(
            legwork(), result=result, source_refs=(source(),) if result == "evidence_found" else ()
        )
        with pytest.raises(CleanupRefused, match="not_complete"):
            validate_deferral(
                deferral(), (checked,), attempted_at=AT, latest_check=AT + timedelta(days=2)
            )
    for checked in (
        replace(legwork(), work_id="other"),
        replace(legwork(), run_id="other"),
        replace(legwork(), target=replace(target(), version=4)),
        replace(legwork(), attempt_id="other"),
    ):
        with pytest.raises(CleanupRefused):
            validate_deferral(
                deferral(), (checked,), attempted_at=AT, latest_check=AT + timedelta(days=2)
            )
    for when in (AT, AT - timedelta(days=1), AT + timedelta(days=3)):
        with pytest.raises(CleanupRefused, match="schedule"):
            validate_deferral(
                replace(deferral(), next_check=when),
                (legwork(),),
                attempted_at=AT,
                latest_check=AT + timedelta(days=2),
            )
    with pytest.raises(CleanupRefused):
        replace(legwork(), result="evidence_found")
    with pytest.raises(CleanupRefused):
        replace(deferral(), recheck_how="invent_delay")


def test_transient_answer_text_and_no_prose_home():
    assert answer_text("  exact answer  ") == "  exact answer  "
    assert answer_text("😀" * 2000) == "😀" * 2000
    for invalid in ("x" * 2001, "\ud800", "", True):
        with pytest.raises(CleanupRefused):
            answer_text(invalid)
    for factory in FACTORIES:
        assert not {"text", "answer_text", "answer_hash", "digest"} & set(factory().to_json())


SCHEMA = {
    "question_bindings": (
        "question_id revision topic_key open_topic_key binding state created_at "
        "identity_day identity_week"
    ),
    "question_work": (
        "question_id group_id state source_binding intent_binding disposition_id intent_id "
        "waiting_operation_id "
        "payload_value_id payload_version target retry_at lease_owner lease_until application_id "
        "transfer reason"
    ),
    "question_applications": (
        "request_id question_id binding_revision principal_id source_binding plan_id commit_id "
        "change_set_id application applied_at"
    ),
    "question_events": (
        "question_id work_id event commit_id change_set_id sequence at snapshot "
        "reason application_id"
    ),
    "curator_legwork": "work_id run_id target result attempt recorded_at reason",
    "curator_escalations": (
        "claim_id claim_type streak_start attempt_id destination state destination_id prerequisite "
        "created_at"
    ),
    "curator_run_order": "run_id ordinal accepted_at",
}


def test_exact_snapshot_schema_and_migration_indexes():
    groups = {t.name for _, members in B1_GROUPS for t in members}
    migration = (
        Path(__file__).parents[2] / "migrations/0016_question_lifecycle.cypher"
    ).read_text()
    assert "SET " not in migration and "MERGE " not in migration
    for name, extra in SCHEMA.items():
        spec = table(name)
        assert name in groups
        assert (spec.group, spec.scope) == ("snapshot", "world")
        assert {p for col in spec.columns for p in col.props} == {
            "id",
            "world_id",
            "version",
            "touched_at",
            *extra.split(),
        }
        assert not {"text_home", "json_home"} & {col.type for col in spec.columns}
        statements = {
            line.split()[2]: line for line in migration.splitlines() if line.startswith("CREATE ")
        }
        for constraint in spec.uniques:
            props = ", ".join(f"n.{p}" for p in constraint.props)
            props = f"({props})" if len(constraint.props) > 1 else props
            actual = statements[constraint.name]
            assert actual.split()[:2] == ["CREATE", "CONSTRAINT"]
            assert f"FOR (n:{spec.label}) REQUIRE {props} IS UNIQUE;" in actual
        for index in spec.indexes:
            props = ", ".join(f"n.{p}" for p in index.props)
            actual = statements[index.name]
            assert actual.split()[:2] == ["CREATE", "INDEX"]
            assert f"FOR (n:{spec.label}) ON ({props});" in actual
    verification = {p: col.type for col in table("verifications").columns for p in col.props}
    for name, kind in {
        "attempt_kind": "code",
        "attempt_id": "id",
        "claim_type": "code",
        "claim_version": "int",
        "work_id": "id",
        "run_ordinal": "int",
        "commit_sequence": "int",
        "legwork_ids": "ids",
        "command": "json_ids",
        "change_set_id": "id",
    }.items():
        assert verification[name] == kind
    assert "verifications_world_id_attempt_id_unique" in migration
    assert "verifications_world_id_claim_id_commit_sequence_idx" in migration
    assert "ordinal" not in {p for col in table("run_records").columns for p in col.props}
    request = {p: col.type for col in table("requests").columns for p in col.props}
    for name, kind in {
        "protocol": "code",
        "question_id": "id",
        "question_binding_revision": "int",
        "answer_command": "json_ids",
        "answer_state": "enum:question_answer_state",
        "application_id": "id",
        "principal_id": "id",
    }.items():
        assert request[name] == kind


def test_runtime_stays_unavailable():
    with pytest.raises(CleanupRefused, match="curator_authority_unavailable"):
        require_runtime()


def test_zero_capture_generation_is_valid_and_versions_stay_positive():
    value = replace(legwork(), capture_generation=0)
    assert decode_json(type(value), canonical_json(value)) == value
    with pytest.raises(CleanupRefused):
        replace(target(), version=0)


def test_transfer_remains_in_pending_work_and_has_no_candidate_requirement():
    value = replace(
        application(),
        resolved_ids=(),
        pending_ids=("work1",),
        transferred_ids=("work1",),
        effects=(),
    )
    validate_coverage(replace(binding(), plans=(plan("return_to_sources"),)), value)
    assert replace(transfer(), candidate_targets=()).candidate_targets == ()
