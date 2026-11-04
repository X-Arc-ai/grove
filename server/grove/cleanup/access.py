"""An explicit owner seam, not an implementation of D14 authorization.

No production provider is installed. Internal callers must hold the owner's real
authorization/forget lease across reads, network calls, model calls and writes.
Tests supply a clearly named synthetic provider. A boolean never grants access.
"""

from contextlib import AbstractContextManager
from typing import Protocol

from grove.store.scope import check_world_id


class CleanupRefused(ValueError):
    """A bounded, value-free refusal."""


class Access(Protocol):
    def hold(
        self, world_id: str, run_id: str, action: str, references: tuple[str, ...]
    ) -> AbstractContextManager[None]:
        """Validate the principal, run, policy and references; hold through exit."""


def hold(access: Access | None, world_id: str, run_id: str, action: str, *references: str):
    check_world_id(world_id)
    if access is None or not callable(getattr(access, "hold", None)):
        raise CleanupRefused("curator_authority_unavailable")
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        raise CleanupRefused("curator_run_invalid")
    return access.hold(world_id, run_id, action, tuple(references))
