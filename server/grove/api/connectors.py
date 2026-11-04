"""Session-only source connections. Provider credentials never reach a response."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from grove import clock
from grove.api import google as signin_google
from grove.api.errors import ApiError, invalid_arguments, not_found, unavailable
from grove.api.session import current_session
from grove.api.uploads import _screener
from grove.api.views import check_json_type, read_json_body
from grove.connectors import google
from grove.connectors.calendar import API_BASE, CalendarRefused, identifier
from grove.connectors.secrets import SecretUnavailable, Vault, load_vault
from grove.connectors.service import sync_calendar
from grove.ingest import NotAnExport, message_key
from grove.store.query import WorldQuery
from grove.views.session import SESSION_COOKIE
from grove.writer.connectors import (
    ConnectionBusy,
    connection,
    consume_state,
    disconnect,
    ensure_connection,
    update_connection,
)
from grove.writer.errors import InvalidRecord

PREFIX = "/v1/connectors"
router = APIRouter(prefix=PREFIX)
STATE_SECONDS = 600


@dataclass
class ConnectorDeps:
    http: httpx.Client | None = None
    client: signin_google.GoogleClient | None = None
    vault: Vault | None = None
    calendar_base: str = API_BASE
    repositories: dict[tuple[str, str], Path] = field(default_factory=dict)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _deps(request: Request) -> ConnectorDeps:
    return request.app.state.connectors


def _client(request: Request):
    client = _deps(request).client or signin_google.load_client(
        request.app.state.settings.google_oauth_file
    )
    if client is None:
        raise unavailable("Google Calendar is not configured")
    return client


def _vault(request: Request):
    try:
        return _deps(request).vault or load_vault()
    except SecretUnavailable:
        raise unavailable("source credential storage is not configured") from None


def _cookie(request: Request):
    return (
        "__Host-grove_calendar" if request.app.state.settings.secure_cookies else "grove_calendar"
    )


def _session_hash(request: Request):
    return _hash(request.cookies.get(SESSION_COOKIE, ""))


async def _body(request: Request) -> dict:
    origin = request.headers.get("origin")
    if (origin and origin != request.app.state.settings.public_base_url) or request.headers.get(
        "sec-fetch-site"
    ) == "cross-site":
        raise ApiError(403, "forbidden", "request origin is not allowed")
    check_json_type(request)
    body = await read_json_body(request, 8192)
    if not isinstance(body, dict):
        raise invalid_arguments("an object is required", "body")
    return body


def _settings(body: dict) -> dict:
    out = {}
    if body.get("log_from") is not None:
        try:
            out["log_from"] = date.fromisoformat(body["log_from"]).isoformat()
        except (TypeError, ValueError):
            raise invalid_arguments("log_from must be a date", "log_from") from None
    days = body.get("logged_days", [])
    if not isinstance(days, list) or len(days) > 3660:
        raise invalid_arguments("logged_days must be a bounded date list", "logged_days")
    try:
        out["logged_days"] = sorted({date.fromisoformat(day).isoformat() for day in days})
    except (ValueError, TypeError):
        raise invalid_arguments("logged_days must contain dates", "logged_days") from None
    return out


@router.post("/google/start")
async def google_start(request: Request):
    session = current_session(request)
    body = await _body(request)
    try:
        calendar_id = identifier(body.get("calendar_id", "primary"))
    except CalendarRefused:
        raise invalid_arguments("calendar_id is not valid", "calendar_id") from None
    settings = _settings(body)
    client, vault = _client(request), _vault(request)
    held = signin_google.new_state("/")
    driver, world = request.app.state.driver, session.world_id
    pending = ensure_connection(
        driver, world, "google_calendar", "pending:" + _session_hash(request)
    )
    update_connection(
        driver,
        world,
        pending,
        oauth_state_hash=_hash(held.state),
        oauth_expires_at=clock.now(world) + timedelta(seconds=STATE_SECONDS),
        settings=json.dumps(settings),
        credential_ciphertext=vault.seal(
            {"calendar_id": calendar_id}, world=world, reference=pending
        ),
    )
    state = vault.seal(
        {
            "pending": pending,
            "held": signin_google.pack_state(held),
            "session": _session_hash(request),
        },
        world=world,
        reference="oauth-state",
    )
    url = google.authorization_url(
        client, held, request.app.state.settings.public_base_url + google.CALLBACK
    )
    response = JSONResponse({"authorization_url": url})
    response.set_cookie(
        _cookie(request),
        state,
        max_age=STATE_SECONDS,
        path="/",
        httponly=True,
        secure=request.app.state.settings.secure_cookies,
        samesite="lax",
    )
    return response


@router.get("/google/callback")
def google_callback(request: Request):
    session = current_session(request)
    driver, world = request.app.state.driver, session.world_id
    good = False
    owned_http = None
    try:
        vault = _vault(request)
        state = vault.open(
            request.cookies.get(_cookie(request), ""), world=world, reference="oauth-state"
        )
        held = signin_google.unpack_state(state.get("held"))
        if (
            not held
            or not signin_google.same_text(state.get("session", ""), _session_hash(request))
            or not signin_google.same_text(request.query_params.get("state", ""), held.state)
        ):
            raise CalendarRefused("denied")
        pending = consume_state(driver, world, state["pending"], _hash(held.state))
        if pending is None:
            raise CalendarRefused("denied")
        calendar_id = identifier(
            vault.open(pending["credential_ciphertext"], world=world, reference=state["pending"])[
                "calendar_id"
            ]
        )
        code = request.query_params.get("code")
        if request.query_params.get("error") or not code or len(code) > 8192:
            raise CalendarRefused("denied")
        http = _deps(request).http
        if http is None:
            owned_http = http = httpx.Client()
        tokens = google.exchange(
            http,
            _client(request),
            held,
            code,
            request.app.state.settings.public_base_url + google.CALLBACK,
        )
        # Different Google accounts with a primary calendar are different connections.
        identity = message_key("account", tokens["subject"], message_key("cal", calendar_id))
        chosen = ensure_connection(driver, world, "google_calendar", identity)
        tokens["calendar_id"] = calendar_id
        update_connection(
            driver,
            world,
            chosen,
            connection_status="connected",
            credential_ciphertext=vault.seal(tokens, world=world, reference=chosen),
            settings=pending["settings"],
            scope=json.dumps([message_key("cal", calendar_id)]),
        )
        good = True
    except (SecretUnavailable, CalendarRefused, KeyError, InvalidRecord, ConnectionBusy):
        pass
    finally:
        if owned_http:
            owned_http.close()
    response = RedirectResponse(
        "/?calendar=connected" if good else "/?calendar=refused",
        status_code=303,
        headers={"Referrer-Policy": "no-referrer"},
    )
    response.delete_cookie(
        _cookie(request),
        path="/",
        httponly=True,
        secure=request.app.state.settings.secure_cookies,
        samesite="lax",
    )
    return response


@router.post("/{connection_id}/sync")
async def sync(request: Request, connection_id: str):
    session = current_session(request)
    await _body(request)
    driver, world = request.app.state.driver, session.world_id
    row = _owned(driver, world, connection_id)
    # Provider I/O runs off the event loop; the database lease serializes a connection's reads.
    from starlette.concurrency import run_in_threadpool

    from grove.connectors.service import sync_git

    owned_http = None
    try:
        if row["provider"] == "git":
            path = _repository(request, world, json.loads(row["settings"])["repository"])
            return await run_in_threadpool(
                sync_git, driver, world, connection_id, path=path, screener=_screener()
            )
        http = _deps(request).http
        if http is None:
            owned_http = http = httpx.Client()
        return await run_in_threadpool(
            sync_calendar,
            driver,
            world,
            connection_id,
            http=http,
            client=_client(request),
            vault=_vault(request),
            screener=_screener(),
            base_url=_deps(request).calendar_base,
        )
    except ConnectionBusy:
        raise ApiError(409, "busy", "source sync is already active") from None
    except (CalendarRefused, SecretUnavailable, InvalidRecord, NotAnExport):
        raise unavailable("source could not be read; check its connection") from None
    finally:
        if owned_http:
            owned_http.close()


def _owned(driver, world, connection_id):
    try:
        return connection(driver, world, connection_id)
    except InvalidRecord:
        raise not_found("no such source connection") from None


def _repository(request: Request, world: str, alias: str) -> Path:
    return repository_path(world, alias, _deps(request))


def repository_path(world: str, alias: str, deps: ConnectorDeps) -> Path:
    # Tests can inject a per-world map. Production reads only the operator's mode-600 allowlist.
    path = deps.repositories.get((world, alias))
    if path is not None:
        return path
    configured = os.environ.get("GROVE_GIT_REPOSITORIES_FILE")
    if not configured:
        raise not_found("repository is not allowed for this world")
    try:
        fd = os.open(configured, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                raise ValueError()
            mapping = json.load(source)
        selected = mapping[world][alias]
        if not isinstance(selected, str) or not Path(selected).is_absolute():
            raise ValueError()
        return Path(selected)
    except (OSError, ValueError, TypeError, KeyError):
        raise not_found("repository is not allowed for this world") from None


@router.post("/git")
async def git_connect(request: Request):
    session = current_session(request)
    body = await _body(request)
    alias = body.get("repository")
    if (
        not isinstance(alias, str)
        or not 1 <= len(alias) <= 64
        or not alias.isascii()
        or not all(c.isalnum() or c in "_-" for c in alias)
    ):
        raise invalid_arguments("repository must be an allowed alias", "repository")
    _repository(request, session.world_id, alias)
    chosen = ensure_connection(request.app.state.driver, session.world_id, "git", alias)
    update_connection(
        request.app.state.driver,
        session.world_id,
        chosen,
        connection_status="connected",
        settings=json.dumps({"repository": alias}),
        scope=json.dumps([alias]),
    )
    return {"id": chosen, "provider": "git", "state": "connected"}


@router.post("/{connection_id}/settings")
async def settings(request: Request, connection_id: str):
    session = current_session(request)
    body = await _body(request)
    row = _owned(request.app.state.driver, session.world_id, connection_id)
    if row["provider"] != "google_calendar":
        raise invalid_arguments("only calendar log settings can change", "settings")
    try:
        update_connection(
            request.app.state.driver,
            session.world_id,
            connection_id,
            settings=json.dumps(_settings(body)),
        )
    except ConnectionBusy:
        raise ApiError(409, "busy", "source sync is already active") from None
    return {"id": connection_id, "state": "saved"}


@router.post("/{connection_id}/disconnect")
async def remove(request: Request, connection_id: str):
    session = current_session(request)
    await _body(request)
    _owned(request.app.state.driver, session.world_id, connection_id)
    try:
        disconnect(request.app.state.driver, session.world_id, connection_id)
    except ConnectionBusy:
        raise ApiError(409, "busy", "source sync is already active") from None
    return {"id": connection_id, "state": "disconnected"}


@router.get("")
def list_connections(request: Request):
    session = current_session(request)
    rows = WorldQuery(request.app.state.driver, session.world_id).read(
        "MATCH (c:Connector) WHERE c.connection_status IN ['connected', 'disconnected'] "
        "RETURN c.id AS id, c.provider AS provider, c.connection_status AS state, "
        "c.last_outcome AS last_outcome, c.read_count AS read_count, "
        "c.denied_count AS denied_count, c.failed_count AS failed_count"
    )
    return {"connections": rows}
