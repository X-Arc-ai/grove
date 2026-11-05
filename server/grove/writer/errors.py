"""What the writer raises: a decline names each refused operation; a failure is never a receipt."""

from dataclasses import dataclass, field
from typing import Any

# B.3's typed errors, by HTTP status.
_BY_STATUS: dict[int, tuple[str, ...]] = {
    400: ("invalid_arguments",),
    401: ("unauthenticated",),
    403: ("scope_denied", "approval_required"),
    404: ("not_found",),
    409: ("version_conflict", "stale_proposal"),
    422: (
        "unknown_kind",
        "unknown_meaning",
        "unknown_status",
        "endpoint_kind_mismatch",
        "cardinality_conflict",
        "ended_target",
        "missing_provenance",
        "unresolvable_locator",
        "caller_source_type",
        "missing_became_true",
        "end_before_start",
        "missing_recheck",
        "record_immutable",
        "terminal_needs_confirmation",
        "near_duplicate",
        "not_confirmable",
        "judge_not_supports",
        "inferred_evidence",
        "unmapped_status",
        "contamination",
        "verbatim_required",
        "unrecorded_verdict",
        "caller_score",
        "second_judge_required",
        "judges_disagree",
        "state_property_on_item",
    ),
    429: ("rate_limited", "budget_reached"),
    503: ("unavailable",),
}
HTTP_STATUS: dict[str, int] = {
    code: status for status, codes in _BY_STATUS.items() for code in codes
}


@dataclass(frozen=True)
class Refusal:
    """One refused operation. op_id is None for a fault of the batch as a whole."""

    op_id: str | None
    op: str | None
    code: str
    rule: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "op": self.op,
            "code": self.code,
            "rule": self.rule,
            "message": self.message,
            "details": self.details,
        }


class Refused(Exception):
    """Raised inside the writer by a check; the batch loop turns it into a Refusal."""

    def __init__(
        self, code: str, rule: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code} ({rule}): {message}")
        self.code = code
        self.rule = rule
        self.message = message
        self.details = details or {}

    def refusal(self, op_id: str | None, op: str | None) -> Refusal:
        return Refusal(op_id, op, self.code, self.rule, self.message, self.details)


@dataclass(frozen=True)
class DeclinedReceipt:
    """The receipt of a decline: nothing was written, so it is never read back from the store.
    `summaries` maps an op_id to its short form, such as `link 9a1e0c33 -[lives in]-> 31cdee01`."""

    batch_size: int
    refusals: tuple[Refusal, ...]
    summaries: dict[str, str] = field(default_factory=dict)
    request_id: str | None = None
    state: str = "declined"

    def render(self) -> str:
        from grove.writer.receipts import short_id

        head = f"declined (batch of {self.batch_size}, nothing written)"
        if self.request_id:
            head += f", request {short_id(self.request_id)}"
        lines = [head]
        for refusal in self.refusals:
            name = self.summaries.get(refusal.op_id or "") or _fallback_summary(refusal)
            lines.append(f"  ! {name}: {refusal.code} ({refusal.rule})")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "batch_size": self.batch_size,
            "request_id": self.request_id,
            "refusals": [refusal.to_json() for refusal in self.refusals],
        }

    def __str__(self) -> str:
        return self.render()


def _fallback_summary(refusal: Refusal) -> str:
    if refusal.op_id is None:
        return "batch"
    return f"{refusal.op or 'operation'} {refusal.op_id}"


def only_version_conflict(refusals: list[Refusal]) -> bool:
    """A stale version and what depends on it: the one decline that a fresh read can repair."""
    return any(refusal.code == "version_conflict" for refusal in refusals) and all(
        refusal.code == "version_conflict" or refusal.rule == "depends_on_declined"
        for refusal in refusals
    )


class BatchDeclined(Exception):
    """The batch was refused and nothing was written. `refusals` names every refused operation.
    `retryable` is true only for a stale version: the next pass re-reads and proposes a new one."""

    def __init__(
        self,
        refusals: list[Refusal],
        *,
        batch_size: int,
        summaries: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.refusals = list(refusals)
        self.receipt = DeclinedReceipt(
            batch_size=batch_size,
            refusals=tuple(self.refusals),
            summaries=dict(summaries or {}),
            request_id=request_id,
        )
        self.retryable = only_version_conflict(self.refusals)
        super().__init__(self.receipt.render())

    code = "declined"


class WriteFailed(Exception):
    """The commit did not happen. Nothing was written, and a retry may succeed. A commit whose
    outcome is unknown raises WriteOutcomeUnknown instead."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "unavailable",
        retryable: bool = True,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


class WriteOutcomeUnknown(Exception):
    """The connection dropped while the commit was in flight: the batch may or may not have
    committed. A replay with the same `request_id` and the same batch is safe, because the request
    row says whether it committed. Any other retry is blind. The change set can also be read back
    by `change_set_id` (or `commit_id`): if it holds entries, the batch is saved."""

    code = "outcome_unknown"
    retryable = False

    def __init__(
        self, message: str, *, commit_id: str, change_set_id: str, request_id: str | None
    ) -> None:
        super().__init__(message)
        self.commit_id = commit_id
        self.change_set_id = change_set_id
        self.request_id = request_id


class ReceiptUnavailable(Exception):
    """The batch committed, but its receipt could not be read back. The batch is saved. A replay
    with the same `request_id` and the same batch writes nothing and returns the receipt."""

    code = "receipt_unavailable"
    retryable = False

    def __init__(
        self, message: str, *, commit_id: str, change_set_id: str, request_id: str | None
    ) -> None:
        super().__init__(message)
        self.commit_id = commit_id
        self.change_set_id = change_set_id
        self.request_id = request_id


class InvalidRecord(ValueError):
    """A connector, source or episode the writer refuses to record. Nothing was written."""

    def __init__(self, code: str, rule: str, message: str) -> None:
        super().__init__(f"{code} ({rule}): {message}")
        self.code = code
        self.rule = rule


class LedgerUnavailable(RuntimeError):
    """No private and durable ledger is available; the caller must not erase anything."""
