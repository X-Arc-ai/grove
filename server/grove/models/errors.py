"""Typed errors of the model-call layer, in the B.3 shape.

A message never holds a key or a prompt."""

from typing import Any


class ModelError(RuntimeError):
    code = "unavailable"
    status = 503
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_body(self) -> dict[str, Any]:
        """B.3's error document. `type` is there too, because OpenAI clients read it."""
        return {
            "error": {
                "code": self.code,
                "type": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            }
        }


class BudgetReached(ModelError):
    """The world's daily ceiling is reached and this job waits until the day resets (D15)."""

    code = "budget_reached"
    status = 429
    retryable = True


class RunCapReached(ModelError):
    """A run used its token cap (Appendix C, run caps)."""

    code = "run_cap_reached"
    status = 429


class ModelRefused(ModelError):
    """Unknown model, a contributor id, an unknown job, or an effort the model does not take."""

    code = "invalid_arguments"
    status = 400


class EgressRefused(ModelError):
    """A request to a host outside the runtime's egress allowlist. No socket was opened."""

    code = "egress_refused"
    status = 403


class RateLimited(ModelError):
    code = "rate_limited"
    status = 429
    retryable = True


class ProviderError(ModelError):
    """The provider answered with an error, or did not answer."""

    code = "unavailable"
    status = 503
    retryable = True


class InvalidOutput(ModelError):
    """The model's answer is empty, is not JSON, or does not fit the schema. The call is metered."""

    code = "invalid_output"
    status = 502


class AudioTooLong(ModelError):
    code = "invalid_arguments"
    status = 400


class NotConfigured(ModelError):
    """A setting the call needs is missing. The message names the variable, never a value."""

    code = "unavailable"
    status = 503


class Unauthenticated(ModelError):
    code = "unauthenticated"
    status = 401
