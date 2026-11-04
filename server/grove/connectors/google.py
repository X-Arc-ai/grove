"""Separate Google Calendar consent and refresh. No provider values enter errors."""

import base64
import hashlib
import json
from urllib.parse import urlencode

import httpx

from grove import clock
from grove.api.google import ISSUERS, GoogleClient, HeldState, same_text
from grove.connectors.calendar import CALENDAR_SCOPE, CalendarRefused, identifier

SCOPE = "openid " + CALENDAR_SCOPE
CALLBACK = "/v1/connectors/google/callback"


def authorization_url(client: GoogleClient, held: HeldState, callback: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(held.verifier.encode()).digest()).rstrip(
        b"="
    )
    return (
        client.auth_uri
        + "?"
        + urlencode(
            {
                "client_id": client.client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "scope": SCOPE,
                "state": held.state,
                "nonce": held.nonce,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
            }
        )
    )


def _post(http: httpx.Client, client: GoogleClient, fields: dict) -> dict:
    try:
        with http.stream(
            "POST",
            client.token_uri,
            data={
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                **fields,
            },
            timeout=20,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise CalendarRefused("denied")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise CalendarRefused("invalid_response")
            body = json.loads(raw)
    except (httpx.HTTPError, ValueError):
        raise CalendarRefused() from None
    if not isinstance(body, dict):
        raise CalendarRefused("invalid_response")
    return body


def _tokens(body: dict, *, previous: dict | None = None) -> dict:
    previous = previous or {}
    access = body.get("access_token")
    refresh = body.get("refresh_token", previous.get("refresh_token"))
    scope = body.get("scope", previous.get("scope", ""))
    seconds = body.get("expires_in")
    if (
        not all(isinstance(v, str) and 0 < len(v) <= 8192 for v in (access, refresh))
        or not isinstance(scope, str)
        or CALENDAR_SCOPE not in scope.split()
        or type(seconds) is not int
        or not 0 < seconds <= 86400
        or str(body.get("token_type", "")).lower() != "bearer"
    ):
        raise CalendarRefused("denied")
    return {
        **previous,
        "access_token": access,
        "refresh_token": refresh,
        "scope": scope,
        "expires_at": clock.now().timestamp() + seconds,
    }


def exchange(
    http: httpx.Client, client: GoogleClient, held: HeldState, code: str, callback: str
) -> dict:
    body = _post(
        http,
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": held.verifier,
            "redirect_uri": callback,
        },
    )
    tokens = _tokens(body)
    try:
        token = body["id_token"]
        middle = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4)))
        if (
            claims.get("iss") not in ISSUERS
            or claims.get("aud") != client.client_id
            or claims.get("azp", client.client_id) != client.client_id
            or type(claims.get("exp")) not in (int, float)
            or claims["exp"] <= clock.now().timestamp()
            or not isinstance(claims.get("nonce"), str)
            or not same_text(claims["nonce"], held.nonce)
        ):
            raise CalendarRefused("denied")
        tokens["subject"] = identifier(claims["sub"])
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        raise CalendarRefused("denied") from None
    return tokens


def refresh(http: httpx.Client, client: GoogleClient, tokens: dict) -> dict:
    if tokens.get("expires_at", 0) > clock.now().timestamp() + 30:
        return tokens
    body = _post(
        http, client, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    )
    return _tokens(body, previous=tokens)
