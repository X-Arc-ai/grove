"""Question inputs and ordering; no answer is inferred from an agent's prose."""

from dataclasses import dataclass

from grove.cleanup.access import CleanupRefused
from grove.store.enums import ENUMS


@dataclass(frozen=True)
class Question:
    subjects: tuple[str, ...]
    ask: str
    kind: str
    # Pairs of retained source id and recorded not-about-claim judgment id.
    legwork: tuple[tuple[str, str], ...]
    reason: str
    options: tuple[str, ...] = ()
    priority: int = 0

    def __post_init__(self):
        if not 1 <= len(self.subjects) <= 20 or len(set(self.subjects)) != len(self.subjects):
            raise CleanupRefused("question_subjects_invalid")
        if any(type(value) is not str or not 0 < len(value) <= 128 for value in self.subjects):
            raise CleanupRefused("question_subject_invalid")
        if self.kind not in ENUMS["question_class"]:
            raise CleanupRefused("question_class_invalid")
        if not 1 <= len(self.legwork) <= 50:
            raise CleanupRefused("question_legwork_required")
        if any(
            len(pair) != 2 or any(type(v) is not str or not 0 < len(v) <= 128 for v in pair)
            for pair in self.legwork
        ):
            raise CleanupRefused("question_legwork_invalid")
        if any(
            type(text) is not str or not text.strip() or len(text) > 2000
            for text in (self.ask, self.reason)
        ):
            raise CleanupRefused("question_text_invalid")
        if type(self.priority) is not int or not 0 <= self.priority <= 2:
            raise CleanupRefused("question_priority_invalid")
        if len(self.options) > 20 or len(set(self.options)) != len(self.options):
            raise CleanupRefused("question_options_invalid")
        if any(type(option) is not str or not 0 < len(option) <= 128 for option in self.options):
            raise CleanupRefused("question_option_invalid")


def ordered(rows: list[dict]) -> list[dict]:
    """Risk first, decisions next, information last; oldest first within a class."""
    return sorted(
        rows,
        key=lambda row: (
            -row.get("priority", 0),
            0 if row.get("class") == "decision" else 1,
            row["raised_at"],
            row["id"],
        ),
    )
