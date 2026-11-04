"""Authenticated encryption for recoverable provider credentials, outside source content."""

import base64
import json
import os
import secrets
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretUnavailable(Exception):
    """The key or sealed credential is unavailable. Never retain a value in the error."""


class Vault:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise SecretUnavailable()
        self._cipher = AESGCM(key)

    def seal(self, body: dict, *, world: str, reference: str) -> str:
        nonce = secrets.token_bytes(12)
        aad = json.dumps(["grove.connectors.v1", world, reference]).encode()
        raw = json.dumps(body, separators=(",", ":")).encode()
        if len(raw) > 65536:
            raise SecretUnavailable()
        return base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, raw, aad)).decode()

    def open(self, value: str, *, world: str, reference: str) -> dict:
        try:
            if not isinstance(value, str) or len(value) > 90000:
                raise SecretUnavailable()
            raw = base64.b64decode(value, altchars=b"-_", validate=True)
            aad = json.dumps(["grove.connectors.v1", world, reference]).encode()
            body = json.loads(self._cipher.decrypt(raw[:12], raw[12:], aad))
            if not isinstance(body, dict):
                raise SecretUnavailable()
            return body
        except (ValueError, TypeError, InvalidTag):
            raise SecretUnavailable() from None


def load_vault() -> Vault:
    path = os.environ.get("GROVE_CONNECTOR_KEY_FILE")
    if not path:
        raise SecretUnavailable()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 128:
                raise SecretUnavailable()
            raw = source.read(128)
        return Vault(base64.b64decode(raw.strip(), altchars=b"-_", validate=True))
    except (OSError, ValueError):
        raise SecretUnavailable() from None
