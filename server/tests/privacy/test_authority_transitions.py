"""Pure replay tests. A matching projection never acts as a release capability."""

import copy
from dataclasses import replace

import pytest

from grove.authority.codec import DecoderBudget, decode_event, scan_journal
from grove.authority.records import CREDENTIAL_KINDS, TYPES, AuthorityCodecRefused
from grove.authority.transitions import (
    REQUIREMENT_CODES,
    compare_projection,
    lookup_retry,
)
from tests.privacy.test_authority_codec import (
    AT,
    LATER,
    PROFILE_RAW,
    H,
    Journal,
    canonical,
    controller,
    effect,
    hashed,
    ledger,
    owner,
    resign,
    uid,
)


def projection(result):
    return TYPES["ProjectionBinding"](
        installation_id=result.journal.header.installation_id,
        installation_epoch=result.installation_epoch,
        authority_epoch=result.authority_epoch,
        sequence=result.journal.metrics.events,
        head_digest=result._state["head_digest"],
        schema_profile=result.journal.header.schema_profile,
        state_digest=result.state_digest,
        ledger=result.ledger,
        ready=True,
    )


def grant():
    return {
        "grant_id": "grant-1",
        "grant_epoch": 1,
        "user_id": "owner-1",
        "client": {"kind": "oauth_client", "identity_digest": H},
        "resource": {"kind": "oauth_resource", "identity_digest": H},
    }


def consent(fixture):
    approval = owner(600 + len(fixture.events))
    aid = approval["approval_id"]
    fixture.prepare(
        [
            effect("class_create", 10, class_id="class-1", label_value_id="value-1"),
            effect(
                "grant_set",
                11,
                grant=grant(),
                expected_epoch=0,
                scopes=["grove.read"],
                state="active",
                approval_id=aid,
            ),
            effect(
                "policy_replace",
                12,
                policy_id="policy-1",
                grant=grant(),
                expected_revision=0,
                revision=1,
                rules=[{"kind": "allow_class", "class_id": "class-1"}],
                state="active",
                approval_id=aid,
            ),
            effect(
                "connection_rule_set",
                13,
                rule_id=uid(40),
                class_id="class-1",
                grant_id="grant-1",
                grant_epoch=1,
                expected_revision=0,
                revision=1,
                state="active",
                approval_id=aid,
            ),
        ],
        auth=approval,
    )
    fixture.commit()


def account():
    return {
        "kind": "account_flow",
        "flow_id": "flow-1",
        "attempt_id": uid(900),
        "mechanism": "google_oidc",
        "configuration_digest": H,
    }


def issue_session(fixture):
    fixture.prepare(
        [
            effect(
                "credential_issue",
                20,
                credential_id="session-1",
                binding={"kind": "session", "user_id": "owner-1"},
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            )
        ],
        auth=account(),
    )
    fixture.commit()


def credential(credential_id="session-1", kind="session"):
    oauth = kind in ("oauth_code", "access_token", "refresh_token")
    return {
        "kind": "credential_transition",
        "credential_kind": kind,
        "credential_id": credential_id,
        "user_id": "owner-1",
        "installation_epoch": 1,
        "authority_epoch": 1,
        "client": grant()["client"] if oauth else None,
        "grant_id": "grant-1" if oauth else None,
        "family_id": "family-1" if kind in ("access_token", "refresh_token") else None,
    }


def service(run_id="run-1"):
    return {
        "kind": "service_transition",
        "configuration_digest": H,
        "run_id": run_id,
        "agent": "grove",
    }


def start_run(fixture):
    fixture.prepare(
        [
            effect(
                "run_state",
                70,
                run_id="run-1",
                agent="grove",
                configuration_digest=H,
                expected_state="absent",
                state="active",
            )
        ],
        auth=service(),
    )
    fixture.commit()


def forget(fixture):
    fixture.prepare(
        [
            effect(
                "forget_apply",
                90,
                row={
                    "id": "forget-1",
                    "plan_version": "v2",
                    "row_digest": H,
                    "plan_digest": H,
                    "extension_digest": None,
                },
                before_ledger=ledger(),
                after_ledger=ledger(1),
                expected_revision=0,
                revision=1,
            )
        ],
        mode="restrictive",
    )


@pytest.mark.parametrize("adopt", [False, True])
def test_origin_and_projection_match_remain_untrusted(adopt):
    fixture = Journal(adopt=adopt)
    result = fixture.decode()
    assert result.state_digest == fixture.state_digest()
    assert result.release_state == "no_host_policy"
    agreement = compare_projection(result, projection(result))
    assert agreement.requirements
    assert all(r.code in REQUIREMENT_CODES for r in agreement.requirements)
    assert "trusted_genesis" in {r.code for r in agreement.requirements}
    assert not hasattr(agreement, "authorize")
    assert not hasattr(agreement, "verified")


def test_consent_dependency_order_and_revision_tombstones():
    fixture = Journal()
    consent(fixture)
    result = fixture.decode()
    assert result.release_state == "ready"
    assert result.state_digest == fixture.state_digest()
    fixture.prepare(
        [
            effect(
                "grant_revoke",
                22,
                grant_id="grant-1",
                grant_epoch=1,
                expected_revision=1,
                revision=2,
                reason="owner",
            )
        ],
        mode="restrictive",
    )
    fixture.commit()
    result = fixture.decode()
    assert result.release_state == "no_host_policy"
    assert result._state["entities"][("connection", uid(40))].status == "retired"
    assert result._state["entities"][("policy", "policy-1")].status == "revoked"


def test_effect_lexical_order_cannot_change_dependency_meaning():
    fixture = Journal()
    consent(fixture)
    intent = fixture.events[2]["payload"]["intent"]
    # A fresh fixture constructs the same semantic consent with creator last.
    other = Journal()
    changed = copy.deepcopy(intent["effects"])
    for index, e in enumerate(changed):
        e["effect_id"] = uid(100 - index)
    other.prepare(changed, auth=owner(602))
    other.commit()
    assert other.decode().release_state == "ready"


def test_two_effects_cannot_write_the_same_class_in_one_intent():
    fixture = Journal()
    fixture.prepare(
        [
            effect("class_create", 10, class_id="c", label_value_id="v"),
            effect(
                "class_label",
                11,
                class_id="c",
                label_value_id="v2",
                expected_revision=0,
                revision=1,
            ),
        ]
    )
    with pytest.raises(AuthorityCodecRefused, match="transition"):
        fixture.decode()


def test_wrong_authorization_variant_cannot_grant_disclosure():
    fixture = Journal()
    fixture.prepare([effect("class_create", 10, class_id="c", label_value_id="v")], auth=account())
    with pytest.raises(AuthorityCodecRefused, match="transition"):
        fixture.decode()


def test_pending_origin_and_pending_ordinary_refuse_projection_release():
    fixture = Journal()
    fixture.events.pop()
    fixture.state["applied"] = []
    fixture.state["authority_epoch"], fixture.state["ledger"] = 0, None
    fixture.pending = fixture.events[0]["payload"]["intent"]
    assert fixture.decode().release_state == "pending"
    fixture = Journal()
    fixture.prepare([effect("class_create", 10, class_id="c", label_value_id="v")])
    result = fixture.decode()
    with pytest.raises(AuthorityCodecRefused, match="pending"):
        compare_projection(result, projection(result))


def test_exact_retry_has_no_new_event_and_changed_retry_refuses():
    fixture = Journal()
    raw_intent = fixture.events[0]["payload"]["intent"]
    result = fixture.decode()
    original = decode_event(canonical(fixture.events[0]), budget=DecoderBudget()).payload.intent
    assert lookup_retry(result, original).outcome == "committed"
    assert result.journal.metrics.events == 2
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        lookup_retry(result, replace(original, result_state_digest=H))
    assert raw_intent["idempotency_key"] == original.idempotency_key


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", 1),
        ("head_digest", H),
        ("installation_epoch", 2),
        ("authority_epoch", 2),
        ("state_digest", H),
        ("ready", False),
    ],
)
def test_projection_checks_each_dimension(field, value):
    result = Journal().decode()
    with pytest.raises(AuthorityCodecRefused, match="projection_mismatch"):
        compare_projection(result, replace(projection(result), **{field: value}))


def test_seal_is_sticky_and_changes_no_erasure_state():
    fixture = Journal()
    original = fixture.state_digest()
    fixture.seal()
    result = fixture.decode()
    assert result.state_digest == original and result.erasure_revision == 0
    assert result.sealed and result.release_state == "sealed"
    fixture.seal()
    with pytest.raises(AuthorityCodecRefused):
        fixture.decode()


def test_pending_restriction_can_finish_after_seal():
    fixture = Journal()
    forget(fixture)
    fixture.seal()
    fixture.commit(resolution="complete_restriction")
    result = fixture.decode()
    assert result.release_state == "sealed"
    assert result.erasure_revision == 1 and result.ledger.count == 1


def test_pending_expansion_cannot_be_sealed_or_cancelled_as_restriction():
    fixture = Journal()
    fixture.prepare([effect("class_create", 10, class_id="c", label_value_id="v")])
    fixture.seal()
    with pytest.raises(AuthorityCodecRefused, match="transition"):
        fixture.decode()


@pytest.mark.parametrize(
    "resolution", ["confirm_committed", "cancel_unapplied_expansion", "complete_reapproved"]
)
def test_exact_expansion_recovery(resolution):
    fixture = Journal()
    fixture.prepare([effect("class_create", 10, class_id="c", label_value_id="v")])
    original = copy.deepcopy(fixture.pending["authorization"])
    renewed = None
    if resolution == "complete_reapproved":
        renewed = {
            **original,
            "approval_id": uid(999),
            "approved_at": "2026-11-04T12:01:00.000000Z",
        }
    fixture.commit(resolution=resolution, renewed=renewed)
    result = fixture.decode()
    assert result.state_digest == fixture.state_digest()
    assert original["approval_id"] in result._state["burned"]
    if renewed:
        assert renewed["approval_id"] in result._state["burned"]
    if resolution == "cancel_unapplied_expansion":
        assert ("class", "c") not in result._state["entities"]


def test_mixed_or_uncertain_recovery_cannot_cancel():
    fixture = Journal()
    fixture.prepare([effect("class_create", 10, class_id="c", label_value_id="v")])
    fixture.commit()
    fixture.prepare(
        [
            effect(
                "class_label",
                11,
                class_id="c",
                label_value_id="v2",
                expected_revision=1,
                revision=2,
            )
        ]
    )
    fixture.commit(resolution="cancel_unapplied_expansion")
    with pytest.raises(AuthorityCodecRefused, match="transition"):
        fixture.decode()


def test_recovery_observation_must_match_exact_original_result():
    fixture = Journal()
    forget(fixture)
    fixture.commit(resolution="complete_restriction")
    fixture.events[-1]["payload"]["observed_db_digest"] = H
    resign(fixture.events[-1])
    with pytest.raises(AuthorityCodecRefused):
        fixture.decode()


def test_closed_identity_cannot_be_reborn_or_append():
    fixture = Journal()
    fixture.prepare(
        [effect("world_close", 91, closure_id=uid(80), owner_id="owner-1")], mode="close"
    )
    fixture.commit()
    assert fixture.decode().release_state == "closed"
    fixture.seal()
    with pytest.raises(AuthorityCodecRefused, match="closed"):
        fixture.decode()


@pytest.mark.parametrize("source", ["logical_snapshot", "history_rebuild", "physical_dump"])
@pytest.mark.parametrize("scope", ["world", "installation"])
def test_all_restore_kinds_keep_distinct_epochs_and_invalidate_credentials(source, scope):
    fixture = Journal()
    issue_session(fixture)
    common = {
        "operation_id": uid(92),
        "source_kind": source,
        "artifact_digest": H,
        "inventory_digest": H,
        "d3_fingerprint": H,
        "old_epoch": 1,
        "new_epoch": 2,
        "scope": scope,
        "installation_before": 1,
        "installation_after": 2 if scope == "installation" else 1,
    }
    extra = {
        **common,
        "controller_receipt_id": "receipt-1",
        "live_authority_head": hashed(fixture.events[-1]),
        "ledger": ledger(),
    }
    fixture.prepare(
        [
            effect("restore_begin", 93, **common, profile=PROFILE_RAW, live_ledger=ledger()),
            effect("epoch_invalidate", 94, old_epoch=1, new_epoch=2, kinds=list(CREDENTIAL_KINDS)),
        ],
        auth=controller("restore", 92),
        mode="restore",
        kind="restore_epoch",
        extra=extra,
    )
    assert fixture.decode().release_state == "restoring"
    fixture.commit(resolution="confirm_committed")
    result = fixture.decode()
    assert result.authority_epoch == 2
    assert result.installation_epoch == (2 if scope == "installation" else 1)
    assert result._state["entities"][("credential", "session-1")].status == "revoked"


def test_account_flow_can_issue_session_but_cannot_create_classes():
    fixture = Journal()
    issue_session(fixture)
    assert fixture.decode()._state["entities"][("credential", "session-1")].status == "active"


def test_service_run_and_pass_end_atomically():
    fixture = Journal()
    start_run(fixture)
    fixture.prepare(
        [
            effect(
                "credential_issue",
                71,
                credential_id="pass-1",
                binding={
                    "kind": "runner_pass",
                    "run_id": "run-1",
                    "agent": "grove",
                    "configuration_digest": H,
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            )
        ],
        auth=service(),
    )
    fixture.commit()
    fixture.prepare(
        [
            effect(
                "run_state",
                72,
                run_id="run-1",
                agent="grove",
                configuration_digest=H,
                expected_state="active",
                state="completed",
            )
        ],
        auth=service(),
        mode="restrictive",
    )
    fixture.commit()
    result = fixture.decode()
    assert result._state["entities"][("credential", "pass-1")].status == "revoked"


def test_prospective_assignment_requires_exact_authenticated_origin_and_rule():
    fixture = Journal()
    consent(fixture)
    origin = {
        "grant_id": "grant-1",
        "grant_epoch": 1,
        "request_id": "request-1",
        "source_id": "source-1",
        "origin_digest": H,
    }
    auth = {"kind": "prospective_assignment", "rule_id": uid(40), **origin}
    fixture.prepare(
        [
            effect(
                "assignment_set",
                33,
                assignment_id="assignment-1",
                ref={"kind": "source", "id": "source-1", "selector": {"kind": "whole"}},
                expected_revision=0,
                revision=1,
                classes=[{"class_id": "class-1", "rule_id": uid(40)}],
                origin=origin,
                approval_id=None,
            )
        ],
        auth=auth,
    )
    fixture.commit()
    assert fixture.decode()._state["entities"][("assignment", "assignment-1")].revision == 1


def test_lineage_derivation_output_dependency_and_sticky_unknown():
    fixture = Journal()
    start_run(fixture)
    auth = {
        "kind": "observed_lineage",
        "run_id": "run-1",
        "model_call_id": "call-1",
        "configuration_digest": H,
        "observation_digest": H,
    }
    fixture.prepare(
        [
            effect(
                "lineage_derivation",
                80,
                derivation_id="derivation-1",
                run_id="run-1",
                ordinal=1,
                previous_id=None,
                model_call_id="call-1",
                input_count=1,
                input_digest=H,
                seed_count=0,
                seed_digest=H,
                state="complete",
                authority_epoch=1,
                created_at=AT,
            ),
            effect(
                "lineage_output",
                81,
                output_id="output-1",
                run_id="run-1",
                model_call_id="call-1",
                derivation_id="derivation-1",
                tool_call_id="tool-1",
                output_kind="proposal",
                canonical_digest=H,
                request_id="request-1",
                conversion="grove.proposal.v1",
                state="complete",
                authority_epoch=1,
                created_at=AT,
            ),
            effect(
                "lineage_dependency",
                82,
                dependency_id="dependency-1",
                output_slot={"kind": "value", "id": "value-1", "field": "value"},
                output_version=1,
                derivation_id="derivation-1",
                output_id="output-1",
                created_at=AT,
            ),
        ],
        auth=auth,
    )
    fixture.commit()
    result = fixture.decode()
    assert ("dependency", "dependency-1") in result._state["entities"]
    fixture.prepare(
        [effect("lineage_invalidate", 83, run_id="run-1", reason="interrupted")],
        auth=auth,
        mode="restrictive",
    )
    fixture.commit()
    assert fixture.decode()._state["entities"][("lineage_run", "run-1")].status == "unknown"


def test_duplicate_event_identity_and_foreign_wrapper_refuse():
    fixture = Journal()
    fixture.events[1]["event_id"] = fixture.events[0]["event_id"]
    resign(fixture.events[1])
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        scan_journal(fixture.raw(), budget=DecoderBudget())
    fixture = Journal()
    fixture.header["world_id"] = "world-2"
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        fixture.decode()


def issue_code(fixture):
    fixture.prepare(
        [
            effect(
                "credential_issue",
                24,
                credential_id="code-1",
                binding={
                    "kind": "oauth_code",
                    "grant": grant(),
                    "scopes": ["grove.read"],
                    "flow_id": "flow-1",
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            )
        ],
        auth=credential(),
    )
    fixture.commit()


def issue_pair(fixture, *, parent="code-1", number=30, generation=1):
    kind = "oauth_code" if parent == "code-1" else "refresh_token"
    fixture.prepare(
        [
            effect(
                "credential_consume",
                number,
                credential_id=parent,
                use_id=uid(1000 + number),
                outcome="used",
            ),
            effect(
                "credential_issue",
                number + 1,
                credential_id=f"access-{generation}",
                binding={
                    "kind": "access_token",
                    "grant": grant(),
                    "scopes": ["grove.read"],
                    "family_id": "family-1",
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            ),
            effect(
                "credential_issue",
                number + 2,
                credential_id=f"refresh-{generation}",
                binding={
                    "kind": "refresh_token",
                    "grant": grant(),
                    "scopes": ["grove.read"],
                    "family_id": "family-1",
                    "parent_credential_id": None if kind == "oauth_code" else parent,
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            ),
        ],
        auth=credential(parent, kind),
    )
    fixture.commit()


def test_code_and_refresh_single_use_and_family_reuse_revocation():
    fixture = Journal()
    consent(fixture)
    issue_session(fixture)
    issue_code(fixture)
    issue_pair(fixture)
    assert fixture.decode()._state["entities"][("credential", "code-1")].status == "consumed"
    issue_pair(fixture, parent="refresh-1", number=35, generation=2)
    result = fixture.decode()
    assert result._state["entities"][("credential", "access-1")].status == "active"
    assert result._state["entities"][("credential", "refresh-1")].status == "consumed"
    fixture.prepare(
        [
            effect(
                "credential_consume",
                40,
                credential_id="refresh-1",
                use_id=uid(1040),
                outcome="reused",
            )
        ],
        auth=credential("refresh-1", "refresh_token"),
        mode="restrictive",
    )
    fixture.commit()
    result = fixture.decode()
    assert result._state["entities"][("family", "family-1")].status == "revoked"
    for identifier in ("access-1", "access-2", "refresh-1", "refresh-2"):
        assert result._state["entities"][("credential", identifier)].status == "revoked"


def test_email_attempts_reach_terminal_state_and_cannot_restart():
    fixture = Journal()
    fixture.prepare(
        [
            effect(
                "credential_issue",
                20,
                credential_id="email-1",
                binding={
                    "kind": "email_code",
                    "challenge_id": "challenge-1",
                    "user_id": "owner-1",
                    "attempt_limit": 2,
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            )
        ],
        auth={**account(), "mechanism": "email_challenge"},
    )
    fixture.commit()
    fixture.prepare(
        [
            effect(
                "credential_attempt",
                21,
                credential_id="email-1",
                expected_attempts=0,
                attempts=1,
                outcome="invalid",
            )
        ],
        auth=credential("email-1", "email_code"),
    )
    fixture.commit()
    fixture.prepare(
        [
            effect(
                "credential_attempt",
                22,
                credential_id="email-1",
                expected_attempts=1,
                attempts=2,
                outcome="locked",
            )
        ],
        auth=credential("email-1", "email_code"),
    )
    fixture.commit()
    result = fixture.decode()
    assert result._state["entities"][("credential", "email-1")].attempts == 2
    assert result._state["entities"][("credential", "email-1")].status == "consumed"


def test_service_authorization_cannot_revoke_an_owner_session():
    fixture = Journal()
    issue_session(fixture)
    start_run(fixture)
    fixture.prepare(
        [effect("credential_revoke", 100, credential_id="session-1", reason="run_end")],
        auth=service(),
        mode="restrictive",
    )
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        fixture.decode()


def test_credential_flow_cannot_revoke_an_unrelated_credential():
    fixture = Journal()
    consent(fixture)
    issue_session(fixture)
    issue_code(fixture)
    fixture.prepare(
        [effect("credential_revoke", 100, credential_id="code-1", reason="owner")],
        auth=credential(),
        mode="restrictive",
    )
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        fixture.decode()


def test_new_code_cannot_reuse_an_old_token_family():
    fixture = Journal()
    consent(fixture)
    issue_session(fixture)
    issue_code(fixture)
    issue_pair(fixture)
    fixture.prepare(
        [
            effect(
                "credential_issue",
                50,
                credential_id="code-2",
                binding={
                    "kind": "oauth_code",
                    "grant": grant(),
                    "scopes": ["grove.read"],
                    "flow_id": "flow-2",
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            )
        ],
        auth=credential(),
    )
    fixture.commit()
    # Independently build the otherwise valid second code redemption with a reused family.
    fixture.prepare(
        [
            effect(
                "credential_consume", 51, credential_id="code-2", use_id=uid(1051), outcome="used"
            ),
            effect(
                "credential_issue",
                52,
                credential_id="access-2",
                binding={
                    "kind": "access_token",
                    "grant": grant(),
                    "scopes": ["grove.read"],
                    "family_id": "family-1",
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            ),
            effect(
                "credential_issue",
                53,
                credential_id="refresh-2",
                binding={
                    "kind": "refresh_token",
                    "grant": grant(),
                    "scopes": ["grove.read"],
                    "family_id": "family-1",
                    "parent_credential_id": None,
                },
                issued_at=AT,
                expires_at=LATER,
                installation_epoch=1,
                authority_epoch=1,
            ),
        ],
        auth=credential("code-2", "oauth_code"),
    )
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        fixture.decode()
