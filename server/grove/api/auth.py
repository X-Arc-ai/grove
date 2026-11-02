"""The sign-in routes under /v1/auth: an email code, Google, and logout. docs/signin.md.

uvicorn runs without proxy headers, so nothing here reads the scheme or the host of a request: the
callback and the cookie flags come from settings.public_base_url.

Every failure says as little as it can. A wrong, expired, used or absent code reads the same, and a
new address reads the same as a known one, so the routes do not tell who has an account.
"""

import re
import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from math import ceil
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api import google
from grove.api.config import ApiSettings
from grove.api.errors import (
    invalid_arguments,
    rate_limited,
    store_unavailable,
    unauthenticated,
    unavailable,
)
from grove.api.mail import Mailer, MailFailed, load_mailer, valid_address
from grove.api.views import check_json_type, read_json_body
from grove.store.connection import Driver
from grove.store.probe import DriverError, Neo4jError
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.graph import session_payload
from grove.views.session import MAX_TOKEN_LENGTH, SESSION_COOKIE, find_session, token_hash
from grove.writer.accounts import (
    TooManyCodes,
    code_hash,
    create_session,
    default_world_id,
    email_hash,
    ensure_account,
    revoke_session,
    store_email_code,
    try_email_code,
)
from grove.writer.errors import BatchDeclined, InvalidRecord

AUTH_PREFIX = "/v1/auth"

# The starting values; docs/signin.md lists them.
SESSION_LIFETIME = timedelta(days=30)
CODE_DIGITS = 6
CODE_LIFETIME = timedelta(minutes=10)
MAX_TRIES_PER_CODE = 5
MAX_CODES_PER_ADDRESS = 5
ADDRESS_WINDOW = timedelta(minutes=15)
MAX_CODES_PER_HOUR = 300
GLOBAL_WINDOW_SECONDS = 3600.0
GOOGLE_STATE_SECONDS = 600

MAX_BODY_BYTES = 4096
SIGNIN_ERROR_PATH = "/signin?signin_error=google"
STATE_COOKIE_HTTPS = "__Host-grove_google"
STATE_COOKIE_LOOPBACK = "grove_google"

_CODE = re.compile(r"[0-9]{6}")
# What the store and the writer raise when a sign-in cannot finish: the person may try again.
_STORE_FAILURES = (Neo4jError, DriverError, BatchDeclined, InvalidRecord)

router = APIRouter(prefix=AUTH_PREFIX)


class CodeWindow:
    """The cap across the server, on the monotonic clock. In process: one API process runs, a
    restart resets it, and it reads no store, so it holds while the store is under load."""

    def __init__(
        self, limit: int = MAX_CODES_PER_HOUR, seconds: float = GLOBAL_WINDOW_SECONDS
    ) -> None:
        self._limit, self._seconds = limit, seconds
        self._taken: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> int | None:
        """None when a slot was taken, else the seconds until one is free."""
        now = clock.monotonic()
        with self._lock:
            while self._taken and self._taken[0] <= now - self._seconds:
                self._taken.popleft()
            if len(self._taken) >= self._limit:
                return max(1, ceil(self._taken[0] + self._seconds - now))
            self._taken.append(now)
            return None

    def give_back(self) -> None:
        """A start that stored no code spends no slot: one busy address must not stop the others."""
        with self._lock:
            if self._taken:
                self._taken.pop()


@dataclass
class AuthDeps:
    """What create_app hands the routes. None means the real thing, loaded at first use."""

    mailer: Mailer | None = None
    google_http: httpx.Client | None = None
    world_id_for: Callable[[str], str] | None = None
    window: CodeWindow = field(default_factory=CodeWindow)
    google_client: google.GoogleClient | None = None


def _deps(request: Request) -> AuthDeps:
    return request.app.state.auth


def _settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def _driver(request: Request) -> Driver:
    return request.app.state.driver


def _set_session_cookie(response: Response, token: str, settings: ApiSettings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response, settings: ApiSettings) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax"
    )


def _revoke_named_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token and len(token) <= MAX_TOKEN_LENGTH:
        revoke_session(_driver(request), token_hash(token), clock.now())


def _sign_in(
    request: Request, hashed_address: str, *, method: str, display_name: str | None = None
) -> str:
    """The account, a new session, and the end of the session the incoming cookie names. Returns
    the new session's token. The :World row exists before the caller sets the cookie."""
    driver = _driver(request)
    account = ensure_account(
        driver,
        hashed_address,
        method=method,
        display_name=display_name,
        world_id_for=_deps(request).world_id_for or default_world_id,
    )
    token = secrets.token_urlsafe(32)
    create_session(
        driver,
        account.user_id,
        token_hash(token),
        method=method,
        expires_at=clock.now() + SESSION_LIFETIME,
    )
    _revoke_named_session(request)
    return token


def _object_body(body: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(body, dict) or not set(body) <= allowed:
        names = " and ".join(sorted(allowed))
        raise invalid_arguments(f"the body must be an object with {names} only", "body")
    return body


def _address(body: dict[str, Any]) -> str:
    address = body.get("email")
    if not valid_address(address):
        raise invalid_arguments("email must be one valid address", "email")
    return address


# The email code ------------------------------------------------------------------------------


def _start_email(request: Request, address: str) -> dict[str, bool]:
    deps = _deps(request)
    if deps.mailer is None:
        deps.mailer = load_mailer()
    if deps.mailer is None:
        raise unavailable("the mail cannot be sent")
    waited = deps.window.take()
    if waited is not None:
        raise rate_limited(waited)
    hashed = email_hash(address)
    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    try:
        stored = store_email_code(
            _driver(request),
            hashed,
            lambda row_id: code_hash(row_id, hashed, code),
            now=clock.now(),
            ttl=CODE_LIFETIME,
            max_per_window=MAX_CODES_PER_ADDRESS,
            window=ADDRESS_WINDOW,
        )
    except _STORE_FAILURES:
        deps.window.give_back()
        raise store_unavailable() from None
    if isinstance(stored, TooManyCodes):
        deps.window.give_back()
        raise rate_limited(stored.retry_after_seconds)
    try:
        deps.mailer.send_code(address, code)
    except MailFailed:
        raise unavailable("the mail cannot be sent") from None
    return {"sent": True}


@router.post("/email/start")
async def email_start(request: Request) -> dict[str, bool]:
    body = _object_body(await read_json_body(request, MAX_BODY_BYTES), {"email"})
    address = _address(body)
    return await run_in_threadpool(_start_email, request, address)


def _verify_email(request: Request, address: str, code: str) -> JSONResponse:
    driver = _driver(request)
    hashed = email_hash(address)
    try:
        matched = try_email_code(
            driver, hashed, code, now=clock.now(), max_attempts=MAX_TRIES_PER_CODE
        )
        if not matched:
            raise unauthenticated("the code does not match")
        token = _sign_in(request, hashed, method="email")
        session = find_session(PlatformQuery(driver), token, clock.now())
        if session is None:
            raise store_unavailable()
        answer = session_payload(WorldQuery(driver, session.world_id), session)
    except _STORE_FAILURES:
        raise store_unavailable() from None
    response = JSONResponse(answer)
    _set_session_cookie(response, token, _settings(request))
    return response


@router.post("/email/verify")
async def email_verify(request: Request) -> JSONResponse:
    body = _object_body(await read_json_body(request, MAX_BODY_BYTES), {"email", "code"})
    address = _address(body)
    code = body.get("code")
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        raise invalid_arguments(f"code must be exactly {CODE_DIGITS} digits", "code")
    return await run_in_threadpool(_verify_email, request, address, code)


# Google --------------------------------------------------------------------------------------


def _state_cookie_name(settings: ApiSettings) -> str:
    return STATE_COOKIE_HTTPS if settings.secure_cookies else STATE_COOKIE_LOOPBACK


def _google_client(request: Request) -> google.GoogleClient | None:
    """Read at first use: a missing file costs the sign-in, never the start of the API. A failed
    read is not kept, so a file that lands later works with no restart."""
    deps = _deps(request)
    if deps.google_client is None:
        deps.google_client = google.load_client(_settings(request).google_oauth_file)
    return deps.google_client


def _to_signin_error() -> RedirectResponse:
    return RedirectResponse(SIGNIN_ERROR_PATH, status_code=303)


@router.get("/google/start")
def google_start(request: Request) -> RedirectResponse:
    client = _google_client(request)
    if client is None:
        return _to_signin_error()
    settings = _settings(request)
    held = google.new_state(request.query_params.get("next"))
    response = RedirectResponse(
        google.authorization_url(client, held, redirect_uri=settings.google_callback_url),
        status_code=303,
    )
    response.set_cookie(
        _state_cookie_name(settings),
        google.pack_state(held),
        max_age=GOOGLE_STATE_SECONDS,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
    return response


def _google_identity(request: Request, held: google.HeldState, code: str) -> google.GoogleIdentity:
    client = _google_client(request)
    if client is None:
        raise google.GoogleRefused
    redirect_uri = _settings(request).google_callback_url
    http = _deps(request).google_http
    exchange = {"code": code, "verifier": held.verifier, "redirect_uri": redirect_uri}
    if http is not None:
        id_token = google.exchange_code(http, client, **exchange)
    else:
        with httpx.Client() as own:
            id_token = google.exchange_code(own, client, **exchange)
    return google.check_id_token(
        id_token, client_id=client.client_id, nonce=held.nonce, now=clock.now()
    )


def _finish_google(request: Request) -> tuple[str, str] | None:
    """(next path, session token), or None for any failure: the page shows one neutral error."""
    settings = _settings(request)
    params = request.query_params
    held = google.unpack_state(request.cookies.get(_state_cookie_name(settings)))
    code = params.get("code")
    if held is None or "error" in params or not code:
        return None
    if not google.same_text(held.state, params.get("state", "")):
        return None
    try:
        identity = _google_identity(request, held, code)
        token = _sign_in(
            request, email_hash(identity.email), method="google", display_name=identity.name
        )
    except (google.GoogleRefused, *_STORE_FAILURES):
        return None
    return held.next, token


@router.get("/google/callback")
def google_callback(request: Request) -> RedirectResponse:
    settings = _settings(request)
    finished = _finish_google(request)
    response = _to_signin_error()
    if finished is not None:
        next_path, token = finished
        response = RedirectResponse(next_path, status_code=303)
        _set_session_cookie(response, token, settings)
    # The answer leaves the code's URL behind; nothing here may follow the person to the next page.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.delete_cookie(
        _state_cookie_name(settings),
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
    return response


# Logout --------------------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """The person's world stays. With the store down the cookie still goes, and the answer says the
    session could not be ended."""
    check_json_type(request)
    settings = _settings(request)
    response = JSONResponse({"signed_in": False})
    try:
        await run_in_threadpool(_revoke_named_session, request)
    except _STORE_FAILURES:
        failed = store_unavailable()
        response = JSONResponse(failed.body(), status_code=failed.status)
    _clear_session_cookie(response, settings)
    return response
