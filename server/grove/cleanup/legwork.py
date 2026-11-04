"""Structural legwork and no-source schedules, without search or truth authority."""

from dataclasses import dataclass
from datetime import datetime

from grove.cleanup.access import CleanupRefused
from grove.cleanup.question_lifecycle import Record, SourceBindingV1, TargetV1, instant, unique
from grove.store.enums import ENUMS


@dataclass(frozen=True, slots=True)
class LegworkAttemptV1(Record):
    attempt_id: str
    work_id: str
    run_id: str
    target: TargetV1
    result: str
    scope_ids: tuple[str, ...]
    capture_generation: int
    authority_id: str
    reason_value_id: str
    source_refs: tuple[SourceBindingV1, ...] = ()

    def _check(self):
        if self.result not in ENUMS["legwork_result"]:
            raise CleanupRefused("question_legwork_result_invalid")
        unique(self.scope_ids, limit=100)
        unique(self.source_refs, limit=50)
        unique(tuple((s.source_id, s.message_key) for s in self.source_refs), limit=50)
        if self.result == "evidence_found" and not self.source_refs:
            raise CleanupRefused("question_legwork_source_required")
        if self.result == "no_match" and not self.scope_ids:
            raise CleanupRefused("question_legwork_scope_required")


@dataclass(frozen=True, slots=True)
class NoSourceDeferralV1(Record):
    attempt_id: str
    work_id: str
    run_id: str
    target: TargetV1
    legwork_ids: tuple[str, ...]
    reason_value_id: str
    next_check: datetime
    recheck_how: str | None = None

    def _check(self):
        unique(self.legwork_ids, limit=50, minimum=1)
        if self.recheck_how not in (None, "ask_the_person", "check_sources"):
            raise CleanupRefused("question_recheck_how_invalid")


def validate_deferral(
    command: NoSourceDeferralV1,
    attempts: tuple[LegworkAttemptV1, ...],
    *,
    attempted_at: datetime,
    latest_check: datetime,
) -> None:
    """Check supplied legwork and an owner-computed D4 horizon, without granting authority.

    The writer still must read committed attempts under the owner lease and apply
    its actual claim/horizon policy. Unavailable or partial searches prove nothing.
    """
    if type(command) is not NoSourceDeferralV1 or type(attempts) is not tuple:
        raise CleanupRefused("question_deferral_invalid")
    command._validate()
    if not 1 <= len(attempts) <= 50 or any(type(a) is not LegworkAttemptV1 for a in attempts):
        raise CleanupRefused("question_legwork_invalid")
    for attempt in attempts:
        attempt._validate()
    unique(tuple(a.attempt_id for a in attempts), limit=50, minimum=1)
    if {a.attempt_id for a in attempts} != set(command.legwork_ids):
        raise CleanupRefused("question_legwork_coverage_invalid")
    for attempt in attempts:
        if (attempt.work_id, attempt.run_id, attempt.target) != (
            command.work_id,
            command.run_id,
            command.target,
        ):
            raise CleanupRefused("question_legwork_binding_invalid")
        if attempt.result not in ("no_match", "no_eligible_source", "person_only"):
            raise CleanupRefused("question_legwork_not_complete")
    if not instant(attempted_at) < command.next_check <= instant(latest_check):
        raise CleanupRefused("question_deferral_schedule_invalid")
