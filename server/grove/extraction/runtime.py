"""No production Learner authority or dispatch registration exists in this unit."""

from .contracts import ExtractionRefused


def require_runtime() -> None:
    raise ExtractionRefused("learner_authority_unavailable")
