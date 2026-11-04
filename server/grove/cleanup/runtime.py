"""The production activation barrier for D11.

No configuration flag or arbitrary app.state callback activates these source
components. A later integration must supply complete D7/D14 capture, filtered
inputs and a final release lease, then replace this barrier with that provider.
"""

from grove.cleanup.access import CleanupRefused


def require_runtime() -> None:
    raise CleanupRefused("curator_authority_unavailable")
