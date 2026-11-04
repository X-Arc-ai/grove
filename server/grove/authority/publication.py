"""Inactive publication data and explicit owner contracts; no resource authority.

A trusted owner must implement every protocol method against live resources.
These records are comparisons, never transferable permissions or receipts.
"""

from dataclasses import dataclass
from typing import Protocol

from .codec import record_metrics
from .lease import LockIdentity, RootIdentity
from .records import PROFILE, TYPES, VerificationRequirement, check_scalar
from .scalars import validate_count, validate_digest, validate_identifier

CODES = frozenset(
    "binding missing private file_type changed io capacity candidate conflict pending "
    "outcome_unknown adapter_missing cancelled recovery_required".split()
)
STAGES = frozenset(
    "open inspect stage prepare legacy outcome completion metadata "
    "manifest final recovery seal close".split()
)
OUTCOMES = frozenset({"COMMITTED_EXACT", "CANCELLED_EXACT", "NOT_APPLIED_PROVEN", "UNKNOWN"})
MAX_CHUNK = 65536
MAX_ENTRIES = 4096
MAX_CANDIDATES = 6
MAX_TEMP_BYTES = 51145728
REMAINING = (
    "trusted_owner_implementation",
    "native_database_outcome",
    "current_principal",
    "source_and_consent",
    "d10_d11_erasure_closure",
    "final_release",
    "power_loss_durability",
)


class PublicationRefused(ValueError):
    def __init__(self, code, stage="inspect"):
        self.code = code if type(code) is str and code in CODES else "binding"
        self.stage = stage if type(stage) is str and stage in STAGES else "inspect"
        super().__init__(f"{self.stage}:{self.code}")


def refuse(code, stage="inspect"):
    raise PublicationRefused(code, stage) from None


def _requirements(value):
    if type(value) is not tuple or len(value) > 262144:
        refuse("capacity")
    for item in value:
        if type(item) is not VerificationRequirement:
            refuse("binding")
        validate_identifier(item.code)
        check_scalar("UUID", item.event_id)
        if item.intent_id is not None:
            check_scalar("UUID", item.intent_id)
        validate_digest(item.binding_digest)


@dataclass(frozen=True, slots=True)
class PublicationBindings:
    installation_id: str
    activation_id: str
    genesis_digest: str
    inventory_digest: str
    profile: object
    root_identity: RootIdentity
    lock_identity: LockIdentity
    authority_identity: RootIdentity
    store_identity_digest: str

    def __post_init__(self):
        check_scalar("UUID", self.installation_id)
        check_scalar("UUID", self.activation_id)
        for value in (self.genesis_digest, self.inventory_digest, self.store_identity_digest):
            validate_digest(value)
        if self.profile != PROFILE or type(self.profile) is not TYPES["Profile"]:
            refuse("binding", "open")
        if (
            type(self.root_identity) is not RootIdentity
            or type(self.lock_identity) is not LockIdentity
        ):
            refuse("binding", "open")
        if type(self.authority_identity) is not RootIdentity:
            refuse("binding", "open")
        if self.root_identity.device != self.authority_identity.device:
            refuse("binding", "open")


@dataclass(frozen=True, slots=True)
class ObservedFile:
    role: str
    device: int
    inode: int
    mode: int
    owner_uid: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str

    def __post_init__(self):
        if self.role not in ("manifest", "journal", "legacy"):
            refuse("binding")
        for value in (
            self.device,
            self.inode,
            self.mode,
            self.owner_uid,
            self.link_count,
            self.size,
        ):
            validate_count(value)
        validate_digest(self.digest)


@dataclass(frozen=True, slots=True)
class UntrustedFileState:
    world_id: str
    owner_id: str
    installation_epoch: int
    authority_epoch: int
    erasure_revision: int
    sequence: int
    head_digest: str
    state_digest: str
    ledger: object
    files: tuple
    state: str
    pending_id: str | None
    requirements: tuple
    projection: object

    def __post_init__(self):
        validate_identifier(self.world_id)
        validate_identifier(self.owner_id)
        for value in (
            self.installation_epoch,
            self.authority_epoch,
            self.erasure_revision,
            self.sequence,
        ):
            validate_count(value)
        validate_digest(self.head_digest)
        validate_digest(self.state_digest)
        if self.pending_id is not None:
            check_scalar("UUID", self.pending_id)
        if self.state not in (
            "pending",
            "restoring",
            "ready",
            "no_host_policy",
            "sealed",
            "closed",
            "saturated",
        ):
            refuse("binding")
        if (
            type(self.files) is not tuple
            or len(self.files) != 3
            or any(type(f) is not ObservedFile for f in self.files)
        ):
            refuse("binding")
        if tuple(f.role for f in self.files) != ("manifest", "journal", "legacy"):
            refuse("binding")
        if (
            type(self.ledger) is not TYPES["Ledger"]
            or type(self.projection) is not TYPES["ProjectionBinding"]
        ):
            refuse("binding")
        _requirements(self.requirements)


@dataclass(frozen=True, slots=True)
class ProposedPublication:
    initial_event: object
    completion_template: object
    pending_manifest: object
    completed_manifest: object
    seal_event: object | None = None


@dataclass(frozen=True, slots=True)
class PendingPublication:
    session_id: str
    world_id: str
    intent_id: str
    intent_digest: str
    operation_id: str
    state: UntrustedFileState
    next_stage: str

    def __post_init__(self):
        for value in (self.session_id, self.intent_id, self.operation_id):
            check_scalar("UUID", value)
        validate_identifier(self.world_id)
        validate_digest(self.intent_digest)
        if type(self.state) is not UntrustedFileState or self.next_stage not in (
            "legacy",
            "outcome",
            "metadata",
            "retry",
        ):
            refuse("binding")


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    lifetime_id: str
    operation_id: str
    bindings: PublicationBindings
    world_id: str
    intent_id: str
    intent_digest: str
    outcome: str
    owner_id: str
    erasure_revision: int
    state_digest: str
    projection: object
    ledger: object
    requirements: tuple

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            refuse("binding", "outcome")
        for value in (self.lifetime_id, self.operation_id, self.intent_id):
            check_scalar("UUID", value)
        validate_identifier(self.world_id)
        validate_identifier(self.owner_id)
        validate_count(self.erasure_revision)
        validate_digest(self.intent_digest)
        validate_digest(self.state_digest)
        record_metrics(self.projection)
        record_metrics(self.ledger)
        _requirements(self.requirements)


@dataclass(frozen=True, slots=True)
class CheckObservation:
    lifetime_id: str
    operation_id: str
    bindings: PublicationBindings
    state: UntrustedFileState
    requirements: tuple
    intent_digest: str | None
    prior_outcome: OutcomeObservation | None = None

    def __post_init__(self):
        check_scalar("UUID", self.lifetime_id)
        check_scalar("UUID", self.operation_id)
        if (
            type(self.bindings) is not PublicationBindings
            or type(self.state) is not UntrustedFileState
        ):
            refuse("binding")
        if self.intent_digest is not None:
            validate_digest(self.intent_digest)
        if self.prior_outcome is not None and type(self.prior_outcome) is not OutcomeObservation:
            refuse("binding")
        _requirements(self.requirements)


@dataclass(frozen=True, slots=True)
class LocalPublicationResult:
    stage: str
    state: UntrustedFileState
    requirements: tuple
    code: str
    remaining: tuple = REMAINING


@dataclass(frozen=True, slots=True)
class RecoveryRequirements:
    stage: str
    intent_id: str | None
    resolutions: tuple
    requirements: tuple
    state: UntrustedFileState


@dataclass(frozen=True, slots=True)
class InitializationRequirements:
    mode: str
    bindings: PublicationBindings
    missing: tuple


def inspect_initialization(request):
    """Describe unmet requirements; this unit never provisions or adopts."""
    if (
        type(request) is not InitializationRequirements
        or request.mode
        not in (
            "new_empty",
            "existing",
            "world_birth",
            "world_adopt",
            "restore",
        )
        or type(request.bindings) is not PublicationBindings
    ):
        refuse("binding", "open")
    return InitializationRequirements(
        request.mode,
        request.bindings,
        (
            "controller_custody",
            "actual_resource_provenance",
            "complete_inventory",
            "current_restrictions",
            "quiescent_writers",
            "bootstrap_owner_not_implemented",
        ),
    )


class OwnerLifetime(Protocol):
    """Live owner context. assert_current must consult the actual worker owner."""

    lifetime_id: str
    operation_id: str

    def __enter__(self): ...
    def __exit__(self, exc_type, exc, tb): ...
    def assert_current(self, bindings, world_id, operation_kind): ...


class PublicationOwner(Protocol):
    """Absent by default; no production implementation belongs to this unit."""

    def operation(self, bindings, world_id, operation_kind) -> OwnerLifetime: ...
    def check_current(self, state, proposal) -> CheckObservation: ...
    def check_legacy_extension(self, pending, before, after, row) -> CheckObservation: ...
    def resolve_outcome(self, pending) -> OutcomeObservation: ...
    def finalize_projection(self, pending, exact_target_projection) -> OutcomeObservation: ...
    def check_recovery(self, state, operation, resolution_event) -> CheckObservation: ...
    def check_final(self, state, exact_target_projection) -> CheckObservation: ...
