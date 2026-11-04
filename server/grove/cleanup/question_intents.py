"""Prospective D10 handoff data. No intent or transfer completes Learner work."""

from dataclasses import dataclass

from grove.cleanup.access import CleanupRefused
from grove.cleanup.question_lifecycle import (
    QuestionApplicationV1,
    Record,
    SourceBindingV1,
    TargetV1,
    structural,
    targets,
    unique,
)


@dataclass(frozen=True, slots=True)
class IntentPartV1(Record):
    disposition_id: str
    obligation_id: str
    source: SourceBindingV1
    waiting_operation_id: str
    payload_value_id: str
    payload_version: int
    disposition: str

    def _check(self):
        if self.disposition not in ("held_fact", "unresolved"):
            raise CleanupRefused("question_intent_disposition_invalid")


@dataclass(frozen=True, slots=True)
class CuratorQuestionIntentV1(Record):
    intent_id: str
    world_id: str
    run_id: str
    request_id: str
    execution_id: str
    execution_version: int
    group_id: str
    parts: tuple[IntentPartV1, ...]
    targets: tuple[TargetV1, ...]
    legwork_ids: tuple[str, ...]

    def _check(self):
        unique(self.parts, limit=100, minimum=1)
        unique(tuple(p.disposition_id for p in self.parts), limit=100, minimum=1)
        unique(tuple(p.obligation_id for p in self.parts), limit=100, minimum=1)
        unique(
            tuple((p.source.source_id, p.source.message_key) for p in self.parts),
            limit=100,
            minimum=1,
        )
        # A page cannot mix two revisions or locators for the same retained source.
        sources, episodes = {}, {}
        for part in self.parts:
            source = part.source
            source_state = (source.source_version, source.episode_id, source.episode_version)
            if sources.setdefault(source.source_id, source_state) != source_state:
                raise CleanupRefused("question_intent_source_conflict")
            if (
                episodes.setdefault(source.episode_id, source.episode_version)
                != source.episode_version
            ):
                raise CleanupRefused("question_intent_episode_conflict")
        targets(self.targets)
        unique(self.legwork_ids, limit=50)


@dataclass(frozen=True, slots=True)
class ResolutionTransferV1(Record):
    transfer_id: str
    application_id: str
    question_id: str
    binding_revision: int
    source: SourceBindingV1
    obligation_ids: tuple[str, ...]
    candidate_targets: tuple[TargetV1, ...]

    def _check(self):
        unique(self.obligation_ids, limit=100, minimum=1)
        targets(self.candidate_targets)


def validate_accounting(
    intent: CuratorQuestionIntentV1,
    accepted_ids: tuple[str, ...],
    refused_ids: tuple[str, ...],
) -> None:
    """Account for every disposition exactly once in a consumer's bounded response."""
    if type(intent) is not CuratorQuestionIntentV1:
        raise CleanupRefused("question_intent_invalid")
    intent._validate()
    for group in (accepted_ids, refused_ids):
        if type(group) is not tuple or len(group) > 100:
            raise CleanupRefused("question_intent_accounting_invalid")
        for key in group:
            structural(key)
    unique(accepted_ids + refused_ids, limit=100, minimum=1)
    if set(accepted_ids + refused_ids) != {p.disposition_id for p in intent.parts}:
        raise CleanupRefused("question_intent_accounting_invalid")


def validate_transfer(
    intent: CuratorQuestionIntentV1,
    transfer: ResolutionTransferV1,
    application: QuestionApplicationV1,
) -> None:
    """Validate references only; D10 must revalidate live provenance before effects."""
    for value, cls in (
        (intent, CuratorQuestionIntentV1),
        (transfer, ResolutionTransferV1),
        (application, QuestionApplicationV1),
    ):
        if type(value) is not cls:
            raise CleanupRefused("question_transfer_invalid")
        value._validate()
    if (
        transfer.application_id,
        transfer.question_id,
        transfer.binding_revision,
        transfer.source,
    ) != (
        application.application_id,
        application.question_id,
        application.binding_revision,
        application.source,
    ):
        raise CleanupRefused("question_transfer_binding_invalid")
    obligations = set(transfer.obligation_ids)
    if not obligations <= {p.obligation_id for p in intent.parts}:
        raise CleanupRefused("question_transfer_obligation_invalid")
    if not obligations <= set(application.transferred_ids):
        raise CleanupRefused("question_transfer_not_applied")
    if not set(transfer.candidate_targets) <= set(intent.targets):
        raise CleanupRefused("question_transfer_candidate_invalid")
