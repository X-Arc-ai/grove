"""Closed local commands and bounded native facts for the inactive authority writer.

Constructing a record grants no authority. Every transaction checks its exact
live owner, issued command identity, publication session and native World lock.
Credentials and private journal input never enter a detached outcome.
"""

import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime

from grove.authority.codec import DecoderBudget, event_digest, record_metrics
from grove.authority.publication import PendingPublication, refuse
from grove.authority.records import PROFILE, SCHEMA_NAMES, SCHEMAS, TYPES, check_scalar

MAX_ROWS = 32768
MAX_ROW_BYTES = 16384


def copy_record(value):
    """Copy only declared bounded fields; never keep a decoder reservation."""
    if type(value) is tuple:
        return tuple(copy_record(v) for v in value)
    name = SCHEMA_NAMES.get(type(value))
    if name is not None:
        return TYPES[name](**{key: copy_record(getattr(value, key)) for key, _ in SCHEMAS[name]})
    return value


def codec_record(value, name):
    if type(value) is not TYPES[name]:
        refuse("binding")
    budget = DecoderBudget()
    try:
        record_metrics(value, budget=budget)
        value = copy_record(value)
        if name == "Event" and value.digest != event_digest(value):
            refuse("binding")
    finally:
        budget.close()
    return copy_record(value)


def instant(value):
    check_scalar("AT", value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeBinding:
    owner: object = field(repr=False)
    lifetime_id: str
    operation_id: str
    world_id: str
    at: str

    def __post_init__(self):
        for value in (self.lifetime_id, self.operation_id):
            check_scalar("UUID", value)
        check_scalar("ID", self.world_id)
        check_scalar("AT", self.at)


def check_binding(binding, tx):
    from grove.writer.authority_adapter import InactiveAuthorityWriter

    if type(binding) is not RuntimeBinding or type(binding.owner) is not InactiveAuthorityWriter:
        refuse("binding", "outcome")
    binding.owner._assert_binding(binding, tx)
    return binding.owner


def check_command(command, tx):
    owner = check_binding(command.binding, tx)
    owner._assert_command(command)
    return owner


class _Command:
    def __post_init__(self):
        if type(self.binding) is not RuntimeBinding:
            refuse("binding")
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in {
                "attempt_id",
                "operation_id",
                "intent_id",
                "old_attempt_id",
                "barrier_id",
            }:
                check_scalar("UUID", value)
            elif f.name in {"expected_fence", "old_fence", "fence"}:
                check_scalar("U" if f.name == "expected_fence" else "P", value)
            elif f.name == "intent_digest":
                check_scalar("H", value)
            elif f.name in {"prepare", "completion", "resolution", "seal"}:
                object.__setattr__(self, f.name, codec_record(value, "Event"))
            elif f.name in {"expected_projection", "current_projection", "target", "before"}:
                object.__setattr__(self, f.name, codec_record(value, "ProjectionBinding"))
            elif f.name == "pending":
                if type(value) is not PendingPublication or (
                    value.world_id != self.binding.world_id
                    or value.operation_id != self.binding.operation_id
                ):
                    refuse("binding")
        if hasattr(self, "prepare") and self.prepare.kind != "prepare":
            refuse("adapter_missing")


@dataclass(frozen=True, slots=True)
class AttemptCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    prepare: object
    pending: PendingPublication
    expected_projection: object
    expected_fence: int
    attempt_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class InspectionCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    intent_id: str
    intent_digest: str
    current_projection: object


@dataclass(frozen=True, slots=True)
class BarrierCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    prepare: object
    pending: PendingPublication
    old_attempt_id: str
    old_fence: int
    barrier_id: str


@dataclass(frozen=True, slots=True)
class ApplyCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    prepare: object
    pending: PendingPublication
    attempt_id: str
    fence: int
    legacy: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class CancelCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    prepare: object
    resolution: object
    pending: PendingPublication
    proof: object


@dataclass(frozen=True, slots=True)
class FinalizeCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    pending: PendingPublication
    completion: object
    target: object
    expected_fence: int


@dataclass(frozen=True, slots=True)
class SealCommand(_Command):
    binding: RuntimeBinding = field(repr=False)
    seal: object
    before: object
    target: object
    expected_fence: int


@dataclass(frozen=True, slots=True)
class BarrierProof:
    lifetime_id: str
    operation_id: str
    installation_id: str
    world_id: str
    intent_id: str
    intent_digest: str
    old_attempt_id: str
    old_fence: int
    barrier_id: str
    barrier_fence: int
    installation_epoch: int
    authority_epoch: int
    erasure_revision: int
    base_state_digest: str
    ledger: object
    d3_sequence: int
    read_generation: int

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "ledger":
                object.__setattr__(self, f.name, codec_record(value, "Ledger"))
            else:
                kind = (
                    "ID"
                    if f.name == "world_id"
                    else "H"
                    if f.name in {"intent_digest", "base_state_digest"}
                    else "P"
                    if f.name
                    in {"old_fence", "barrier_fence", "installation_epoch", "authority_epoch"}
                    else "U"
                    if f.name in {"erasure_revision", "d3_sequence", "read_generation"}
                    else "UUID"
                )
                check_scalar(kind, value)


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalAccess:
    """Actual approval-owner result. Never persist or print its bearer value."""

    approval: object
    target_digest: str
    token: str | None = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "approval", codec_record(self.approval, "OwnerApproval"))
        check_scalar("H", self.target_digest)
        if self.token is not None and (type(self.token) is not str or len(self.token) > 128):
            refuse("binding")


def _schema(groups):
    return {name: kind for kind, names in groups for name in names.split()}


PROJECTION_FIELDS = _schema(
    (
        ("ID", "world_id owner_id"),
        ("UUID", "installation_id"),
        ("profile", "profile_code"),
        ("H", "profile_digest head_digest state_digest ledger_file_digest ledger_rows_digest"),
        ("P", "installation_epoch authority_epoch"),
        ("U", "erasure_revision ledger_count fence"),
        ("SEQ", "sequence"),
        ("format", "ledger_format"),
        ("BOOL", "ready"),
        ("UUID?", "active_intent_id"),
    )
)
REGISTRATION_FIELDS = _schema(
    (
        ("UUID", "id idempotency_key installation_id prepare_event_id"),
        ("ID", "world_id owner_id"),
        (
            "H",
            "profile_digest intent_digest prepare_head_digest "
            "base_state_digest expected_result_digest",
        ),
        ("SEQ", "prepare_sequence"),
        ("BASE", "base_sequence"),
        ("H?", "base_head_digest choice_digest"),
        ("P", "installation_epoch authority_epoch"),
        ("U", "erasure_revision"),
        ("mode", "mode"),
        ("UUID?", "original_approval_id"),
        ("AT", "registered_at"),
    )
)
ATTEMPT_FIELDS = _schema(
    (
        ("phase", "phase"),
        ("UUID", "attempt_id operation_id"),
        ("P", "attempt_fence"),
        ("U", "revoked_through"),
        ("UUID?", "barrier_id"),
        ("P?", "barrier_fence"),
    )
)
TERMINAL_FIELDS = _schema(
    (
        ("outcome", "outcome"),
        ("H", "result_state_digest result_ledger_file_digest result_ledger_rows_digest"),
        ("P", "result_installation_epoch result_authority_epoch"),
        ("U", "result_erasure_revision result_ledger_count d3_sequence_before d3_sequence_after"),
        ("format", "result_ledger_format"),
        ("AT", "terminal_at"),
        ("resolution", "resolution"),
        ("UUID?", "renewed_approval_id"),
    )
)
USE_FIELDS = _schema(
    (
        ("UUID", "id approval_id installation_id intent_id"),
        ("ID", "world_id user_id"),
        ("H", "choice_digest display_digest"),
        ("P", "installation_epoch authority_epoch"),
        ("use_kind", "use_kind"),
        ("role", "role"),
        ("AT", "used_at"),
    )
)
ENUMS = {
    "profile": ("authority_state_v1",),
    "format": ("grove.forget-ledger.v1",),
    "mode": ("ordinary", "restrictive", "restore", "origin", "close"),
    "phase": ("active", "fenced", "applied", "cancelled"),
    "outcome": ("COMMITTED_EXACT", "CANCELLED_EXACT"),
    "resolution": (
        "apply",
        "complete_restriction",
        "complete_reapproved",
        "cancel_unapplied_expansion",
    ),
    "use_kind": ("applied", "cancelled"),
    "role": ("original", "renewed"),
}


def validate_row(kind, raw):
    """Strict native row variants, nullable absence, and bounded safe facts."""
    if type(raw) is not dict:
        refuse("binding", "outcome")
    schema = {"projection": PROJECTION_FIELDS, "use": USE_FIELDS}.get(kind)
    if kind == "operation":
        schema = REGISTRATION_FIELDS | ATTEMPT_FIELDS
        if raw.get("phase") in ("applied", "cancelled"):
            schema = schema | TERMINAL_FIELDS
    if schema is None or set(raw) - schema.keys():
        refuse("binding", "outcome")
    row = {}
    for key, spec in schema.items():
        value = raw.get(key)
        if spec.endswith("?") and value is None:
            row[key] = None
            continue
        spec = spec.rstrip("?")
        if spec == "AT":
            if not isinstance(value, datetime) or value.utcoffset() != UTC.utcoffset(value):
                refuse("binding", "outcome")
            check_scalar("AT", value.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        elif spec in ENUMS:
            check_scalar(("enum", ENUMS[spec]), value)
        else:
            check_scalar(
                {"SEQ": ("number", 1, MAX_ROWS), "BASE": ("number", 0, MAX_ROWS)}.get(spec, spec),
                value,
            )
        row[key] = value
    if "profile_digest" in row and row["profile_digest"] != PROFILE.digest:
        refuse("binding", "outcome")
    if kind == "operation":
        if (row["base_sequence"] == 0) != (row["base_head_digest"] is None):
            refuse("binding", "outcome")
        if (row["original_approval_id"] is None) != (row["choice_digest"] is None):
            refuse("binding", "outcome")
        terminal = row["phase"] in ("applied", "cancelled")
        barrier = row["barrier_id"] is not None and row["barrier_fence"] is not None
        if (row["phase"] == "fenced" and not barrier) or (row["phase"] == "active" and barrier):
            refuse("binding", "outcome")
        if (row["barrier_id"] is None) != (row["barrier_fence"] is None):
            refuse("binding", "outcome")
        if terminal and (row["outcome"] == "CANCELLED_EXACT") != (row["phase"] == "cancelled"):
            refuse("binding", "outcome")
        if terminal and (row["resolution"] == "complete_reapproved") != (
            row["renewed_approval_id"] is not None
        ):
            refuse("binding", "outcome")
    if kind == "use" and row["id"] != row["approval_id"]:
        refuse("binding", "outcome")
    # Scalars were bounded above, before serialization allocates a result.
    if len(json.dumps(row, default=lambda v: v.isoformat(), separators=(",", ":"))) > MAX_ROW_BYTES:
        refuse("capacity", "outcome")
    return row


def projection_binding(row):
    row = validate_row("projection", row)
    return TYPES["ProjectionBinding"](
        installation_id=row["installation_id"],
        installation_epoch=row["installation_epoch"],
        authority_epoch=row["authority_epoch"],
        sequence=row["sequence"],
        head_digest=row["head_digest"],
        schema_profile=PROFILE,
        state_digest=row["state_digest"],
        ready=row["ready"],
        ledger=TYPES["Ledger"](
            format=row["ledger_format"],
            file_digest=row["ledger_file_digest"],
            rows_digest=row["ledger_rows_digest"],
            count=row["ledger_count"],
        ),
    )


@dataclass(frozen=True, slots=True)
class NativeOutcome:
    outcome: str
    projection: object
    erasure_revision: int
    fence: int
    operation: tuple = ()
    proof: BarrierProof | None = None

    def __post_init__(self):
        if self.outcome not in {
            "UNKNOWN",
            "COMMITTED_EXACT",
            "CANCELLED_EXACT",
            "NOT_APPLIED_PROVEN",
        }:
            refuse("binding")
        object.__setattr__(self, "projection", codec_record(self.projection, "ProjectionBinding"))
        check_scalar("U", self.erasure_revision)
        check_scalar("U", self.fence)
        if type(self.operation) is not tuple or len(self.operation) > 64:
            refuse("capacity")
        if self.operation:
            validate_row("operation", dict(self.operation))
        if self.proof is not None and type(self.proof) is not BarrierProof:
            refuse("binding")
