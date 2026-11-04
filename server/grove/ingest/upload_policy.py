"""Mandatory screening for an ordinary account's authenticated source upload.

The HTTP route proves identity and rejects designated demo worlds before this factory.
Personal rename/deny rules and identifier exemptions need a separate, World-bound policy
interface. They must never come from a different account's global film configuration.
"""

from grove.screening import Screener, ScreeningConfig
from grove.store.scope import check_world_id

ORDINARY_UPLOAD_POLICY = "ordinary_upload_v1"


def screener_for_upload(world_id: str) -> Screener:
    """Create a fresh mandatory policy after the route resolves an authenticated World.

    No file, environment value, request field or imported text selects this policy.
    Empty personal selections retain all mandatory credential, card, code, link and
    contact detectors. No identifier is exempt, including the account owner's.
    """
    check_world_id(world_id)
    return Screener(ScreeningConfig())
