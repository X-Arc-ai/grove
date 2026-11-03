"""What a host may do in a person's world: the three scopes of A.3 and the words the consent page
gives them. docs/hosts.md.

A request's scope is its intersection with grove.api.config.SCOPES. A scope Grove does not know is
dropped with no error, because hosts send their own (openid, profile, offline_access). No known
scope gives the default, grove.read and grove.write, so grove.sensitive_write is granted only when a
request names it.
"""

from collections.abc import Iterable

from grove.api.config import SCOPES

READ, WRITE, SENSITIVE_WRITE = SCOPES
DEFAULT_SCOPES = (READ, WRITE)

# What the consent page says each scope lets the host do.
SCOPE_WORDS = {
    READ: "Read what Grove knows: recall, what changed, what it breaks, questions",
    WRITE: "Add to Grove: remember, answer questions, correct",
    SENSITIVE_WRITE: "Forget things in Grove (each forget still asks you)",
}


def known_scopes(raw: object) -> tuple[str, ...]:
    """The known scopes of a space-separated scope value, in the order of SCOPES; () for none."""
    if not isinstance(raw, str):
        return ()
    asked = set(raw.split())
    return tuple(scope for scope in SCOPES if scope in asked)


def granted_scopes(raw: object) -> tuple[str, ...]:
    """The scopes an authorization request gets: the known ones it names, else the default."""
    return known_scopes(raw) or DEFAULT_SCOPES


def scope_text(scopes: Iterable[str]) -> str:
    """The scope parameter form: the codes joined by one space."""
    return " ".join(scopes)
