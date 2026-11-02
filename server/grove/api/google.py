"""Google sign-in (OIDC, scopes exactly `openid email profile`), as steps with no web framework.

The calendar is not asked here: `ingest-a` asks it later, in its own consent. No refresh token is
asked either: sign-in needs one answer, once. The client file holds no redirect_uris key, and only
the callback at the configured public base URL is registered at Google.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import httpx

from grove.api.mail import valid_address

SCOPE = "openid email profile"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CALLBACK_PATH = "/v1/auth/google/callback"
DEFAULT_NEXT = "/"
MAX_NEXT_LENGTH = 512
_TIMEOUT_SECONDS = 10.0
_JWT_PARTS = 3
_CLIENT_FIELDS = ("client_id", "client_secret", "auth_uri", "token_uri")


class GoogleRefused(Exception):
    """Any failure of the callback. It carries no reason: the page shows one neutral error."""


@dataclass(frozen=True)
class GoogleClient:
    client_id: str
    client_secret: str = field(repr=False)
    auth_uri: str
    token_uri: str


@dataclass(frozen=True)
class HeldState:
    """What the state cookie holds between the redirect and the callback."""

    state: str
    nonce: str
    verifier: str
    next: str


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    name: str | None


def load_client(path: str | None) -> GoogleClient | None:
    """The `web` key of Google's client file, or None for no file or a bad file."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as opened:
            web = json.load(opened)["web"]
        values = [web[name] for name in _CLIENT_FIELDS]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(value, str) and value for value in values):
        return None
    return GoogleClient(*values)


def safe_next(value: object) -> str:
    """A same-origin path, or `/`. `//host` and `/\\host` leave the origin in a browser."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_NEXT_LENGTH:
        return DEFAULT_NEXT
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_NEXT
    if not value.isascii() or not value.isprintable():
        return DEFAULT_NEXT
    return value


def new_state(next_path: object) -> HeldState:
    return HeldState(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(64),
        next=safe_next(next_path),
    )


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decoded(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pack_state(held: HeldState) -> str:
    body = {"state": held.state, "nonce": held.nonce, "verifier": held.verifier, "next": held.next}
    return _encoded(json.dumps(body).encode("utf-8"))


def unpack_state(text: str | None) -> HeldState | None:
    """The browser holds this cookie, so `next` is checked again here and nothing else is trusted:
    the state must still match the query, and the nonce the id token."""
    if not text:
        return None
    try:
        body = json.loads(_decoded(text))
    except (ValueError, binascii.Error, RecursionError):
        return None
    if not isinstance(body, dict):
        return None
    values = [body.get(name) for name in ("state", "nonce", "verifier")]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return HeldState(*values, next=safe_next(body.get("next")))


def authorization_url(client: GoogleClient, held: HeldState, *, redirect_uri: str) -> str:
    challenge = _encoded(hashlib.sha256(held.verifier.encode("ascii")).digest())
    query = urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": held.state,
            "nonce": held.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return f"{client.auth_uri}?{query}"


def same_text(first: str, second: str) -> bool:
    return hmac.compare_digest(
        first.encode("utf-8", "replace"),
        second.encode("utf-8", "replace"),
    )


def _claims_of(id_token: object) -> dict:
    if not isinstance(id_token, str):
        raise GoogleRefused
    parts = id_token.split(".")
    if len(parts) != _JWT_PARTS:
        raise GoogleRefused
    try:
        claims = json.loads(_decoded(parts[1]))
    except (ValueError, binascii.Error, RecursionError):
        raise GoogleRefused from None
    if not isinstance(claims, dict):
        raise GoogleRefused
    return claims


def check_id_token(
    id_token: object, *, client_id: str, nonce: str, now: datetime
) -> GoogleIdentity:
    """The claims OIDC asks of a code flow, with no signature check and no JWT library.

    The token comes straight from Google's token endpoint over TLS, so OIDC Core 3.1.3.7 step 6
    allows the TLS server check in place of the signature.
    """
    claims = _claims_of(id_token)
    expires = claims.get("exp")
    fresh = (
        isinstance(expires, int | float)
        and not isinstance(expires, bool)
        and expires > now.timestamp()
    )
    token_nonce = claims.get("nonce")
    subject = claims.get("sub")
    email = claims.get("email")
    if (
        claims.get("iss") not in ISSUERS
        or claims.get("aud") != client_id
        or claims.get("azp", client_id) != client_id
        or not fresh
        or not isinstance(token_nonce, str)
        or not same_text(token_nonce, nonce)
        or claims.get("email_verified") is not True
        or not valid_address(email)
        or not isinstance(subject, str)
        or not subject
    ):
        raise GoogleRefused
    name = claims.get("name")
    return GoogleIdentity(email=email, name=name if isinstance(name, str) else None)


def exchange_code(
    http: httpx.Client, client: GoogleClient, *, code: str, verifier: str, redirect_uri: str
) -> object:
    """The id token of a code, as Google sent it."""
    try:
        response = http.post(
            client.token_uri,
            data={
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        # No chain: the request the error carries holds the client secret.
        raise GoogleRefused from None
    if response.status_code != 200:
        raise GoogleRefused
    try:
        body = response.json()
    except ValueError:
        raise GoogleRefused from None
    if not isinstance(body, dict):
        raise GoogleRefused
    return body.get("id_token")
