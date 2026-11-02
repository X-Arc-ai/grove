"""Who sends a batch: a caller kind from B.2, the scopes its credential holds, and an actor code."""

from collections.abc import Iterable
from dataclasses import dataclass

# B.2's caller letters.
CALLER_KINDS: dict[str, str] = {
    "update": "U",
    "cleanup": "C",
    "consolidation": "K",
    "ai_host": "H",
    "person": "P",
    "writer": "W",
}
WRITE_SCOPES = frozenset({"grove.write", "grove.propose"})


@dataclass(frozen=True)
class Caller:
    kind: str
    scopes: frozenset[str]
    actor: str

    def __init__(self, kind: str, scopes: Iterable[str], actor: str) -> None:
        if kind not in CALLER_KINDS:
            raise ValueError(f"caller kind must be one of {sorted(CALLER_KINDS)}")
        if isinstance(scopes, str):
            raise ValueError("scopes is a set of scope names, not one string")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("a caller needs an actor code")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scopes", frozenset(scopes))
        object.__setattr__(self, "actor", actor)

    @property
    def letter(self) -> str:
        return CALLER_KINDS[self.kind]

    @property
    def can_write(self) -> bool:
        return bool(self.scopes & WRITE_SCOPES)
