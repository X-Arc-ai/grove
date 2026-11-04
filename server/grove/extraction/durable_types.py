"""Frozen durable protocol data. These records are never authority capabilities."""

from dataclasses import dataclass

from .execution_types import ExecutionBinding


@dataclass(frozen=True)
class OperationMapEntry:
    descriptor_id: str
    operation_index: int


@dataclass(frozen=True)
class PayloadEnvelope:
    schema: str
    writer_revision: str
    binding_digest: str
    # Canonical JSON keeps nested executable data immutable.
    batch: str
    operation_map: tuple[OperationMapEntry, ...]


@dataclass(frozen=True)
class RunWitness:
    actual_run_id: str
    actual_row_version: int
    protocol_run_version: int
    pass_id: str
    input_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedHead:
    work_id: str
    work_version: int
    execution_id: str
    execution_ordinal: int
    request_id: str
    request_version: int
    claim_epoch: int


@dataclass(frozen=True)
class ExecutionRef:
    work_id: str
    execution_id: str
    execution_ordinal: int
    request_version: int
    work_version: int
    claim_epoch: int


@dataclass(frozen=True)
class CompletionRef:
    completion_id: str
    work_id: str
    request_id: str
    execution_id: str
    commit_id: str
    change_set_id: str
    outcome: str


@dataclass(frozen=True)
class HandoffRef:
    handoff_id: str
    version: int
    state: str
    consumer_receipt_id: str | None


@dataclass(frozen=True)
class IntakeOrder:
    stream_anchor_source_id: str
    position: int
    predecessor_progress_id: str | None
    expected_predecessor_version: int | None
    complete_manifest_value_id: str


@dataclass(frozen=True)
class IntakeCommand:
    event_id: str
    new_work_id: str
    new_request_id: str
    new_execution_id: str
    binding: ExecutionBinding
    actual_run_witness: RunWitness
    order: IntakeOrder


@dataclass(frozen=True)
class StoredInvocationRef:
    call_id: str
    request_value_id: str
    result_value_id: str | None
    binding_value_id: str


@dataclass(frozen=True)
class StoredCallRequest:
    normalized_request: str
    provider_request_body: str
    wire_format_version: int = 1


@dataclass(frozen=True)
class StoredCallResult:
    provider_response_text: str
    selected_output_text: str
    normalized_output: str
    normalizer_revision: str


@dataclass(frozen=True)
class RecordedAttemptWitness:
    call_id: str
    model_call_id: str
    actual_run_id: str
    claim_epoch: int
    stage: str
    route: str
    model: str
    job: str
    effort: str
    outcome: str
    wire_request_digest: str
    wire_response_digest: str
    selected_output_digest: str
    normalized_request_digest: str
    normalized_output_digest: str


@dataclass(frozen=True)
class ConsumerReceipt:
    handoff_id: str
    kind: str
    accepted_transaction_id: str
    destination_ids: tuple[str, ...]
    contract_version: int = 1
