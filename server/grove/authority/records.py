"""Closed, immutable and deliberately untrusted authority data.

The registry is code, not an input schema. Generated slotted record classes have
only their declared fields. A record constructor is not a validation capability;
the codec checks every field again before encoding or reducing it.
"""

import re
from dataclasses import dataclass, field, make_dataclass
from hashlib import sha256
from types import MappingProxyType

from .scalars import (
    MAX_COUNT,
    ScalarRefused,
    validate_count,
    validate_digest,
    validate_identifier,
)

REFUSALS = frozenset(
    "input_type byte_limit token_limit node_limit depth_limit allocation_limit "
    "noncanonical duplicate_key unknown_field unsupported_variant scalar digest identity "
    "sequence transition projection_mismatch ledger_mismatch pending sealed closed "
    "event_limit journal_limit saturated reserve_shortfall manifest_limit cancelled".split()
)


class AuthorityCodecRefused(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in REFUSALS else "scalar"
        super().__init__(self.code)


def refuse(code):
    raise AuthorityCodecRefused(code) from None


@dataclass(frozen=True, slots=True)
class _Record:
    _span: object = field(default=None, repr=False, compare=False, kw_only=True)
    _reservation: object = field(default=None, repr=False, compare=False, kw_only=True)


def enum(*values):
    return ("enum", values)


def nullable(spec):
    return ("nullable", spec)


def array(spec, maximum, minimum=0, order=()):
    return ("array", spec, maximum, minimum, order)


def number(low, high):
    return ("number", low, high)


def union(*names):
    return ("union", names)


ID, H, UUID, AT, U, P = "ID", "H", "UUID", "AT", "U", "P"
SEQ = number(1, 32768)
BASE_SEQ = number(0, 32768)
AGENT = enum("grove", "learner", "curator", "connector")
CREDENTIAL_KINDS = tuple(
    sorted(
        (
            "session",
            "oauth_code",
            "access_token",
            "refresh_token",
            "email_code",
            "auth_state",
            "runner_pass",
        )
    )
)
CREDENTIAL_KIND = enum(*CREDENTIAL_KINDS)
SCOPES = array(enum("grove.read", "grove.write", "grove.sensitive_write"), 3, order=("$",))
ITEM_FIELD = enum("title", "summary", "body", "properties", "search_terms", "status")
REGISTRY_FIELDS = (
    ("kinds", ("name", "description", "reason", "status_vocabulary")),
    ("meanings", ("name", "description", "reason")),
    ("property_names", ("name", "description", "reason")),
    ("aliases", ("phrase", "normalised_phrase", "reason")),
    ("capabilities", ("reason",)),
)
REG_TABLE = enum(*(row[0] for row in REGISTRY_FIELDS))
REG_FIELD = enum(
    "name", "phrase", "normalised_phrase", "description", "reason", "status_vocabulary"
)
SOURCE_KIND = enum("logical_snapshot", "history_rebuild", "physical_dump")
RESTRICTION_KINDS = (
    "grant_revoke",
    "credential_revoke",
    "family_revoke",
    "epoch_invalidate",
    "world_close",
    "forget_apply",
    "lineage_invalidate",
    "credential_consume",
)
OWNER_EFFECTS = (
    "class_create",
    "class_label",
    "class_retire",
    "policy_replace",
    "world_exclusions_replace",
    "assignment_set",
    "connection_rule_set",
    "grant_set",
    "grant_revoke",
    "credential_revoke",
    "family_revoke",
    "world_close",
    "forget_apply",
)
CREDENTIAL_EFFECTS = (
    "credential_issue",
    "credential_attempt",
    "credential_consume",
    "credential_revoke",
    "family_revoke",
)
LINEAGE_EFFECTS = (
    "lineage_derivation",
    "lineage_output",
    "lineage_dependency",
    "lineage_invalidate",
)
_schemas = {}


def _define(name, **fields):
    _schemas[name] = tuple(sorted(fields.items()))
    return name


_define("Profile", code=enum("authority_state_v1"), digest=H)
_define("Client", kind=enum("oauth_client"), identity_digest=H)
_define("Resource", kind=enum("oauth_resource"), identity_digest=H)
_define("Ledger", format=enum("grove.forget-ledger.v1"), file_digest=H, rows_digest=H, count=U)
_define("WholeSelector", kind=enum("whole"))
_define("MessageSelector", kind=enum("message"), key_digest=H)
_define("ItemSelector", kind=enum("item_field"), field=ITEM_FIELD, property_id=nullable(ID))
_define("RegistrySelector", kind=enum("registry_field"), table=REG_TABLE, field=REG_FIELD)
_define(
    "FactPredicate",
    version=enum("text_fact_values.v1"),
    kind=enum("local_candidate", "instant", "source_offset", "source_timezone"),
    value_bytes=number(1, 512),
    sha256=H,
)
_define(
    "FactSelector",
    kind=enum("text_fact"),
    key_digest=H,
    fact_set_digest=H,
    predicate="FactPredicate",
)
_define(
    "Ref",
    kind=enum("source", "item", "value", "registry_field"),
    id=ID,
    selector=union(
        "WholeSelector", "MessageSelector", "ItemSelector", "RegistrySelector", "FactSelector"
    ),
)
_define(
    "OwnerApproval",
    kind=enum("owner_approval"),
    user_id=ID,
    session_id=ID,
    approval_id=UUID,
    payload_digest=H,
    display_digest=H,
    expected_revision=U,
    approved_at=AT,
    expires_at=AT,
)
_define(
    "CredentialAuthorization",
    kind=enum("credential_transition"),
    credential_kind=CREDENTIAL_KIND,
    credential_id=ID,
    user_id=nullable(ID),
    installation_epoch=P,
    authority_epoch=P,
    client=nullable("Client"),
    grant_id=nullable(ID),
    family_id=nullable(ID),
)
_define(
    "ProspectiveAuthorization",
    kind=enum("prospective_assignment"),
    rule_id=UUID,
    grant_id=ID,
    grant_epoch=P,
    source_id=ID,
    request_id=ID,
    origin_digest=H,
)
_define(
    "LineageAuthorization",
    kind=enum("observed_lineage"),
    run_id=ID,
    model_call_id=ID,
    configuration_digest=H,
    observation_digest=H,
)
_define(
    "ServiceAuthorization",
    kind=enum("service_transition"),
    configuration_digest=H,
    run_id=nullable(ID),
    agent=AGENT,
)
_define(
    "AccountAuthorization",
    kind=enum("account_flow"),
    flow_id=ID,
    attempt_id=UUID,
    mechanism=enum("google_oidc", "email_challenge"),
    configuration_digest=H,
)
_define(
    "ControllerAuthorization",
    kind=enum("controller_operation"),
    operation_id=UUID,
    controller_receipt_id=ID,
    genesis_digest=H,
    purpose=enum("birth", "adopt", "restore", "recover", "close"),
)
AUTHORIZATION = union(
    "OwnerApproval",
    "CredentialAuthorization",
    "ProspectiveAuthorization",
    "LineageAuthorization",
    "ServiceAuthorization",
    "AccountAuthorization",
    "ControllerAuthorization",
)
_define("Rule", kind=enum("allow_class", "exclude_class"), class_id=ID)
_define("ClassRule", class_id=ID, rule_id=UUID)
_define("Origin", grant_id=ID, grant_epoch=P, request_id=ID, source_id=ID, origin_digest=H)
_define(
    "GrantBinding", grant_id=ID, grant_epoch=P, user_id=ID, client="Client", resource="Resource"
)
_define("SessionBinding", kind=enum("session"), user_id=ID)
_define("CodeBinding", kind=enum("oauth_code"), grant="GrantBinding", scopes=SCOPES, flow_id=ID)
_define(
    "AccessBinding", kind=enum("access_token"), grant="GrantBinding", scopes=SCOPES, family_id=ID
)
_define(
    "RefreshBinding",
    kind=enum("refresh_token"),
    grant="GrantBinding",
    scopes=SCOPES,
    family_id=ID,
    parent_credential_id=nullable(ID),
)
_define(
    "EmailBinding",
    kind=enum("email_code"),
    challenge_id=ID,
    user_id=nullable(ID),
    attempt_limit=number(1, 100),
)
_define(
    "AuthStateBinding",
    kind=enum("auth_state"),
    flow_id=ID,
    provider=enum("google_oidc"),
    user_id=nullable(ID),
)
_define("PassBinding", kind=enum("runner_pass"), run_id=ID, agent=AGENT, configuration_digest=H)
BINDING = union(
    "SessionBinding",
    "CodeBinding",
    "AccessBinding",
    "RefreshBinding",
    "EmailBinding",
    "AuthStateBinding",
    "PassBinding",
)
_effects = []


def _effect(name, kind, **fields):
    _effects.append(_define(name, effect_id=UUID, kind=enum(kind), **fields))


_effect(
    "OriginEffect",
    "origin",
    mode=enum("birth", "adopt"),
    owner_id=ID,
    ledger="Ledger",
    inventory_digest=H,
    host_policy=enum("none"),
)
_effect("ClassCreate", "class_create", class_id=ID, label_value_id=ID)
_effect(
    "ClassLabel", "class_label", class_id=ID, expected_revision=U, revision=P, label_value_id=ID
)
_effect("ClassRetire", "class_retire", class_id=ID, expected_revision=U, revision=P)
_effect(
    "PolicyReplace",
    "policy_replace",
    policy_id=ID,
    grant="GrantBinding",
    expected_revision=U,
    revision=P,
    rules=array("Rule", 256, order=("kind", "class_id")),
    state=enum("active", "revoked"),
    approval_id=UUID,
)
_effect(
    "ExclusionsReplace",
    "world_exclusions_replace",
    expected_revision=U,
    revision=P,
    class_ids=array(ID, 256, order=("$",)),
    approval_id=UUID,
)
_effect(
    "AssignmentSet",
    "assignment_set",
    assignment_id=ID,
    ref="Ref",
    expected_revision=U,
    revision=P,
    classes=array("ClassRule", 256, order=("class_id", "rule_id")),
    origin=nullable("Origin"),
    approval_id=nullable(UUID),
)
_effect(
    "ConnectionRuleSet",
    "connection_rule_set",
    rule_id=UUID,
    class_id=ID,
    grant_id=ID,
    grant_epoch=P,
    expected_revision=U,
    revision=P,
    state=enum("active", "retired"),
    approval_id=UUID,
)
_effect(
    "GrantSet",
    "grant_set",
    grant="GrantBinding",
    expected_epoch=U,
    scopes=SCOPES,
    state=enum("active"),
    approval_id=UUID,
)
_effect(
    "GrantRevoke",
    "grant_revoke",
    grant_id=ID,
    grant_epoch=P,
    expected_revision=U,
    revision=P,
    reason=enum("owner", "client_removed", "security_recovery"),
)
_effect(
    "CredentialIssue",
    "credential_issue",
    credential_id=ID,
    binding=BINDING,
    issued_at=AT,
    expires_at=AT,
    installation_epoch=P,
    authority_epoch=P,
)
_effect(
    "CredentialAttempt",
    "credential_attempt",
    credential_id=ID,
    expected_attempts=U,
    attempts=P,
    outcome=enum("invalid", "accepted", "expired", "locked"),
)
_effect(
    "CredentialConsume",
    "credential_consume",
    credential_id=ID,
    use_id=UUID,
    outcome=enum("used", "reused"),
)
_effect(
    "CredentialRevoke",
    "credential_revoke",
    credential_id=ID,
    reason=enum("owner", "replacement", "reuse", "run_end", "configuration", "recovery"),
)
_effect(
    "FamilyRevoke",
    "family_revoke",
    grant_id=ID,
    grant_epoch=P,
    family_id=ID,
    reason=enum("owner", "reuse", "grant_revoked", "recovery"),
)
_effect(
    "RunState",
    "run_state",
    run_id=ID,
    agent=AGENT,
    configuration_digest=H,
    expected_state=enum("absent", "active"),
    state=enum("active", "completed", "abandoned"),
)
_effect(
    "ConfigurationReplace",
    "configuration_replace",
    expected_configuration_digest=H,
    configuration_digest=H,
    operation_id=UUID,
)
_effect(
    "EpochInvalidate",
    "epoch_invalidate",
    old_epoch=P,
    new_epoch=P,
    kinds=array(CREDENTIAL_KIND, 7, 7, ("$",)),
)
_effect("WorldClose", "world_close", closure_id=UUID, owner_id=ID)
_define(
    "LedgerRowRef",
    id=ID,
    plan_version=enum("legacy", "v2", "v3", "v4"),
    row_digest=H,
    plan_digest=nullable(H),
    extension_digest=nullable(H),
)
_effect(
    "ForgetApply",
    "forget_apply",
    row="LedgerRowRef",
    before_ledger="Ledger",
    after_ledger="Ledger",
    expected_revision=U,
    revision=P,
)
_effect(
    "RestoreBegin",
    "restore_begin",
    operation_id=UUID,
    source_kind=SOURCE_KIND,
    artifact_digest=H,
    inventory_digest=H,
    d3_fingerprint=H,
    profile="Profile",
    old_epoch=P,
    new_epoch=P,
    scope=enum("world", "installation"),
    installation_before=P,
    installation_after=P,
    live_ledger="Ledger",
)
_effect(
    "LineageDerivation",
    "lineage_derivation",
    derivation_id=ID,
    run_id=ID,
    ordinal=number(1, 128),
    previous_id=nullable(ID),
    model_call_id=ID,
    input_count=number(0, 4096),
    input_digest=H,
    seed_count=number(0, 256),
    seed_digest=H,
    state=enum("complete", "unknown"),
    authority_epoch=P,
    created_at=AT,
)
_effect(
    "LineageOutput",
    "lineage_output",
    output_id=ID,
    run_id=ID,
    model_call_id=ID,
    derivation_id=ID,
    tool_call_id=nullable(ID),
    output_kind=enum("proposal", "segment"),
    canonical_digest=nullable(H),
    request_id=nullable(ID),
    conversion=enum("grove.proposal.v1", "grove.output-segment.v1"),
    state=enum("complete", "incomplete", "unknown"),
    authority_epoch=P,
    created_at=AT,
)
_define("ItemSlot", kind=enum("item"), id=ID, field=ITEM_FIELD, property_id=nullable(ID))
_define("LinkSlot", kind=enum("link"), id=ID, field=enum("provenance_reason"))
_define("ValueSlot", kind=enum("value"), id=ID, field=enum("value"))
_define("RegistrySlot", kind=enum("registry"), id=ID, table=REG_TABLE, field=REG_FIELD)
_define("SourceSlot", kind=enum("source"), id=ID, field=enum("content"))
SLOT = union("ItemSlot", "LinkSlot", "ValueSlot", "RegistrySlot", "SourceSlot")
_effect(
    "LineageDependency",
    "lineage_dependency",
    dependency_id=ID,
    output_slot=SLOT,
    output_version=P,
    derivation_id=ID,
    output_id=ID,
    created_at=AT,
)
_effect(
    "LineageInvalidate",
    "lineage_invalidate",
    run_id=ID,
    reason=enum("unbound_input", "unbound_output", "interrupted", "restore_mismatch"),
)
EFFECT = union(*_effects)
_define(
    "Intent",
    intent_id=UUID,
    idempotency_key=UUID,
    authorization=AUTHORIZATION,
    base_sequence=BASE_SEQ,
    base_head_digest=nullable(H),
    base_state_digest=H,
    expected_installation_epoch=P,
    expected_authority_epoch=U,
    expected_erasure_revision=U,
    mode=enum("ordinary", "restrictive", "restore", "origin", "close"),
    effects=array(EFFECT, 4096, 1, ("effect_id",)),
    result_state_digest=H,
    intent_digest=H,
)
_define(
    "Completion",
    intent_id=UUID,
    intent_digest=H,
    applied_state_digest=H,
    ledger="Ledger",
    authority_epoch=P,
    erasure_revision=U,
    completed_at=AT,
)
_define(
    "BirthPayload",
    owner_id=ID,
    installation_id=UUID,
    activation_id=UUID,
    genesis_digest=H,
    empty_ledger="Ledger",
    intent="Intent",
)
_define(
    "AdoptPayload",
    owner_id=ID,
    installation_id=UUID,
    activation_id=UUID,
    genesis_digest=H,
    inventory_digest=H,
    live_store_digest=H,
    ledger="Ledger",
    prior_authority_digest=nullable(H),
    intent="Intent",
)
_define("PreparePayload", intent="Intent")
_define("CommitPayload", completion="Completion")
_define(
    "RecoverPayload",
    operation_id=UUID,
    controller_receipt_id=ID,
    resolution=enum(
        "complete_restriction",
        "confirm_committed",
        "cancel_unapplied_expansion",
        "complete_reapproved",
    ),
    completion="Completion",
    observed_db_digest=H,
    original_approval_digest=nullable(H),
    renewed_approval=nullable("OwnerApproval"),
)
_define(
    "RestorePayload",
    operation_id=UUID,
    controller_receipt_id=ID,
    source_kind=SOURCE_KIND,
    artifact_digest=H,
    inventory_digest=H,
    d3_fingerprint=H,
    old_epoch=P,
    new_epoch=P,
    scope=enum("world", "installation"),
    installation_before=P,
    installation_after=P,
    live_authority_head=H,
    ledger="Ledger",
    intent="Intent",
)
_define("ClosedPayload", completion="Completion", closure_id=UUID)
_define("SealPayload", reason=enum("capacity"))
EVENT_PAYLOADS = (
    ("world_birth", "BirthPayload"),
    ("adopt_world", "AdoptPayload"),
    ("prepare", "PreparePayload"),
    ("commit", "CommitPayload"),
    ("recover", "RecoverPayload"),
    ("restore_epoch", "RestorePayload"),
    ("world_closed", "ClosedPayload"),
    ("seal", "SealPayload"),
)
_define(
    "Event",
    format=enum("grove.authority-event.v1"),
    world_id=ID,
    sequence=SEQ,
    event_id=UUID,
    previous_digest=H,
    kind=enum(*(row[0] for row in EVENT_PAYLOADS)),
    actor_id=nullable(ID),
    approved_at=nullable(AT),
    payload=("dependent", "kind", EVENT_PAYLOADS),
    digest=H,
)
_define(
    "WorldHead",
    world_id=ID,
    owner_id=ID,
    origin_event_id=nullable(UUID),
    origin_digest=nullable(H),
    sequence=BASE_SEQ,
    head_digest=nullable(H),
    ledger=nullable("Ledger"),
    authority_epoch=U,
    erasure_revision=U,
    release_state=enum(
        "uninitialized", "pending", "ready", "no_host_policy", "restoring", "sealed", "closed"
    ),
    pending_id=nullable(UUID),
)
_define(
    "Manifest",
    format=enum("grove.authority-manifest.v1"),
    installation_id=UUID,
    installation_epoch=P,
    activation_id=UUID,
    genesis_digest=H,
    inventory_digest=H,
    schema_profile="Profile",
    state=enum("uninitialized", "ready", "restoring"),
    worlds=array("WorldHead", 2048, order=("world_id",)),
    checksum=H,
)
_define(
    "JournalHeader",
    format=enum("grove.authority-journal.v1"),
    installation_id=UUID,
    world_id=ID,
    owner_id=ID,
    genesis_digest=H,
    schema_profile="Profile",
)
_define(
    "AppliedIntent",
    intent_id=UUID,
    approval_id=nullable(UUID),
    effects=array(EFFECT, 4096, 1, ("effect_id",)),
)
_define("CancelledIntent", intent_id=UUID, intent_digest=H, approval_id=nullable(UUID))
_define(
    "ProjectionBinding",
    installation_id=UUID,
    installation_epoch=P,
    authority_epoch=P,
    sequence=SEQ,
    head_digest=H,
    schema_profile="Profile",
    state_digest=H,
    ledger="Ledger",
    ready="BOOL",
)

SCHEMAS = MappingProxyType(_schemas)
_types = {
    name: make_dataclass(
        name,
        [(key, object) for key, _ in fields],
        bases=(_Record,),
        frozen=True,
        slots=True,
        namespace={"__module__": __name__},
    )
    for name, fields in SCHEMAS.items()
}
TYPES = MappingProxyType(_types)
SCHEMA_NAMES = MappingProxyType({cls: name for name, cls in TYPES.items()})
UntrustedEvent = TYPES["Event"]
UntrustedManifest = TYPES["Manifest"]

# Every semantic switch below is part of the pinned profile, not caller policy.
PROFILE_DESCRIPTOR = (
    "authority_state_v1",
    tuple(SCHEMAS.items()),
    REGISTRY_FIELDS,
    RESTRICTION_KINDS,
    OWNER_EFFECTS,
    CREDENTIAL_EFFECTS,
    LINEAGE_EFFECTS,
    ("legacy", "v2", "v3:area", "v4:area-fact-media"),
    (
        "origin-pending-completion",
        "one-pending",
        "exact-retry-no-append",
        "sticky-seal",
        "sticky-close",
        "all-class-edits-ordinary",
        "exact-reapproval",
        "no-owner-transfer",
        "world-and-installation-epochs-distinct",
        "complete-wrapper-accounting",
    ),
    (
        16000000,
        32768,
        262144,
        14000000,
        30000,
        230000,
        262144,
        8192,
        1024,
        64,
        525312,
        3,
        16448,
        1048576,
        2048,
        12,
        6,
        67108864,
    ),
)
PROFILE_DIGEST = sha256(repr(PROFILE_DESCRIPTOR).encode("ascii")).hexdigest()
PROFILE = TYPES["Profile"](code="authority_state_v1", digest=PROFILE_DIGEST)
_schemas = SCHEMAS
_effects = EFFECT[1]
del _types

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")


def at_microseconds(value):
    """Exact UTC calendar arithmetic; this module has no clock dependency."""
    if type(value) is not str or len(value) != 27 or not _AT.fullmatch(value):
        refuse("scalar")
    year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
    hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not (
        1 <= year <= 9999
        and 1 <= month <= 12
        and 1 <= day <= days[month - 1]
        and hour < 24
        and minute < 60
        and second < 60
    ):
        refuse("scalar")
    prior = year - 1
    ordinal = prior * 365 + prior // 4 - prior // 100 + prior // 400
    ordinal += sum(days[: month - 1]) + day - 1
    return ((ordinal * 24 + hour) * 3600 + minute * 60 + second) * 1000000 + int(value[20:26])


def check_scalar(spec, value):
    try:
        if spec == ID:
            validate_identifier(value)
        elif spec == H:
            validate_digest(value)
        elif spec in (U, P):
            validate_count(value)
            if spec == P and value == 0:
                refuse("scalar")
        elif spec == UUID:
            if (
                type(value) is not str
                or len(value) != 36
                or not _UUID.fullmatch(value)
                or value == "00000000-0000-0000-0000-000000000000"
            ):
                refuse("scalar")
        elif spec == AT:
            at_microseconds(value)
        elif spec == "BOOL":
            if type(value) is not bool:
                refuse("scalar")
        elif type(spec) is tuple and spec[0] == "enum":
            if type(value) is not str or value not in spec[1]:
                refuse("unsupported_variant")
        elif type(spec) is tuple and spec[0] == "number":
            if type(value) is not int or not spec[1] <= value <= spec[2]:
                refuse("scalar")
        else:
            refuse("scalar")
    except ScalarRefused:
        refuse("scalar")
    return value


def check_record(record):
    """Local cross-field invariants; no existence or live authorization claim."""
    name = SCHEMA_NAMES.get(type(record))
    if name is None:
        refuse("input_type")
    r = record
    if name == "Profile" and r != PROFILE:
        refuse("unsupported_variant")
    if name in ("RegistrySelector", "RegistrySlot"):
        if r.field not in dict(REGISTRY_FIELDS)[r.table]:
            refuse("scalar")
    if name in ("ItemSelector", "ItemSlot"):
        if (r.field == "properties") != (r.property_id is not None):
            refuse("scalar")
    if name == "Ref":
        allowed = {
            "whole": ("source", "item", "value"),
            "message": ("source",),
            "text_fact": ("source",),
            "item_field": ("item",),
            "registry_field": ("registry_field",),
        }
        if r.kind not in allowed[r.selector.kind]:
            refuse("scalar")
    if name == "FactPredicate":
        lengths = {"local_candidate": (14, 16, 17, 19), "instant": (20,), "source_offset": (6,)}
        if r.kind in lengths and r.value_bytes not in lengths[r.kind]:
            refuse("scalar")
    if name == "OwnerApproval":
        age = at_microseconds(r.expires_at) - at_microseconds(r.approved_at)
        if not 0 < age <= 300000000:
            refuse("scalar")
    if name == "CredentialAuthorization":
        oauth = r.credential_kind in ("oauth_code", "access_token", "refresh_token")
        family = r.credential_kind in ("access_token", "refresh_token")
        if (
            oauth != (r.client is not None)
            or oauth != (r.grant_id is not None)
            or family != (r.family_id is not None)
            or (oauth and r.user_id is None)
        ):
            refuse("scalar")
    if name in (
        "ClassLabel",
        "ClassRetire",
        "PolicyReplace",
        "ExclusionsReplace",
        "AssignmentSet",
        "ConnectionRuleSet",
        "GrantRevoke",
        "ForgetApply",
    ):
        if r.expected_revision == MAX_COUNT or r.revision != r.expected_revision + 1:
            refuse("scalar")
    if name == "PolicyReplace" and r.state == "revoked" and r.rules:
        refuse("scalar")
    if name == "AssignmentSet":
        if r.approval_id is None and r.origin is None:
            refuse("scalar")
        if len({c.class_id for c in r.classes}) != len(r.classes):
            refuse("scalar")
    if name == "GrantSet" and (
        r.expected_epoch == MAX_COUNT or r.grant.grant_epoch != r.expected_epoch + 1
    ):
        refuse("scalar")
    if name == "CredentialIssue" and at_microseconds(r.issued_at) >= at_microseconds(r.expires_at):
        refuse("scalar")
    if name == "CredentialAttempt" and (
        r.expected_attempts == MAX_COUNT or r.attempts != r.expected_attempts + 1
    ):
        refuse("scalar")
    if name in ("EpochInvalidate", "RestoreBegin", "RestorePayload"):
        if r.old_epoch == MAX_COUNT or r.new_epoch != r.old_epoch + 1:
            refuse("scalar")
    if name == "EpochInvalidate" and r.kinds != CREDENTIAL_KINDS:
        refuse("scalar")
    if name in ("RestoreBegin", "RestorePayload"):
        delta = 1 if r.scope == "installation" else 0
        if r.installation_after != r.installation_before + delta:
            refuse("scalar")
    if name == "LedgerRowRef":
        if (r.plan_version != "legacy") != (r.plan_digest is not None) or (
            r.plan_version in ("v3", "v4")
        ) != (r.extension_digest is not None):
            refuse("scalar")
    if name == "ForgetApply" and (
        r.before_ledger.count == MAX_COUNT or r.after_ledger.count != r.before_ledger.count + 1
    ):
        refuse("ledger_mismatch")
    if name == "LineageDerivation" and (r.ordinal == 1) != (r.previous_id is None):
        refuse("scalar")
    if name == "LineageOutput":
        if (r.state == "complete") != (r.canonical_digest is not None):
            refuse("scalar")
        if r.output_kind == "proposal":
            if (
                r.request_id is None
                or r.tool_call_id is None
                or r.conversion != "grove.proposal.v1"
            ):
                refuse("scalar")
        elif r.request_id is not None or r.conversion != "grove.output-segment.v1":
            refuse("scalar")
    if name == "Intent" and (r.base_sequence == 0) != (r.base_head_digest is None):
        refuse("scalar")
    if name == "RecoverPayload":
        if (r.resolution == "complete_reapproved") != (r.renewed_approval is not None):
            refuse("scalar")
    if name == "WorldHead":
        if r.sequence == 0:
            if (
                r.release_state != "uninitialized"
                or r.authority_epoch
                or r.erasure_revision
                or any(
                    v is not None
                    for v in (r.origin_event_id, r.origin_digest, r.head_digest, r.pending_id)
                )
            ):
                refuse("identity")
        elif (
            r.authority_epoch == 0
            or r.ledger is None
            or r.origin_event_id is None
            or r.origin_digest is None
            or r.head_digest is None
            or r.release_state == "uninitialized"
        ):
            # Pending origin is the one initialized-looking head with epoch zero.
            if not (
                r.sequence == 1
                and r.authority_epoch == 0
                and r.erasure_revision == 0
                and r.pending_id is not None
                and r.release_state == "pending"
                and r.ledger is not None
                and r.origin_event_id is not None
                and r.origin_digest is not None
                and r.head_digest is not None
            ):
                refuse("identity")
        if (r.release_state == "pending" and r.pending_id is None) or (
            r.release_state in ("ready", "no_host_policy", "closed", "uninitialized")
            and r.pending_id is not None
        ):
            refuse("identity")
    return r


@dataclass(frozen=True, slots=True)
class Metrics:
    bytes: int
    nodes: int
    depth: int
    events: int = 0


@dataclass(frozen=True, slots=True)
class UntrustedJournal:
    header: object
    metrics: Metrics
    _data: object = field(repr=False)
    _offsets: object = field(repr=False)
    _budget: object = field(repr=False, compare=False)
    _reservation: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class VerificationRequirement:
    code: str
    event_id: str
    intent_id: str | None
    binding_digest: str


@dataclass(frozen=True, slots=True)
class UntrustedReduction:
    journal: UntrustedJournal
    state_digest: str
    installation_epoch: int
    authority_epoch: int
    erasure_revision: int
    ledger: object
    release_state: str
    pending_id: str | None
    sealed: bool
    closed: bool
    requirements: tuple
    _state: object = field(repr=False, compare=False)
    _reservation: object = field(repr=False, compare=False)
