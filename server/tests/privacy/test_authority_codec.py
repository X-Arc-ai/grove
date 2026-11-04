"""Independent canonical vectors for the inactive, closed authority format."""

import copy
import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

import pytest

from grove.authority.codec import (
    DecoderBudget,
    choice_digest,
    complete_event_digest,
    decode_event,
    decode_manifest,
    encode_event,
    encode_journal,
    encode_manifest,
    event_digest,
    identity_digest,
    intent_digest,
    journal_event,
    manifest_checksum,
    record_metrics,
    scan_journal,
)
from grove.authority.records import (
    CREDENTIAL_KINDS,
    PROFILE_DIGEST,
    SCHEMAS,
    TYPES,
    AuthorityCodecRefused,
    at_microseconds,
)

H = "a" * 64
Z = "0" * 64
AT = "2026-11-04T12:00:00.000000Z"
LATER = "2026-11-04T12:04:00.000000Z"
PROFILE_RAW = {"code": "authority_state_v1", "digest": PROFILE_DIGEST}


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def hashed(value):
    return sha256(canonical(value)).hexdigest()


def ledger(count=0):
    return {
        "format": "grove.forget-ledger.v1",
        "count": count,
        "file_digest": hashed(["file", count]),
        "rows_digest": hashed(["rows", count]),
    }


def controller(purpose="recover", operation=1):
    return {
        "kind": "controller_operation",
        "operation_id": uid(operation),
        "controller_receipt_id": "receipt-1",
        "genesis_digest": H,
        "purpose": purpose,
    }


def owner(approval=500):
    return {
        "kind": "owner_approval",
        "user_id": "owner-1",
        "session_id": "session-1",
        "approval_id": uid(approval),
        "payload_digest": H,
        "display_digest": H,
        "expected_revision": 0,
        "approved_at": AT,
        "expires_at": LATER,
    }


def effect(kind, number=1, **fields):
    return {"kind": kind, "effect_id": uid(number), **fields}


class Journal:
    """Independent expected projection, using only stdlib canonical JSON."""

    def __init__(self, *, adopt=False):
        self.events = []
        self.header = {
            "format": "grove.authority-journal.v1",
            "world_id": "world-1",
            "owner_id": "owner-1",
            "installation_id": uid(1),
            "genesis_digest": H,
            "schema_profile": PROFILE_RAW,
        }
        self.state = {
            "format": "grove.authority-state.v1",
            "installation_id": uid(1),
            "world_id": "world-1",
            "owner_id": "owner-1",
            "profile": PROFILE_RAW,
            "installation_epoch": 1,
            "authority_epoch": 0,
            "erasure_revision": 0,
            "ledger": None,
            "applied": [],
            "cancelled": [],
            "burned_approvals": [],
            "closed": False,
        }
        self.pending = None
        self.sealed = False
        mode = "adopt" if adopt else "birth"
        origin = effect(
            "origin",
            mode=mode,
            owner_id="owner-1",
            ledger=ledger(),
            inventory_digest=H,
            host_policy="none",
        )
        extra = {
            "owner_id": "owner-1",
            "installation_id": uid(1),
            "activation_id": uid(2),
            "genesis_digest": H,
        }
        if adopt:
            extra.update(
                ledger=ledger(),
                inventory_digest=H,
                live_store_digest=H,
                prior_authority_digest=None,
            )
        else:
            extra["empty_ledger"] = ledger()
        self.prepare(
            [origin],
            auth=controller(mode),
            mode="origin",
            kind="adopt_world" if adopt else "world_birth",
            extra=extra,
        )
        self.commit()

    def state_digest(self, state=None):
        return hashed(["grove.authority.projection.v1", self.state if state is None else state])

    def event(self, kind, payload, *, actor=None, approved=None):
        event = {
            "format": "grove.authority-event.v1",
            "world_id": "world-1",
            "sequence": len(self.events) + 1,
            "event_id": uid(10000 + len(self.events)),
            "previous_digest": hashed(self.events[-1]) if self.events else Z,
            "kind": kind,
            "actor_id": actor,
            "approved_at": approved,
            "payload": payload,
        }
        event["digest"] = hashed(event)
        self.events.append(event)
        return event

    def preview(self, intent, *, renewed=None, cancel=False):
        state = copy.deepcopy(self.state)
        auth = intent["authorization"]
        approval = auth.get("approval_id")
        if approval is not None:
            state["burned_approvals"].append(approval)
        if renewed is not None:
            state["burned_approvals"].append(renewed["approval_id"])
        state["burned_approvals"].sort()
        if cancel:
            state["cancelled"].append(
                {
                    "intent_id": intent["intent_id"],
                    "intent_digest": intent["intent_digest"],
                    "approval_id": approval,
                }
            )
            state["cancelled"].sort(key=lambda value: value["intent_id"])
            return state
        state["applied"].append(
            {
                "intent_id": intent["intent_id"],
                "approval_id": approval,
                "effects": intent["effects"],
            }
        )
        for e in intent["effects"]:
            if e["kind"] == "origin":
                state["authority_epoch"], state["ledger"] = 1, e["ledger"]
            if e["kind"] == "forget_apply":
                state["ledger"], state["erasure_revision"] = e["after_ledger"], e["revision"]
            if e["kind"] == "restore_begin":
                state["authority_epoch"] = e["new_epoch"]
                state["installation_epoch"] = e["installation_after"]
            if e["kind"] == "world_close":
                state["closed"] = True
        return state

    def prepare(self, effects, *, auth=None, mode="ordinary", kind="prepare", extra=None):
        auth = copy.deepcopy(auth if auth is not None else owner(500 + len(self.events)))
        intent = {
            "intent_id": uid(100 + len(self.events)),
            "idempotency_key": uid(200 + len(self.events)),
            "authorization": auth,
            "base_sequence": len(self.events),
            "base_head_digest": hashed(self.events[-1]) if self.events else None,
            "base_state_digest": self.state_digest(),
            "expected_installation_epoch": self.state["installation_epoch"],
            "expected_authority_epoch": self.state["authority_epoch"],
            "expected_erasure_revision": self.state["erasure_revision"],
            "mode": mode,
            "effects": sorted(effects, key=lambda e: e["effect_id"]),
        }
        if auth["kind"] == "owner_approval":
            auth["payload_digest"] = hashed(
                [
                    "grove.authority.choice.v1",
                    "world-1",
                    intent["base_sequence"],
                    intent["base_head_digest"],
                    intent["expected_installation_epoch"],
                    intent["expected_authority_epoch"],
                    intent["expected_erasure_revision"],
                    intent["effects"],
                ]
            )
        intent["result_state_digest"] = self.state_digest(self.preview(intent))
        intent["intent_digest"] = hashed(["grove.authority.intent.v1", "world-1", intent])
        payload = {**(extra or {}), "intent": intent}
        self.event(
            kind,
            payload,
            actor=auth.get("user_id") if auth["kind"] == "owner_approval" else None,
            approved=auth.get("approved_at") if auth["kind"] == "owner_approval" else None,
        )
        self.pending = intent
        return intent

    def commit(self, *, resolution=None, renewed=None):
        intent = self.pending
        cancel = resolution == "cancel_unapplied_expansion"
        next_state = self.preview(intent, renewed=renewed, cancel=cancel)
        completion = {
            "intent_id": intent["intent_id"],
            "intent_digest": intent["intent_digest"],
            "applied_state_digest": self.state_digest(next_state),
            "ledger": next_state["ledger"],
            "authority_epoch": next_state["authority_epoch"],
            "erasure_revision": next_state["erasure_revision"],
            "completed_at": LATER,
        }
        auth = renewed or intent["authorization"]
        actor = auth.get("user_id") if auth["kind"] == "owner_approval" else None
        approved = auth.get("approved_at") if auth["kind"] == "owner_approval" else None
        if intent["mode"] == "close":
            payload = {
                "completion": completion,
                "closure_id": next(
                    e["closure_id"] for e in intent["effects"] if e["kind"] == "world_close"
                ),
            }
            kind = "world_closed"
        elif resolution is not None:
            payload = {
                "operation_id": uid(700 + len(self.events)),
                "controller_receipt_id": "recovery-1",
                "resolution": resolution,
                "completion": completion,
                "observed_db_digest": intent["base_state_digest"]
                if cancel
                else self.state_digest(next_state),
                "original_approval_digest": intent["authorization"].get("payload_digest"),
                "renewed_approval": renewed,
            }
            kind = "recover"
        else:
            kind, payload = "commit", {"completion": completion}
        self.event(kind, payload, actor=actor, approved=approved)
        self.state, self.pending = next_state, None

    def seal(self):
        self.event("seal", {"reason": "capacity"})
        self.sealed = True

    def manifest(self):
        release = (
            "closed"
            if self.state["closed"]
            else "sealed"
            if self.sealed
            else (
                "restoring"
                if self.pending and self.pending["mode"] == "restore"
                else "pending"
                if self.pending
                else "no_host_policy"
            )
        )
        if release == "no_host_policy":
            policies = {}
            revoked = set()
            for applied in self.state["applied"]:
                for e in applied["effects"]:
                    if e["kind"] == "policy_replace":
                        policies[e["policy_id"]] = e
                    if e["kind"] == "grant_revoke":
                        revoked.add((e["grant_id"], e["grant_epoch"]))
            if any(
                e["state"] == "active"
                and (e["grant"]["grant_id"], e["grant"]["grant_epoch"]) not in revoked
                for e in policies.values()
            ):
                release = "ready"
        ie = self.state["installation_epoch"]
        manifest_state = "ready"
        if self.pending and self.pending["mode"] == "restore":
            begin = next(e for e in self.pending["effects"] if e["kind"] == "restore_begin")
            if begin["scope"] == "installation":
                ie, manifest_state = begin["installation_after"], "restoring"
        head = {
            "world_id": "world-1",
            "owner_id": "owner-1",
            "origin_event_id": self.events[0]["event_id"],
            "origin_digest": hashed(self.events[0]),
            "sequence": len(self.events),
            "head_digest": hashed(self.events[-1]),
            "ledger": self.state["ledger"] or ledger(),
            "authority_epoch": self.state["authority_epoch"],
            "erasure_revision": self.state["erasure_revision"],
            "release_state": release,
            "pending_id": self.pending["intent_id"] if self.pending else None,
        }
        manifest = {
            "format": "grove.authority-manifest.v1",
            "installation_id": uid(1),
            "installation_epoch": ie,
            "activation_id": uid(2),
            "genesis_digest": H,
            "inventory_digest": H,
            "schema_profile": PROFILE_RAW,
            "state": manifest_state,
            "worlds": [head],
        }
        manifest["checksum"] = hashed(manifest)
        return manifest

    def raw(self):
        return canonical({**self.header, "events": self.events})

    def decode(self, budget=None):
        from grove.authority.transitions import reduce_journal

        budget = budget or DecoderBudget()
        journal = scan_journal(self.raw(), budget=budget)
        manifest = decode_manifest(canonical(self.manifest()), budget=budget)
        return reduce_journal(journal, manifest=manifest, budget=budget)


def resign(event):
    event.pop("digest", None)
    event["digest"] = hashed(event)
    return event


def sample(spec, field_name=""):
    if type(spec) is tuple:
        kind = spec[0]
        if kind == "enum":
            return spec[1][0]
        if kind == "number":
            return spec[1]
        if kind == "nullable":
            return None
        if kind == "array":
            return [sample(spec[1]) for _ in range(spec[3])]
        if kind == "union":
            return sample(spec[1][0])
    if spec in SCHEMAS:
        result = {
            name: sample(value, name)
            for name, value in SCHEMAS[spec]
            if not (type(value) is tuple and value[0] == "dependent")
        }
        if spec == "Profile":
            result = copy.deepcopy(PROFILE_RAW)
        if spec == "OwnerApproval":
            result["expires_at"] = LATER
        if spec == "CredentialIssue":
            result["expires_at"] = LATER
        if spec == "AssignmentSet":
            result["approval_id"] = uid(1)
        if spec == "FactPredicate":
            result["value_bytes"] = 14
        if spec in ("EpochInvalidate", "RestoreBegin", "RestorePayload"):
            result["new_epoch"] = 2
        if spec == "EpochInvalidate":
            result["kinds"] = list(CREDENTIAL_KINDS)
        if spec == "ForgetApply":
            result["after_ledger"] = ledger(1)
        if spec == "LineageOutput":
            result.update(request_id="request-1", tool_call_id="tool-1", canonical_digest=H)
        return result
    return {"ID": "ref-1", "H": H, "UUID": uid(1), "AT": AT, "U": 0, "P": 1, "BOOL": False}[spec]


def decode_effect(raw):
    journal = Journal()
    journal.prepare([raw])
    return decode_event(
        canonical(journal.events[-1]), budget=DecoderBudget()
    ).payload.intent.effects[0]


def test_pinned_profile_and_distinct_hash_domains():
    assert PROFILE_DIGEST == "6ed8d5822075b5cfc18692e9b8fea8a55b3b0b329a0f9cf386a169159380f80e"
    raw = Journal().events[0]
    record = decode_event(canonical(raw), budget=DecoderBudget())
    assert event_digest(record) == raw["digest"]
    assert complete_event_digest(record) == hashed(raw)
    assert complete_event_digest(record) != event_digest(record)
    intent = record.payload.intent
    assert intent_digest("world-1", intent) == intent.intent_digest


def test_complete_round_trip_and_immutable_records():
    fixture = Journal()
    budget = DecoderBudget()
    journal = scan_journal(fixture.raw(), budget=budget)
    manifest = decode_manifest(canonical(fixture.manifest()), budget=budget)
    assert b"".join(encode_journal(journal, budget=budget)) == fixture.raw()
    assert b"".join(encode_manifest(manifest, budget=budget)) == canonical(fixture.manifest())
    assert manifest_checksum(manifest) == fixture.manifest()["checksum"]
    for index, raw in enumerate(fixture.events):
        record = journal_event(journal, index)
        assert b"".join(encode_event(record, budget=budget)) == canonical(raw)
        with pytest.raises(FrozenInstanceError):
            record.actor_id = "edited"
        with pytest.raises(FrozenInstanceError):
            setattr(record.payload, next(iter(dict(SCHEMAS[type(record.payload).__name__]))), None)
    assert journal.metrics.events == 2


@pytest.mark.parametrize(
    "schema", [name for name, fields in SCHEMAS.items() if "effect_id" in dict(fields)]
)
def test_every_closed_effect_family_has_a_typed_round_trip(schema):
    raw = sample(schema)
    record = decode_effect(raw)
    assert type(record) is TYPES[schema]
    assert record.kind == raw["kind"]


@pytest.mark.parametrize(
    "name",
    [
        "SessionBinding",
        "CodeBinding",
        "AccessBinding",
        "RefreshBinding",
        "EmailBinding",
        "AuthStateBinding",
        "PassBinding",
    ],
)
def test_all_credential_bindings_are_closed(name):
    raw = sample("CredentialIssue")
    raw["binding"] = sample(name)
    raw["expires_at"] = LATER
    assert type(decode_effect(raw).binding) is TYPES[name]


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda b: b + b"\n", "noncanonical"),
        (lambda b: b"\xef\xbb\xbf" + b, "noncanonical"),
        (lambda b: b.replace(b'"sequence":1', b'"sequence":01'), "noncanonical"),
        (lambda b: b.replace(b'"sequence":1', b'"sequence":1.0'), "noncanonical"),
        (lambda b: b.replace(b'"sequence":1', b'"sequence":1e0'), "noncanonical"),
        (lambda b: b.replace(b'"sequence":1', b'"sequence":true'), "scalar"),
        (lambda b: b.replace(b'"sequence":1', b'"sequence":-0'), "noncanonical"),
        (lambda b: b.replace(b'"sequence":1', b'"sequence":99999999999999999999'), "token_limit"),
        (
            lambda b: b.replace(b'"world_id":"world-1"', b'"world_id":"\\u0077orld-1"'),
            "noncanonical",
        ),
        (
            lambda b: b.replace(b'"actor_id":null', b'"actor_id":null,"actor_id":null'),
            "duplicate_key",
        ),
        (lambda b: b.replace(b'"sequence":1', b'"secret":{},"sequence":1'), "unknown_field"),
    ],
)
def test_noncanonical_and_untyped_input_refuses_without_input_values(mutation, code):
    with pytest.raises(AuthorityCodecRefused) as caught:
        decode_event(mutation(canonical(Journal().events[0])), budget=DecoderBudget())
    assert caught.value.code == code
    assert str(caught.value) == code


@pytest.mark.parametrize(
    "kind,select",
    [
        ("item", {"kind": "item_field", "field": "title", "property_id": "bad"}),
        ("item", {"kind": "item_field", "field": "properties", "property_id": None}),
        ("value", {"kind": "message", "key_digest": H}),
        ("registry_field", {"kind": "registry_field", "table": "capabilities", "field": "name"}),
        ("item", {"kind": "whole", "extra": "private-canary"}),
    ],
)
def test_reference_and_registry_matrix_refuses_invalid_pairs(kind, select):
    raw = sample("AssignmentSet")
    raw.update(ref={"kind": kind, "id": "ref-1", "selector": select}, approval_id=uid(1))
    with pytest.raises(AuthorityCodecRefused):
        decode_effect(raw)


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-29T12:00:00.000000Z",
        "2026-11-04T12:00:60.000000Z",
        "2026-11-04T12:00:00.000000+00:00",
        "2026-11-04T12:00:00Z",
        "0000-01-01T00:00:00.000000Z",
    ],
)
def test_operational_time_is_exact_utc_calendar(value):
    with pytest.raises(AuthorityCodecRefused, match="scalar"):
        at_microseconds(value)


def test_calendar_extremes_and_leap_year_are_valid():
    assert at_microseconds("0001-01-01T00:00:00.000000Z") == 0
    assert at_microseconds("2000-02-29T00:00:00.000000Z") > 0
    assert at_microseconds("9999-12-31T23:59:59.999999Z") > at_microseconds(AT)


def test_owner_choice_uses_exact_effects_and_both_epochs():
    fixture = Journal()
    fixture.prepare([effect("class_create", 20, class_id="class-1", label_value_id="value-1")])
    record = decode_event(canonical(fixture.events[-1]), budget=DecoderBudget())
    intent = record.payload.intent
    assert choice_digest("world-1", intent) == intent.authorization.payload_digest
    assert choice_digest(
        "world-1", replace(intent, expected_installation_epoch=2)
    ) != choice_digest("world-1", intent)
    assert choice_digest("world-1", replace(intent, expected_authority_epoch=2)) != choice_digest(
        "world-1", intent
    )


def test_client_urls_and_opaque_ids_use_distinct_exact_digest_domains():
    budget = DecoderBudget()
    value = "https://client.example/metadata?name=one"
    assert identity_digest("client", value, budget=budget) == hashed(
        ["grove.authority.client.v1", value]
    )
    assert identity_digest("client", value, budget=budget) != identity_digest(
        "resource", value, budget=budget
    )
    assert identity_digest("client", value, budget=budget) != identity_digest(
        "client", value + "/", budget=budget
    )
    assert identity_digest("key", "", source_id="source-1", budget=budget) != identity_digest(
        "key", "", source_id="source-2", budget=budget
    )
    with pytest.raises(AuthorityCodecRefused):
        decode_effect(effect("class_create", 21, class_id=value, label_value_id="value-1"))


def test_key_digest_preserves_unicode_and_rejects_surrogates_and_oversize():
    budget = DecoderBudget()
    assert identity_digest("key", "é", source_id="s", budget=budget) != identity_digest(
        "key", "e\u0301", source_id="s", budget=budget
    )
    for value in ("x" * 1025, "\ud800"):
        with pytest.raises(AuthorityCodecRefused):
            identity_digest("key", value, source_id="s", budget=budget)


@pytest.mark.parametrize("version", ["legacy", "v2", "v3", "v4"])
def test_ledger_version_references_do_not_rewrite_legacy_bytes(version):
    raw = sample("ForgetApply")
    raw["row"].update(
        plan_version=version,
        plan_digest=None if version == "legacy" else H,
        extension_digest=H if version in ("v3", "v4") else None,
    )
    assert decode_effect(raw).row.plan_version == version
    raw["row"]["extension_digest"] = None if version in ("v3", "v4") else H
    with pytest.raises(AuthorityCodecRefused):
        decode_effect(raw)


def test_encoder_revalidates_manually_constructed_or_replaced_records():
    record = decode_event(canonical(Journal().events[0]), budget=DecoderBudget())
    with pytest.raises(AuthorityCodecRefused):
        encode_event(replace(record, sequence=True), budget=DecoderBudget())
    with pytest.raises(AuthorityCodecRefused):
        encode_event(replace(record, payload={"reason": "capacity"}), budget=DecoderBudget())


def test_schema_counts_keys_as_nodes():
    fixture = Journal()
    fixture.seal()
    event = decode_event(canonical(fixture.events[-1]), budget=DecoderBudget())
    assert record_metrics(event).nodes == 23


@pytest.mark.parametrize(
    "left,right,kind",
    [
        ("+01:00", "-01:00", "source_offset"),
        ("UTC", "Etc/UTC", "source_timezone"),
        ("2026-11-04T12:00", "26-11-04T12:00", "local_candidate"),
        ("2026-11-04T12:00", "2026-11-04T12:00:00", "local_candidate"),
    ],
)
def test_fact_selector_preserves_signed_offset_timezone_alias_and_precision(left, right, kind):
    def selected(text):
        predicate = {
            "version": "text_fact_values.v1",
            "kind": kind,
            "value_bytes": len(text.encode()),
            "sha256": sha256(text.encode()).hexdigest(),
        }
        raw = sample("AssignmentSet")
        raw["ref"] = {
            "kind": "source",
            "id": "source-1",
            "selector": {
                "kind": "text_fact",
                "key_digest": H,
                "fact_set_digest": hashed([predicate]),
                "predicate": predicate,
            },
        }
        return decode_effect(raw).ref.selector.predicate

    assert selected(left) != selected(right)


def test_fact_digest_alone_never_erases_kind_or_length():
    raw = sample("AssignmentSet")
    pred = {
        "version": "text_fact_values.v1",
        "kind": "source_offset",
        "value_bytes": 6,
        "sha256": H,
    }
    raw["ref"] = {
        "kind": "source",
        "id": "source-1",
        "selector": {"kind": "text_fact", "key_digest": H, "fact_set_digest": H, "predicate": pred},
    }
    first = decode_effect(raw).ref.selector.predicate
    pred["kind"] = "source_timezone"
    second = decode_effect(raw).ref.selector.predicate
    pred["value_bytes"] = 7
    third = decode_effect(raw).ref.selector.predicate
    assert first != second != third


def test_private_tuple_byte_boundary_is_checked_before_json_encoding(monkeypatch):
    import grove.authority.codec as module

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("encoder must not run")

    monkeypatch.setattr(module.json, "dumps", forbidden)
    with pytest.raises(AuthorityCodecRefused, match="byte_limit"):
        identity_digest("client", "\x01" * 3000, budget=DecoderBudget())
    assert not calls
