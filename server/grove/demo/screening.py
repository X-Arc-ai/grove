"""Fresh screening policy for code-admitted invented bytes only."""

import hashlib
from importlib import resources

from grove.config import ConfigError
from grove.screening import Denylist, RenameMap, Screener, ScreeningConfig


def new_demo_screener() -> Screener:
    # Never reuse the nested mutable policy objects across operations. Exact input and
    # authenticated world checks are required at each caller before selecting this policy.
    return Screener(
        ScreeningConfig(
            rename=RenameMap((("Demo Restricted Name", "Demo Participant"),)),
            denylist=Denylist(("DEMO_REJECT_THIS_SOURCE",)),
            own_identifiers=frozenset(),
            sensitive_classes=frozenset(),
        )
    )


def lumen_fixture() -> bytes:
    raw = resources.files("grove.demo").joinpath("lumen-upload.json").read_bytes()
    if len(raw) != 361 or hashlib.sha256(raw).hexdigest() != (
        "c19b1926b6710c674e701840cbb4151b66abe91aad3276df1937a041623acc20"
    ):
        raise ConfigError("the invented upload fixture is unavailable")
    return raw
