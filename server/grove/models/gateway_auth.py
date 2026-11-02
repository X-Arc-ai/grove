"""Run tokens for the internal gateway: each binds one world, one run and one job, and expires.

The runner gives OpenCode a run token as its provider key. The gateway checks the signature with
GROVE_GATEWAY_SECRET, so the runner never holds a model key (section 4). The form is
`g1.<payload>.<signature>`, both parts base64url without padding; the payload is JSON.
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from grove import clock
from grove.models.config import Catalog
from grove.models.errors import ModelRefused, Unauthenticated
from grove.store.errors import ScopeError
from grove.store.scope import check_world_id

PREFIX = "g1"
MAX_TTL_SECONDS = 6 * 3600


@dataclass(frozen=True)
class RunClaims:
    world_id: str
    run_id: str
    job: str
    expires: int
    token_cap: int | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, f"{PREFIX}.{payload}".encode("ascii"), hashlib.sha256).digest())


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _epoch() -> int:
    return int(clock.now().timestamp())


def mint(
    secret: bytes,
    catalog: Catalog,
    *,
    world_id: str,
    run_id: str,
    job: str,
    ttl_seconds: int = 3600,
    token_cap: int | None = None,
) -> str:
    check_world_id(world_id)
    catalog.job(job)
    if not isinstance(run_id, str) or not run_id or len(run_id) > 80 or not run_id.isascii():
        raise ModelRefused("run_id must be a short ASCII id")
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ModelRefused(f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}")
    if token_cap is not None and not _positive_int(token_cap):
        raise ModelRefused("token_cap must be a positive integer")
    claims = {"w": world_id, "r": run_id, "j": job, "exp": _epoch() + ttl_seconds}
    if token_cap is not None:
        claims["cap"] = token_cap
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{PREFIX}.{payload}.{_sign(secret, payload)}"


def verify(secret: bytes, catalog: Catalog, token: str) -> RunClaims:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or parts[0] != PREFIX:
        raise Unauthenticated("not a gateway run token")
    _, payload, signature = parts
    if not hmac.compare_digest(_sign(secret, payload), signature):
        raise Unauthenticated("the run token's signature does not match")
    try:
        claims = json.loads(_unb64(payload))
        world_id = check_world_id(claims["w"])
        run_id, job, expires = claims["r"], claims["j"], claims["exp"]
        cap = claims.get("cap")
    except (ValueError, KeyError, TypeError, ScopeError) as exc:
        raise Unauthenticated("the run token's claims are malformed") from exc
    if not isinstance(expires, int) or expires <= _epoch():
        raise Unauthenticated("the run token has expired")
    if job not in catalog.jobs or not isinstance(run_id, str):
        raise Unauthenticated("the run token names no known job")
    if cap is not None and not _positive_int(cap):
        raise Unauthenticated("the run token's cap is malformed")
    return RunClaims(world_id=world_id, run_id=run_id, job=job, expires=expires, token_cap=cap)
