"""The OAuth 2.1 authorization server of /mcp: /register, /authorize and its decision, /token,
/revoke, and the person's connections under /v1/views. docs/hosts.md.

The OAuth endpoints answer OAuth errors (RFC 6749 section 5.2: {"error", "error_description"}),
and every answer carries Cache-Control: no-store.

/authorize and its decision redirect to a client only because a person pressed a button.
Registration is open, so a registered redirect URI proves nothing about its owner, and an automatic
redirect would make Grove an open redirector (RFC 9700 section 4.11.2). So every refusal at these
two routes before the csrf check is a 400 HTML page on Grove, with no Location, and no state goes
anywhere. The only two redirects to a client are Approve and Deny, after a valid csrf. With no
session, a document client_id goes to sign-in with no fetch, and a decision is a 401 page before any
other check. A document's row is written only on Approve, after the csrf check: a consent page, a
Deny and every refused decision leave no row, for any caller, and spend no slot of the window, which
serves POST /register alone.

The consent page is web/dist/consent.html, read from GROVE_WEB_DIST at request time. With no built
page, an answer that needs it is a typed 503.

A parameter given twice is refused (RFC 6749 section 3.1), and a parameter with no value counts as
absent. The store is read and written in a thread: the driver is synchronous.
"""

import base64
import binascii
import hmac
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote_plus

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.auth import CodeWindow
from grove.api.config import SCOPES, WEB_DIST_ENV, ApiSettings
from grove.api.errors import not_found, unavailable
from grove.api.views import PREFIX, Session, check_json_type
from grove.ids import new_id
from grove.oauth import (
    ACCESS_TOKEN_LIFETIME,
    CODE_LIFETIME,
    MAX_GRANTS_PER_WORLD,
    MAX_REGISTRATIONS_PER_WINDOW,
    REFRESH_TOKEN_LIFETIME,
    REGISTRATION_WINDOW_SECONDS,
)
from grove.oauth.consent import (
    APPROVE,
    DENY,
    ConsentView,
    TemplateBroken,
    consent_fragment,
    csrf_token,
    error_fragment,
    render_page,
    same_csrf,
)
from grove.oauth.metadata_document import (
    DocumentBusy,
    Resolver,
    document_client,
    is_document_client_id,
    store_document_client,
    system_resolver,
)
from grove.oauth.pkce import S256, valid_pkce_value, verifier_matches
from grove.oauth.redirects import (
    is_loopback,
    redirect_matches,
    return_place,
    shown_host,
    with_params,
)
from grove.oauth.registration import (
    INVALID_CLIENT_METADATA,
    MAX_REGISTRATION_BYTES,
    ClientMetadata,
    InvalidMetadata,
    parse_registration,
)
from grove.oauth.scopes import granted_scopes, known_scopes, scope_text
from grove.oauth.secrets import (
    ACCESS_TOKEN_PREFIX,
    CLIENT_SECRET_PREFIX,
    CODE_PREFIX,
    REFRESH_TOKEN_PREFIX,
    new_secret,
    token_hash,
)
from grove.oauth.store import ClientRow, GrantRow, find_client, list_grants, world_grants
from grove.store.connection import Driver
from grove.store.query import PlatformQuery, WorldQuery
from grove.strict_json import loads_strict
from grove.views.session import SESSION_COOKIE, ViewSession, find_session
from grove.views.times import iso_z
from grove.writer.oauth import (
    DYNAMIC,
    METADATA_DOCUMENT,
    PUBLIC_CLIENT,
    GrantRevoked,
    ScopeNotGranted,
    UnknownClient,
    grant_consent,
    issue_tokens,
    redeem_code,
    register_client,
    revoke_grant,
    revoke_token,
    rotate_refresh,
    store_code,
)

REGISTER_PATH = "/register"
AUTHORIZE_PATH = "/authorize"
DECISION_PATH = "/authorize/decision"
TOKEN_PATH = "/token"
REVOKE_PATH = "/revoke"
# The paths whose every answer the app marks no-store (the decision path starts with /authorize).
OAUTH_PATHS = (REGISTER_PATH, AUTHORIZE_PATH, TOKEN_PATH, REVOKE_PATH)
CONNECTIONS_PATH = f"{PREFIX}/connections"

CONSENT_FILE = "consent.html"
CONSENT_TITLE = "Connect an app: Grove"
FORM_TYPE = "application/x-www-form-urlencoded"
JSON_TYPE = "application/json"
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 64
MAX_STATE_LENGTH = 1024
BASIC_CHALLENGE = 'Basic realm="grove"'
BUSY_RETRY_SECONDS = 5

# RFC 6749 section 4.1.2.1 and 5.2, RFC 8707 section 2, RFC 7009 section 2.2.1.
INVALID_REQUEST = "invalid_request"
INVALID_CLIENT = "invalid_client"
INVALID_GRANT = "invalid_grant"
INVALID_SCOPE = "invalid_scope"
INVALID_TARGET = "invalid_target"
UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
ACCESS_DENIED = "access_denied"
TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
_REDIRECT_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_TOKEN_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# The pages /authorize shows for a refusal: (title, what happened).
_NO_CLIENT = ("This request names no app", "The link that sent you here names no app.")
_UNKNOWN_CLIENT = (
    "Grove does not know this app",
    "The app that sent you here is not registered with Grove, or its description cannot be read.",
)
_BAD_REDIRECT = (
    "This return address is not registered",
    "Grove sends you back only to an address the app registered, and this one is not.",
)
_NO_REDIRECT = (
    "This request names no return address",
    "The app registered more than one return address, and the request does not say which one.",
)
_NOT_VALID = "This request is not valid"
_TWICE = (_NOT_VALID, "The link names the app or its return address twice.")
_SIGNED_OUT = ("You are signed out", "Sign in to Grove, then connect the app again.")
_EXPIRED = (
    "This page has expired",
    "Grove cannot confirm that this answer came from its own page in this session.",
)
_NO_DECISION = ("This answer is not valid", "Choose Approve or Deny on the page.")
_UNREADABLE = ("This answer cannot be read", "The form did not arrive as Grove sent it.")
_BUSY = ("Grove is busy", "Grove cannot read this app's description now. Try again in a moment.")
_TOO_MANY_APPS = (
    "You have too many connected apps",
    "Disconnect an app in Grove, then connect this one again.",
)

router = APIRouter()


@dataclass
class OAuthDeps:
    """What create_app hands these routes. The resolver and the transport of the metadata
    document fetch are the system's unless a test passes fakes; the registration window is the
    cap of POST /register across the server, in process."""

    resolver: Resolver = system_resolver
    transport: httpx.BaseTransport | None = None
    window: CodeWindow = field(
        default_factory=lambda: CodeWindow(
            limit=MAX_REGISTRATIONS_PER_WINDOW, seconds=REGISTRATION_WINDOW_SECONDS
        )
    )


def _deps(request: Request) -> OAuthDeps:
    return request.app.state.oauth


def _settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def _driver(request: Request) -> Driver:
    return request.app.state.driver


# Parameters ------------------------------------------------------------------------------------


class Duplicate(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


Pairs = Sequence[tuple[str, str]]


def _values(pairs: Pairs, name: str) -> list[str]:
    return [value for key, value in pairs if key == name and value != ""]


def _one(pairs: Pairs, name: str) -> str | None:
    values = _values(pairs, name)
    if len(values) > 1:
        raise Duplicate(name)
    return values[0] if values else None


def _canonical_resource(pairs: Pairs, settings: ApiSettings) -> tuple[bool, str | None]:
    """(acceptable, the canonical resource or None). Absent is acceptable; every value given must
    be this server's <base>/mcp, with a trailing slash tolerated."""
    values = _values(pairs, "resource")
    if not values:
        return True, None
    canonical = settings.mcp_url
    if all(value in (canonical, f"{canonical}/") for value in values):
        return True, canonical
    return False, None


class FormRefused(Exception):
    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def _capped_body(request: Request, limit: int) -> bytes | None:
    """The body, or None when it is over the limit (declared or sent)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_form(request: Request) -> list[tuple[str, str]]:
    if _media_type(request) != FORM_TYPE:
        raise FormRefused(f"the body must be {FORM_TYPE}")
    raw = await _capped_body(request, MAX_FORM_BYTES)
    if raw is None:
        raise FormRefused(f"the body is over {MAX_FORM_BYTES} bytes")
    try:
        return parse_qsl(
            raw.decode("utf-8"),
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_FORM_FIELDS,
        )
    except ValueError:
        raise FormRefused("the body is not a valid form") from None


# Answers ---------------------------------------------------------------------------------------


class OAuthRefusal(Exception):
    """An OAuth error answer of /token, /revoke or /register."""

    def __init__(
        self, status: int, error: str, description: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(description)
        self.status, self.error, self.description = status, error, description
        self.headers = headers or {}

    def response(self) -> JSONResponse:
        return _oauth_error(self.status, self.error, self.description, self.headers)


def _oauth_error(
    status: int, error: str, description: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={**_TOKEN_HEADERS, **(headers or {})},
    )


def _param(pairs: Pairs, name: str) -> str | None:
    try:
        return _one(pairs, name)
    except Duplicate:
        raise OAuthRefusal(400, INVALID_REQUEST, f"{name} is given more than once") from None


def _template() -> str:
    folder = os.environ.get(WEB_DIST_ENV)
    if folder:
        try:
            return (Path(folder) / CONSENT_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            pass
    raise unavailable("the consent page is not built")


def _html(status: int, title: str, fragment: str) -> HTMLResponse:
    try:
        page = render_page(_template(), title=title, fragment=fragment)
    except TemplateBroken:
        raise unavailable("the consent page is not built") from None
    return HTMLResponse(page, status_code=status, headers=PAGE_HEADERS)


def _page(status: int, title: str, message: str) -> HTMLResponse:
    return _html(status, f"{title}: Grove", error_fragment(title, message))


def _busy_page() -> HTMLResponse:
    page = _page(503, *_BUSY)
    page.headers["Retry-After"] = str(BUSY_RETRY_SECONDS)
    return page


def _redirect(uri: str, params: dict[str, str]) -> RedirectResponse:
    return RedirectResponse(with_params(uri, params), status_code=302, headers=_REDIRECT_HEADERS)


# The authorization request ---------------------------------------------------------------------


class PageRefusal(Exception):
    def __init__(self, page: tuple[str, str]) -> None:
        super().__init__(page[0])
        self.title, self.message = page


def _not_valid(message: str) -> PageRefusal:
    """A refusal of a parameter. The message names the rule, never what the request sent."""
    return PageRefusal((_NOT_VALID, message))


@dataclass(frozen=True)
class AuthorizeRequest:
    client: ClientRow
    redirect_uri: str
    redirect_given: bool
    scopes: tuple[str, ...]
    code_challenge: str
    state: str | None
    resource: str | None

    def fields(self) -> dict[str, str]:
        """The form's hidden fields: the validated request, which the decision validates again."""
        fields = {"response_type": "code", "client_id": self.client.client_id}
        if self.redirect_given:
            fields["redirect_uri"] = self.redirect_uri
        fields["scope"] = scope_text(self.scopes)
        fields["code_challenge"] = self.code_challenge
        fields["code_challenge_method"] = S256
        if self.state is not None:
            fields["state"] = self.state
        if self.resource is not None:
            fields["resource"] = self.resource
        return fields

    def csrf_fields(self) -> dict[str, str]:
        return {
            "client_id": self.client.client_id,
            "redirect_uri": self.redirect_uri,
            "redirect_given": "true" if self.redirect_given else "false",
            "scope": scope_text(self.scopes),
            "code_challenge": self.code_challenge,
            "state": self.state or "",
            "resource": self.resource or "",
        }


def _client(request: Request, client_id: str, user_id: str | None) -> ClientRow | None:
    """A dynamic client from the store, or a metadata document's client, fetched for this person
    when its copy is not fresh. Both routes send a stranger away before a document gets here."""
    driver = _driver(request)
    if is_document_client_id(client_id):
        assert user_id is not None
        deps = _deps(request)
        return document_client(
            driver,
            client_id,
            now=clock.now(),
            user_id=user_id,
            resolver=deps.resolver,
            transport=deps.transport,
        )
    row = find_client(driver, client_id)
    return row if row is not None and row.registration_type == DYNAMIC else None


def _state(pairs: Pairs) -> str | None:
    """RFC 6749 appendix A.5: printable ASCII."""
    state = _one(pairs, "state")
    if state is None:
        return None
    if len(state) > MAX_STATE_LENGTH or not all(" " <= char <= "~" for char in state):
        raise _not_valid(
            f"The state must be printable ASCII of at most {MAX_STATE_LENGTH} characters."
        )
    return state


def _validate(request: Request, pairs: Pairs, user_id: str | None) -> AuthorizeRequest:
    """The checks of /authorize, in the design's order, for the person who asks (None for a
    stranger, who never names a document here). Each refusal is a PageRefusal, and a document fetch
    that did not start and has no copy to serve is DocumentBusy."""
    try:
        client_id = _one(pairs, "client_id")
        given = _one(pairs, "redirect_uri")
    except Duplicate:
        raise PageRefusal(_TWICE) from None
    if client_id is None:
        raise PageRefusal(_NO_CLIENT)
    client = _client(request, client_id, user_id)
    if client is None:
        raise PageRefusal(_UNKNOWN_CLIENT)
    if given is not None:
        if not redirect_matches(given, client.redirect_uris):
            raise PageRefusal(_BAD_REDIRECT)
        redirect_uri, redirect_given = given, True
    elif len(client.redirect_uris) == 1:
        redirect_uri, redirect_given = client.redirect_uris[0], False
    else:
        raise PageRefusal(_NO_REDIRECT)

    try:
        state = _state(pairs)
        response_type = _one(pairs, "response_type")
        challenge = _one(pairs, "code_challenge")
        method = _one(pairs, "code_challenge_method")
        scope = _one(pairs, "scope")
    except Duplicate as twice:
        raise _not_valid(f"The link gives {twice.name} more than once.") from None
    if response_type is None:
        raise _not_valid("The link gives no response_type.")
    if response_type != "code":
        raise _not_valid("The response_type must be code.")
    if not valid_pkce_value(challenge):
        raise _not_valid("The code_challenge must be 43 to 128 unreserved characters.")
    if method != S256:
        raise _not_valid("The code_challenge_method must be S256.")
    settings = _settings(request)
    acceptable, resource = _canonical_resource(pairs, settings)
    if not acceptable:
        raise _not_valid(f"The resource must be {settings.mcp_url}.")
    assert challenge is not None
    return AuthorizeRequest(
        client=client,
        redirect_uri=redirect_uri,
        redirect_given=redirect_given,
        scopes=granted_scopes(scope),
        code_challenge=challenge,
        state=state,
        resource=resource,
    )


def _to_signin(request: Request) -> RedirectResponse:
    query = request.url.query
    back = AUTHORIZE_PATH + (f"?{query}" if query else "")
    return RedirectResponse(
        f"/signin?next={quote(back, safe='')}", status_code=303, headers=_REDIRECT_HEADERS
    )


_OWN_TITLE = "MATCH (i:Item {id: $id}) RETURN i.title AS title"


def _own_title(driver: Driver, session: ViewSession) -> str | None:
    if not session.self_item_id:
        return None
    rows = WorldQuery(driver, session.world_id).read(_OWN_TITLE, id=session.self_item_id)
    title = rows[0]["title"] if rows else None
    return title if isinstance(title, str) and title else None


def _signed_in(request: Request) -> tuple[str, ViewSession] | None:
    token = request.cookies.get(SESSION_COOKIE)
    session = find_session(PlatformQuery(_driver(request)), token, clock.now())
    if token is None or session is None:
        return None
    return token, session


def _names_a_document(pairs: Pairs) -> bool:
    try:
        return is_document_client_id(_one(pairs, "client_id"))
    except Duplicate:
        return False


@router.get(AUTHORIZE_PATH, include_in_schema=False)
def authorize(request: Request) -> Response:
    pairs = request.query_params.multi_items()
    signed_in = _signed_in(request)
    # The fetch of a document is for a person who signed in, never for a stranger.
    if signed_in is None and _names_a_document(pairs):
        return _to_signin(request)
    try:
        asked = _validate(request, pairs, signed_in[1].user_id if signed_in else None)
    except DocumentBusy:
        return _busy_page()
    except PageRefusal as refused:
        return _page(400, refused.title, refused.message)
    if signed_in is None:
        return _to_signin(request)
    token, session = signed_in
    client = asked.client
    view = ConsentView(
        client_name=client.host,
        document_host=(
            shown_host(client.client_id) if client.registration_type == METADATA_DOCUMENT else None
        ),
        return_place=return_place(asked.redirect_uri),
        scopes=asked.scopes,
        person_title=_own_title(_driver(request), session),
        loopback=is_loopback(asked.redirect_uri),
    )
    fragment = consent_fragment(view, asked.fields(), csrf_token(token, asked.csrf_fields()))
    return _html(200, CONSENT_TITLE, fragment)


def _decide(request: Request, pairs: Pairs) -> Response:
    signed_in = _signed_in(request)
    if signed_in is None:
        return _page(401, *_SIGNED_OUT)
    token, session = signed_in
    try:
        asked = _validate(request, pairs, session.user_id)
    except DocumentBusy:
        return _busy_page()
    except PageRefusal as refused:
        return _page(400, refused.title, refused.message)
    try:
        csrf = _one(pairs, "csrf")
        decision = _one(pairs, "decision")
    except Duplicate:
        return _page(400, *_EXPIRED)
    if not same_csrf(csrf, csrf_token(token, asked.csrf_fields())):
        return _page(400, *_EXPIRED)
    back = {"state": asked.state} if asked.state is not None else {}
    issuer = _settings(request).public_base_url
    if decision == DENY:
        refusal = {"error": ACCESS_DENIED, "error_description": "the person denied the request"}
        return _redirect(asked.redirect_uri, {**refusal, **back, "iss": issuer})
    if decision != APPROVE:
        return _page(400, *_NO_DECISION)
    driver = _driver(request)
    grants = world_grants(driver, session.world_id, asked.client.client_id)
    if not grants.has_client and grants.count >= MAX_GRANTS_PER_WORLD:
        return _page(400, *_TOO_MANY_APPS)
    now = clock.now()
    if not asked.client.stored:
        store_document_client(driver, asked.client, at=now)
    try:
        grant_id = grant_consent(
            driver,
            client_id=asked.client.client_id,
            user_id=session.user_id,
            world_id=session.world_id,
            scopes=list(asked.scopes),
            at=now,
        )
    except UnknownClient:
        return _page(400, *_UNKNOWN_CLIENT)
    code = new_secret(CODE_PREFIX)
    store_code(
        driver,
        code_hash=token_hash(code),
        client_id=asked.client.client_id,
        user_id=session.user_id,
        world_id=session.world_id,
        grant_id=grant_id,
        code_challenge=asked.code_challenge,
        redirect_uri=asked.redirect_uri,
        redirect_given=asked.redirect_given,
        scopes=list(asked.scopes),
        resource=asked.resource,
        at=now,
        expires_at=now + CODE_LIFETIME,
    )
    return _redirect(asked.redirect_uri, {"code": code, **back, "iss": issuer})


@router.post(DECISION_PATH, include_in_schema=False)
async def authorize_decision(request: Request) -> Response:
    try:
        pairs = await _read_form(request)
    except FormRefused:
        return await run_in_threadpool(_page, 400, *_UNREADABLE)
    return await run_in_threadpool(_decide, request, pairs)


# Client authentication -------------------------------------------------------------------------


def _basic(header: str | None) -> tuple[str, str] | None:
    """The (client_id, secret) of an Authorization: Basic header, each part form-url-decoded
    (RFC 6749 section 2.3.1); None for no Basic header. A broken one is refused."""
    if not header or header[:6].lower() != "basic ":
        return None
    unreadable = OAuthRefusal(
        401,
        INVALID_CLIENT,
        "the Basic credentials cannot be read",
        {"WWW-Authenticate": BASIC_CHALLENGE},
    )
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        raise unreadable from None
    name, separator, secret = decoded.partition(":")
    if not separator:
        raise unreadable
    return unquote_plus(name), unquote_plus(secret)


def _authenticate(request: Request, pairs: Pairs) -> ClientRow:
    """The client of a /token or /revoke request: client_secret_basic, client_secret_post, or a
    public client that sends its client_id alone. Secrets are compared as hashes."""
    basic = _basic(request.headers.get("authorization"))
    form_id = _param(pairs, "client_id")
    form_secret = _param(pairs, "client_secret")
    challenge = {"WWW-Authenticate": BASIC_CHALLENGE} if basic is not None else {}
    if basic is not None:
        if form_secret is not None:
            raise OAuthRefusal(400, INVALID_REQUEST, "use one way to authenticate the client")
        if form_id is not None and form_id != basic[0]:
            raise OAuthRefusal(401, INVALID_CLIENT, "client_id differs from Basic", challenge)
        client_id, secret = basic
    else:
        client_id, secret = form_id, form_secret
    if not client_id:
        raise OAuthRefusal(401, INVALID_CLIENT, "client authentication is required", challenge)
    client = find_client(_driver(request), client_id)
    if client is None:
        raise OAuthRefusal(401, INVALID_CLIENT, "the client is unknown", challenge)
    if client.auth_method == PUBLIC_CLIENT:
        if secret:
            raise OAuthRefusal(401, INVALID_CLIENT, "a public client has no secret", challenge)
        return client
    known = client.secret_hash
    if not secret or known is None or not _same_hash(token_hash(secret), known):
        raise OAuthRefusal(401, INVALID_CLIENT, "the client secret does not match", challenge)
    return client


def _same_hash(first: str, second: str) -> bool:
    return hmac.compare_digest(first.encode("ascii"), second.encode("ascii"))


# /token ----------------------------------------------------------------------------------------


def _tokens(access: str, refresh: str, scopes: Sequence[str]) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            "refresh_token": refresh,
            "scope": scope_text(scopes),
        },
        headers=_TOKEN_HEADERS,
    )


def _check_resource(pairs: Pairs, settings: ApiSettings) -> None:
    acceptable, _ = _canonical_resource(pairs, settings)
    if not acceptable:
        raise OAuthRefusal(400, INVALID_TARGET, f"resource must be {settings.mcp_url}")


def _code_grant(request: Request, pairs: Pairs, client: ClientRow) -> JSONResponse:
    settings, driver = _settings(request), _driver(request)
    code = _param(pairs, "code")
    verifier = _param(pairs, "code_verifier")
    redirect_uri = _param(pairs, "redirect_uri")
    if code is None:
        raise OAuthRefusal(400, INVALID_REQUEST, "code is required")
    if verifier is None:
        raise OAuthRefusal(400, INVALID_REQUEST, "code_verifier is required")
    if not valid_pkce_value(verifier):
        raise OAuthRefusal(
            400, INVALID_REQUEST, "code_verifier must be 43 to 128 unreserved characters"
        )
    _check_resource(pairs, settings)
    now = clock.now()
    # From here the code is spent: a wrong verifier, client or redirect burns it.
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=now)
    if redeemed is None:
        raise OAuthRefusal(400, INVALID_GRANT, "the code is unknown, expired or used")
    if redeemed.client_id != client.client_id:
        raise OAuthRefusal(400, INVALID_GRANT, "the code was issued to another client")
    if not verifier_matches(verifier, redeemed.code_challenge):
        raise OAuthRefusal(400, INVALID_GRANT, "the code_verifier does not match the challenge")
    named = redeemed.redirect_given or redirect_uri is not None
    if named and redirect_uri != redeemed.redirect_uri:
        raise OAuthRefusal(400, INVALID_GRANT, "redirect_uri differs from the authorize request")
    access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret(REFRESH_TOKEN_PREFIX)
    try:
        issue_tokens(
            driver,
            access_hash=token_hash(access),
            refresh_hash=token_hash(refresh),
            client_id=client.client_id,
            user_id=redeemed.user_id,
            world_id=redeemed.world_id,
            grant_id=redeemed.grant_id,
            family_id=redeemed.id,
            scopes=list(redeemed.scopes),
            resource=settings.mcp_url,
            at=now,
            access_expires_at=now + ACCESS_TOKEN_LIFETIME,
            refresh_expires_at=now + REFRESH_TOKEN_LIFETIME,
        )
    except GrantRevoked:
        raise OAuthRefusal(400, INVALID_GRANT, "the grant is revoked") from None
    return _tokens(access, refresh, redeemed.scopes)


def _refresh_grant(request: Request, pairs: Pairs, client: ClientRow) -> JSONResponse:
    settings, driver = _settings(request), _driver(request)
    refresh_token = _param(pairs, "refresh_token")
    scope = _param(pairs, "scope")
    if refresh_token is None:
        raise OAuthRefusal(400, INVALID_REQUEST, "refresh_token is required")
    _check_resource(pairs, settings)
    narrowed = list(known_scopes(scope)) if scope is not None else []
    now = clock.now()
    access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret(REFRESH_TOKEN_PREFIX)
    try:
        issued = rotate_refresh(
            driver,
            refresh_hash=token_hash(refresh_token),
            client_id=client.client_id,
            new_access_hash=token_hash(access),
            new_refresh_hash=token_hash(refresh),
            scopes=narrowed or None,
            at=now,
            access_expires_at=now + ACCESS_TOKEN_LIFETIME,
            refresh_expires_at=now + REFRESH_TOKEN_LIFETIME,
        )
    except ScopeNotGranted:
        raise OAuthRefusal(
            400, INVALID_SCOPE, "scope may only narrow the scopes of the refresh token"
        ) from None
    if issued is None:
        raise OAuthRefusal(
            400, INVALID_GRANT, "the refresh token is unknown, expired, used or revoked"
        )
    return _tokens(access, refresh, issued.scopes)


def _token(request: Request, pairs: Pairs) -> JSONResponse:
    try:
        client = _authenticate(request, pairs)
        grant_type = _param(pairs, "grant_type")
        if grant_type is None:
            raise OAuthRefusal(400, INVALID_REQUEST, "grant_type is required")
        if grant_type == "authorization_code":
            return _code_grant(request, pairs, client)
        if grant_type == "refresh_token":
            return _refresh_grant(request, pairs, client)
        raise OAuthRefusal(
            400, UNSUPPORTED_GRANT_TYPE, "grant_type must be authorization_code or refresh_token"
        )
    except OAuthRefusal as refused:
        return refused.response()


@router.post(TOKEN_PATH, include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    try:
        pairs = await _read_form(request)
    except FormRefused as refused:
        return _oauth_error(400, INVALID_REQUEST, refused.description)
    return await run_in_threadpool(_token, request, pairs)


# /revoke ---------------------------------------------------------------------------------------


def _revoke(request: Request, pairs: Pairs) -> JSONResponse:
    """RFC 7009: once the client is authenticated, the answer is 200 whatever the token was."""
    try:
        client = _authenticate(request, pairs)
        value = _param(pairs, "token")
        _param(pairs, "token_type_hint")
        if value is None:
            raise OAuthRefusal(400, INVALID_REQUEST, "token is required")
    except OAuthRefusal as refused:
        return refused.response()
    revoke_token(
        _driver(request), token_hash=token_hash(value), client_id=client.client_id, at=clock.now()
    )
    return JSONResponse({}, headers=_TOKEN_HEADERS)


@router.post(REVOKE_PATH, include_in_schema=False)
async def revoke(request: Request) -> JSONResponse:
    try:
        pairs = await _read_form(request)
    except FormRefused as refused:
        return _oauth_error(400, INVALID_REQUEST, refused.description)
    return await run_in_threadpool(_revoke, request, pairs)


# /register -------------------------------------------------------------------------------------


async def _registration_body(request: Request) -> Any:
    if _media_type(request) != JSON_TYPE:
        raise InvalidMetadata(INVALID_CLIENT_METADATA, f"the body must be {JSON_TYPE}")
    raw = await _capped_body(request, MAX_REGISTRATION_BYTES)
    if raw is None:
        raise InvalidMetadata(
            INVALID_CLIENT_METADATA, f"the body is over {MAX_REGISTRATION_BYTES} bytes"
        )
    try:
        return loads_strict(raw)
    except ValueError:
        raise InvalidMetadata(INVALID_CLIENT_METADATA, "the body is not strict JSON") from None


def _register(request: Request, metadata: ClientMetadata) -> JSONResponse:
    deps = _deps(request)
    waited = deps.window.take()
    if waited is not None:
        return _oauth_error(
            429,
            TEMPORARILY_UNAVAILABLE,
            "too many registrations: try again later",
            {"Retry-After": str(waited)},
        )
    now = clock.now()
    client_id = new_id()
    secret = new_secret(CLIENT_SECRET_PREFIX) if metadata.confidential else None
    try:
        register_client(
            _driver(request),
            client_id=client_id,
            host=metadata.client_name,
            redirect_uris=list(metadata.redirect_uris),
            registration_type=DYNAMIC,
            auth_method=metadata.auth_method,
            secret_hash=token_hash(secret) if secret is not None else None,
            at=now,
        )
    except Exception:
        # A registration that stored nothing spends no slot.
        deps.window.give_back()
        raise
    body: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(now.timestamp()),
        "redirect_uris": list(metadata.redirect_uris),
        "grant_types": list(metadata.grant_types),
        "response_types": list(metadata.response_types),
        "token_endpoint_auth_method": metadata.auth_method,
        "scope": scope_text(SCOPES),
    }
    if metadata.client_name is not None:
        body["client_name"] = metadata.client_name
    if secret is not None:
        body["client_secret"] = secret
        body["client_secret_expires_at"] = 0
    return JSONResponse(body, status_code=201, headers=_TOKEN_HEADERS)


@router.post(REGISTER_PATH, include_in_schema=False)
async def register(request: Request) -> JSONResponse:
    """RFC 7591 with no initial access token. The cap across the server answers 429."""
    try:
        metadata = parse_registration(await _registration_body(request))
    except InvalidMetadata as refused:
        return _oauth_error(400, refused.error, refused.description)
    return await run_in_threadpool(_register, request, metadata)


# The person's connections (the views) ---------------------------------------------------------


def _connection(grant: GrantRow) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "client_id": grant.client_id,
        "host": grant.host,
        "registration_type": grant.registration_type,
        "scopes": list(grant.scopes),
        "granted_at": iso_z(grant.granted_at),
    }


@router.get(CONNECTIONS_PATH)
def get_connections(request: Request, session: Session) -> dict[str, Any]:
    """The hosts this world has approved and not revoked, newest first."""
    grants = list_grants(_driver(request), session.world_id)
    return {
        "mcp_url": _settings(request).mcp_url,
        "connections": [_connection(grant) for grant in grants],
    }


@router.post(CONNECTIONS_PATH + "/{grant_id}/revoke")
def post_connection_revoke(request: Request, session: Session, grant_id: str) -> dict[str, bool]:
    """Ends one host's access to this world: the grant and every live token of it."""
    check_json_type(request)
    if not revoke_grant(
        _driver(request), grant_id=grant_id, world_id=session.world_id, at=clock.now()
    ):
        raise not_found("no such connection")
    return {"revoked": True}
