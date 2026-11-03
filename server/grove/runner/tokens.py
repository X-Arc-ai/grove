"""The two credentials of the runner.

The runner token is the runner service's one credential (`/etc/grove/runner.env`). The API holds
only its sha256 (GROVE_RUNNER_TOKEN_SHA256), so a copy of the API's settings cannot act as the
runner. With it the runner lists due work and starts and ends passes; it reads no world data.

A pass token is what the `brain` CLI sends. The API mints one per pass: it binds one world, one
run and one agent, carries the scopes grove.read and grove.propose, and expires with the pass's
time cap. The API also refuses it once its run record has ended. Its form is
`p1.<payload>.<signature>`, signed with a key derived from GROVE_GATEWAY_SECRET under a label of
its own, so a gateway run token (`g1.`) never passes as a pass token, nor the other way round.
"""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from grove import clock
from grove.models.config import valid_run_id
from grove.store.errors import ScopeError
from grove.store.scope import check_world_id

PREFIX = "p1"
PASS_SCOPES = frozenset({"grove.read", "grove.propose"})
# The agent of a pass, its writer caller kind, and the gateway job its model calls bill to.
AGENTS: dict[str, tuple[str, str]] = {
    "learner": ("update", "update_pass"),
    "curator": ("cleanup", "cleanup_pass"),
    "connector": ("consolidation", "consolidation_pass"),
}
MAX_TTL_SECONDS = 6 * 3600
_LABEL = b"grove.pass-token.v1"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
MIN_RUNNER_TOKEN_LENGTH = 32


class TokenRefused(Exception):
    """The token is not a valid credential. The message never holds the token."""


@dataclass(frozen=True)
class PassClaims:
    world_id: str
    run_id: str
    agent: str
    expires: int

    @property
    def scopes(self) -> frozenset[str]:
        return PASS_SCOPES


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(gateway_secret: bytes) -> bytes:
    if not isinstance(gateway_secret, bytes) or len(gateway_secret) < 32:
        raise TokenRefused("the signing secret is missing or shorter than 32 bytes")
    return hmac.new(gateway_secret, _LABEL, hashlib.sha256).digest()


def _sign(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, f"{PREFIX}.{payload}".encode("ascii"), hashlib.sha256).digest())


def _epoch() -> int:
    return int(clock.now().timestamp())


def mint_pass_token(
    gateway_secret: bytes, *, world_id: str, run_id: str, agent: str, ttl_seconds: int
) -> str:
    check_world_id(world_id)
    if not valid_run_id(run_id):
        raise TokenRefused("run_id must be a short ASCII id")
    if agent not in AGENTS:
        raise TokenRefused("unknown agent")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TokenRefused("ttl_seconds must be whole seconds")
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise TokenRefused(f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}")
    claims = {"w": world_id, "r": run_id, "a": agent, "exp": _epoch() + ttl_seconds}
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{PREFIX}.{payload}.{_sign(_key(gateway_secret), payload)}"


def verify_pass_token(gateway_secret: bytes, token: object) -> PassClaims:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or parts[0] != PREFIX:
        raise TokenRefused("not a pass token")
    _, payload, signature = parts
    if not (payload.isascii() and signature.isascii()):
        raise TokenRefused("not a pass token")
    if not hmac.compare_digest(_sign(_key(gateway_secret), payload), signature):
        raise TokenRefused("the pass token's signature does not match")
    try:
        claims = json.loads(_unb64(payload))
        world_id = check_world_id(claims["w"])
        run_id, agent, expires = claims["r"], claims["a"], claims["exp"]
    except (ValueError, KeyError, TypeError, ScopeError) as exc:
        raise TokenRefused("the pass token's claims are malformed") from exc
    if isinstance(expires, bool) or not isinstance(expires, int) or expires <= _epoch():
        raise TokenRefused("the pass token has expired")
    if agent not in AGENTS or not valid_run_id(run_id):
        raise TokenRefused("the pass token names no known agent or run")
    return PassClaims(world_id=world_id, run_id=run_id, agent=agent, expires=expires)


def runner_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_runner_token_hash(value: str | None) -> str | None:
    """GROVE_RUNNER_TOKEN_SHA256: 64 lowercase hex digits, or unset."""
    if value is None or value == "":
        return None
    if _SHA256_HEX.fullmatch(value) is None:
        raise ValueError("GROVE_RUNNER_TOKEN_SHA256 must be 64 lowercase hex digits")
    return value


def runner_token_matches(presented: object, expected_sha256: str | None) -> bool:
    """A constant-time compare of the presented token's hash. No expected hash matches nothing."""
    if expected_sha256 is None or not isinstance(presented, str):
        return False
    if len(presented) < MIN_RUNNER_TOKEN_LENGTH or not presented.isascii():
        return False
    return hmac.compare_digest(runner_token_hash(presented), expected_sha256)
