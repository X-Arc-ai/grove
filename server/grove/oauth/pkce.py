"""PKCE (RFC 7636), S256 only: `plain` is refused, and so is a request with no method.

A challenge and a verifier are 43 to 128 unreserved characters. The code row keeps the challenge;
/token checks that the S256 of the verifier equals it.
"""

import base64
import hashlib
import hmac
import re

S256 = "S256"
METHODS = (S256,)

_UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


def valid_pkce_value(value: object) -> bool:
    """A challenge or a verifier: 43 to 128 characters of [A-Za-z0-9-._~]."""
    return isinstance(value, str) and _UNRESERVED.fullmatch(value) is not None


def s256(verifier: str) -> str:
    """BASE64URL(SHA256(ASCII(verifier))) with no padding: the challenge of a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verifier_matches(verifier: str, challenge: str) -> bool:
    """True when the verifier is well formed and its S256 equals the stored challenge."""
    if not valid_pkce_value(verifier) or not isinstance(challenge, str):
        return False
    return hmac.compare_digest(s256(verifier).encode("ascii"), challenge.encode("utf-8"))
