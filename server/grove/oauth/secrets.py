"""New secrets and the one hash the store keeps of them (S45).

A code, a token or a client secret leaves the server once, in the answer that issues it. The store
keeps only its sha256 hex, so a dump or a log of a row cannot be replayed.
"""

import hashlib
import secrets

ACCESS_TOKEN_PREFIX = "gva_"
REFRESH_TOKEN_PREFIX = "gvr_"
CODE_PREFIX = "gvc_"
CLIENT_SECRET_PREFIX = "gvs_"

# 32 random bytes: 256 bits, 43 characters of base64url after the prefix.
_RANDOM_BYTES = 32


def token_hash(value: str) -> str:
    """The lowercase sha256 hex of the UTF-8 text: what a row stores in place of the secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret(prefix: str) -> str:
    """A fresh secret that says what it is by its prefix: gva_, gvr_, gvc_ or gvs_."""
    return prefix + secrets.token_urlsafe(_RANDOM_BYTES)
