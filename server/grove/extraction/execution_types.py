"""Immutable untrusted protocol data. None of these records confers authority."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DigestRef:
    category: str
    digest: str


@dataclass(frozen=True)
class VersionRef:
    category: str
    id: str
    version: int


@dataclass(frozen=True)
class Membership:
    category: str
    ids: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class PrincipalRef:
    principal_id: str
    grant_id: str
    policy_revision: int
    privacy_revision: int
    origin_id: str
    admission_id: str
    world_kind: str


@dataclass(frozen=True)
class RunRef:
    id: str
    version: int
    agent: str
    input_ids: tuple[str, ...]
    outcome: str
    deadline: int
    token_cap: int
    pass_id: str


@dataclass(frozen=True)
class WorkRef:
    id: str
    version: int
    request_id: str
    request_version: int
    content_key: str
    source_id: str
    claim_epoch: int
    claimed_by: str | None
    lease_deadline: int
    transport_attempts: int
    regrounds: int
    writer_repairs: int


@dataclass(frozen=True)
class ProducerIdentity:
    id: str
    version: int
    connector_type: str
    provider: str
    parser: str
    batch_rule: str


@dataclass(frozen=True)
class SourceRef:
    id: str
    version: int
    connector_id: str
    content: str
    speakers: str
    content_digest: str
    metadata_digest: str
    screening_id: str
    received_at: str
    episode_ids: tuple[str, ...]
    forget_state: str


@dataclass(frozen=True)
class EpisodeRef:
    id: str
    version: int
    connector_id: str
    source_ids: tuple[str, ...]
    first_key: str
    last_key: str
    cut_rule: str
    start: str
    end: str


@dataclass(frozen=True)
class RetainedMessageRef:
    key: str
    locator: str
    text_digest: str
    source_time: str
    speaker_kind: str
    modality: str
    sender: str
    original_author: str | None
    eligibility: str
    reason: str | None


@dataclass(frozen=True)
class ContextRef:
    capture_id: str
    read_generation: int
    history_sequence: int
    dependencies: tuple[VersionRef, ...]
    memberships: tuple[Membership, ...]


@dataclass(frozen=True)
class ModelStep:
    stage: str
    route: str
    model: str
    job: str
    effort: str
    max_calls: int
    request_limit: int
    output_limit: int


@dataclass(frozen=True)
class ExecutionBinding:
    profile: str
    codec: int
    source_revision: str
    schema_digest: str
    prompt_digests: tuple[DigestRef, ...]
    translator: str
    world_id: str
    principal: PrincipalRef
    run: RunRef
    work: WorkRef
    producer: ProducerIdentity
    source: SourceRef
    episode: EpisodeRef
    inventory: tuple[RetainedMessageRef, ...]
    selected_keys: tuple[str, ...]
    context: ContextRef
    model_steps: tuple[ModelStep, ...]
    writer_ops: tuple[str, ...]


@dataclass(frozen=True)
class Invocation:
    id: str
    binding_digest: str
    stage: str
    ordinal: int
    request_digest: str
    route: str
    model: str
    job: str
    effort: str
    state: str
    model_call_id: str | None
    output_digest: str | None
    request: str
    output: str | None
    run_id: str
    claim_epoch: int


@dataclass(frozen=True)
class CandidateClass:
    locator: str
    statement_class: str


@dataclass(frozen=True)
class ClassOutcome:
    statement_class: str
    final_class: str
    outcome: str
    operation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    adjudication_id: str | None


@dataclass(frozen=True)
class Disposition:
    key: str
    locator: str
    state: str
    reason: str | None
    classes: tuple[ClassOutcome, ...]


@dataclass(frozen=True)
class OperationRef:
    id: str
    op: str
    payload_digest: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceRef:
    id: str
    locator: str
    statement_class: str


@dataclass(frozen=True)
class Proposal:
    binding_digest: str
    operations: tuple[OperationRef, ...]
    evidence: tuple[EvidenceRef, ...]
    dispositions: tuple[Disposition, ...]
    invocations: tuple[Invocation, ...]


@dataclass(frozen=True)
class TriageTrace:
    invocation_id: str
    request_digest: str
    output_digest: str
    reviewed_locators: tuple[str, ...]
    candidates: tuple[CandidateClass, ...]


@dataclass(frozen=True)
class CompletionPlan:
    binding_digest: str
    proposal_digest: str
    disposition_digest: str
    outcome: str
    messages: int
    blank_messages: int
    empty_messages: int
    extracted_messages: int
    operation_ids: tuple[str, ...]


@dataclass(frozen=True)
class CounterDelta:
    held_nothing: int
    extracted: int
    next_cursor: int


@dataclass(frozen=True)
class ExecutionState:
    binding: ExecutionBinding
    revision: int = 0
    predecessor: str | None = None
    status: str = "pending"
    request_status: str = "queued"
    invocations: tuple[Invocation, ...] = ()
    active_invocation: str | None = None
    proposal: Proposal | None = None
    completion: CompletionPlan | None = None
    completion_id: str | None = None
    declined_digests: tuple[str, ...] = ()
    retry_at: int | None = None
    reason: str | None = None
    claim_epoch: int = 0
    request_version: int = 1
    work_version: int = 1
    run_version: int = 1
    claimed_by: str | None = None
    transport_attempts: int = 0
    regrounds: int = 0
    writer_repairs: int = 0
    rebind_required: int = 0
    prior_invocations: tuple[Invocation, ...] = ()
    triage: TriageTrace | None = None
    last_event_at: int = 0
    repair_after: int = 0
    active_pass_id: str | None = None
    active_input_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionEvent:
    kind: str
    expected_revision: int
    claim_epoch: int
    now: int
    invocation: Invocation | None = None
    proposal: Proposal | None = None
    triage: TriageTrace | None = None
    current: ExecutionBinding | None = None
    completion_id: str | None = None
    reason: str | None = None
    retry_at: int | None = None
