"""Session-owned Pipedream Connect links and account status, without content access."""

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from grove.api.errors import ApiError, invalid_arguments, unavailable
from grove.api.session import current_session
from grove.demo_policy import is_demo_world
from grove.views.session import ViewSession

PREFIX = "/v1/views/pipedream"
PROVIDERS = {"gmail": "Gmail", "google_calendar": "Google Calendar", "notion": "Notion"}
API_BASE = "https://api.pipedream.com/v1"
CONNECT_TTL = 600
MAX_RESPONSE_BYTES = 512_000
CATALOG_PAGE_SIZE = 30
ACCOUNT_PAGE_SIZE = 100
MAX_ACCOUNT_PAGES = 3
MAX_DISCONNECT_ACCOUNTS = 20
router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]


@dataclass(frozen=True, repr=False)
class Settings:
    project_id: str
    client_id: str
    client_secret: str
    environment: str


def load_settings() -> Settings | None:
    values = [
        os.environ.get("PIPEDREAM_" + key, "").strip()
        for key in ("PROJECT_ID", "CLIENT_ID", "CLIENT_SECRET", "ENVIRONMENT")
    ]
    if not all(values):
        return None
    project, client_id, secret, environment = values
    if (
        not re.fullmatch(r"proj_[A-Za-z0-9]{1,120}", project)
        or environment != "production"
        or any(len(value) > 4096 or not value.isprintable() for value in values)
    ):
        return None
    return Settings(project, client_id, secret, environment)


def external_user_id(session: ViewSession) -> str:
    """Stable across sessions and secret rotation; no email or browser identity is used."""
    return (
        "grove_"
        + hashlib.sha256(("grove:pipedream:v1:" + session.user_id).encode("utf-8")).hexdigest()
    )


async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=API_BASE, timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=False
    ) as client:
        yield client


Http = Annotated[httpx.AsyncClient, Depends(http_client)]


async def _json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    missing_app: bool = False,
    empty: bool = False,
    **kwargs: Any,
) -> Any:
    """Never expose provider bodies, URLs, tokens or transport exception text in errors."""
    try:
        async with client.stream(method, path, **kwargs) as response:
            if response.status_code == 404 and missing_app:
                raise invalid_arguments("This app is not available.", "provider")
            if response.status_code == 429:
                raise unavailable("Connections are busy. Try again shortly.")
            if response.status_code in (401, 403):
                raise unavailable(
                    "Connections are unavailable. The server credentials need attention."
                )
            if response.status_code < 200 or response.status_code >= 300:
                raise unavailable("Connection status could not be checked. Try again.")
            if empty and response.status_code == 204:
                return None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise unavailable("The connection service returned an invalid response.")
            return json.loads(data)
    except (httpx.HTTPError, ValueError):
        raise unavailable("The connection service could not be reached. Try again.") from None


async def _headers(
    client: httpx.AsyncClient, settings: Settings, scope: str = "connect:accounts:read"
) -> dict[str, str]:
    result = await _json(
        client,
        "POST",
        "/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "scope": scope,
        },
    )
    token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token or len(token) > 8192 or not token.isprintable():
        raise unavailable("The connection service returned an invalid response.")
    return {"Authorization": "Bearer " + token, "x-pd-environment": settings.environment}


def _provider(provider: str) -> str:
    if not re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,119}", provider):
        raise invalid_arguments("Choose an app from the connection catalog.", "provider")
    return provider


def _hosted_link(value: Any, provider: str) -> str:
    if not isinstance(value, str) or len(value) > 16_384:
        raise unavailable("The connection service returned an invalid link.")
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port not in (None, 443)
            or not (host == "pipedream.com" or host.endswith(".pipedream.com"))
        ):
            raise ValueError
        query = [(key, val) for key, val in parse_qsl(url.query) if key != "app"]
        query.append(("app", provider))
        return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
    except ValueError:
        raise unavailable("The connection service returned an invalid link.") from None


async def _eligible(request: Request, session: ViewSession) -> None:
    if "authorization" in request.headers:
        raise ApiError(401, "unauthenticated", "Connections require a browser session.")
    if await run_in_threadpool(is_demo_world, request.app.state.driver, session.world_id):
        raise ApiError(403, "scope_denied", "This account cannot connect external apps.")


def _query(request: Request, allowed: set[str]) -> None:
    keys = list(request.query_params.keys())
    if set(keys) - allowed or any(len(request.query_params.getlist(key)) != 1 for key in keys):
        raise invalid_arguments("Unexpected connection query fields.", "query")


async def _body(request: Request, *, confirmation: bool = False) -> None:
    base = request.app.state.settings.public_base_url
    if (
        request.headers.get("origin") != base
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise ApiError(403, "forbidden", "Open connections from Grove.")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise invalid_arguments("Use a JSON request.", "body")
    _query(request, set())
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024:
            raise invalid_arguments("The connection request is too large.", "body")
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    valid = isinstance(value, dict) and (
        (set(value) == {"confirm"} and value["confirm"] is True) if confirmation else not value
    )
    if not valid:
        raise invalid_arguments(
            "Confirm this disconnection."
            if confirmation
            else "The connection request accepts no identity or options.",
            "body",
        )


def _page(value: Any, limit: int) -> tuple[list[dict[str, Any]], str | None, int]:
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise unavailable("The connection service returned an invalid response.")
    rows, info = value["data"], value.get("page_info")
    if (
        len(rows) > limit
        or any(not isinstance(row, dict) for row in rows)
        or not isinstance(info, dict)
    ):
        raise unavailable("The connection service returned an invalid response.")
    count, total, cursor = info.get("count"), info.get("total_count"), info.get("end_cursor")
    if (
        type(count) is not int
        or count != len(rows)
        or type(total) is not int
        or total < count
        or total > 2**53 - 1
        or (
            cursor is not None
            and (
                not isinstance(cursor, str)
                or len(cursor) > 1024
                or (cursor and not cursor.isprintable())
            )
        )
    ):
        raise unavailable("The connection service returned an invalid response.")
    # Some pages include an end cursor even when the complete list fits on this page.
    next_cursor = cursor or None if total > count and rows else None
    return rows, next_cursor, total


def _app_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise unavailable("The connection service returned invalid app metadata.")
    slug, name, auth = value.get("name_slug"), value.get("name"), value.get("auth_type")
    if (
        not isinstance(slug, str)
        or not re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,119}", slug)
        or not isinstance(name, str)
        or not 1 <= len(name) <= 200
        or not name.isprintable()
        or auth not in ("oauth", "keys", "none", None)
    ):
        raise unavailable("The connection service returned invalid app metadata.")
    return {
        "provider": slug,
        "name": name,
        "auth_type": auth,
        "connectable": auth in ("oauth", "keys"),
    }


async def _app(client: httpx.AsyncClient, provider: str, headers: dict[str, str]) -> dict[str, Any]:
    result = await _json(
        client, "GET", f"/connect/apps/{provider}", headers=headers, missing_app=True
    )
    app = _app_row(result.get("data") if isinstance(result, dict) else None)
    if app["provider"] != provider or not app["connectable"]:
        raise invalid_arguments("This app does not support an account connection.", "provider")
    return app


async def _accounts(
    client: httpx.AsyncClient,
    settings: Settings,
    session: ViewSession,
    provider: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    cursors: set[str] = set()
    after = None
    for _ in range(MAX_ACCOUNT_PAGES):
        params = {
            "external_user_id": external_user_id(session),
            "app": provider,
            "include_credentials": "false",
            "limit": str(ACCOUNT_PAGE_SIZE),
        }
        if after is not None:
            params["after"] = after
        result = await _json(
            client,
            "GET",
            f"/connect/{settings.project_id}/accounts",
            headers=headers,
            params=params,
        )
        rows, after, total = _page(result, ACCOUNT_PAGE_SIZE)
        if any(
            not isinstance(row.get("app"), dict) or row["app"].get("name_slug") != provider
            for row in rows
        ):
            raise unavailable("The connection service returned an invalid response.")
        found.extend(rows)
        if len(found) == total:
            return found
        if len(found) > total or not after or after in cursors:
            raise unavailable("The connection service returned an incomplete response.")
        cursors.add(after)
    raise unavailable("There are too many accounts to check safely. Try a narrower connection.")


def _status(found: list[dict[str, Any]]) -> str:
    if any(
        row.get("healthy") is True and (row.get("dead") is False or row.get("dead") is None)
        for row in found
    ):
        return "connected"
    return "attention" if found else "not_connected"


@router.get("/accounts")
async def accounts(request: Request, session: Session, client: Http) -> dict[str, Any]:
    await _eligible(request, session)
    _query(request, {"provider"})
    selected = request.query_params.get("provider")
    providers = {_provider(selected): selected} if selected is not None else PROVIDERS
    rows = [
        {"provider": key, "name": name, "status": "unavailable"} for key, name in providers.items()
    ]
    settings = load_settings()
    if settings is None:
        return {"account": session.user_id, "configured": False, "providers": rows}
    try:
        async with asyncio.timeout(25):
            scope = "connect:accounts:read"
            if selected is not None:
                scope += " connect:apps:*"
            headers = await _headers(client, settings, scope)
            if selected is not None:
                rows[0]["name"] = (await _app(client, selected, headers))["name"]
            for row in rows:
                row["status"] = _status(
                    await _accounts(client, settings, session, row["provider"], headers)
                )
    except TimeoutError:
        raise unavailable("The connection service timed out. Try again.") from None
    return {"account": session.user_id, "configured": True, "providers": rows}


@router.get("/apps")
async def apps(request: Request, session: Session, client: Http) -> dict[str, Any]:
    await _eligible(request, session)
    _query(request, {"q", "cursor"})
    q, cursor = request.query_params.get("q", ""), request.query_params.get("cursor", "")
    if (
        len(q) > 128
        or (q and not q.isprintable())
        or len(cursor) > 1024
        or (cursor and not cursor.isprintable())
    ):
        raise invalid_arguments("The catalog query is invalid.", "query")
    settings = load_settings()
    if settings is None:
        return {
            "account": session.user_id,
            "configured": False,
            "apps": [],
            "next_cursor": None,
            "total": 0,
        }
    try:
        async with asyncio.timeout(15):
            headers = await _headers(client, settings, "connect:apps:*")
            params = {"limit": str(CATALOG_PAGE_SIZE), "sort_key": "name", "sort_direction": "asc"}
            if q:
                params["q"] = q
            if cursor:
                params["after"] = cursor
            result = await _json(client, "GET", "/connect/apps", headers=headers, params=params)
            rows, next_cursor, total = _page(result, CATALOG_PAGE_SIZE)
            public = [_app_row(row) for row in rows]
    except TimeoutError:
        raise unavailable("The connection service timed out. Try again.") from None
    return {
        "account": session.user_id,
        "configured": True,
        "apps": public,
        "next_cursor": next_cursor,
        "total": total,
    }


@router.post("/{provider}/connect")
async def connect(
    provider: str, request: Request, session: Session, client: Http
) -> dict[str, Any]:
    provider = _provider(provider)
    await _eligible(request, session)
    await _body(request)
    settings = load_settings()
    if settings is None:
        raise unavailable("Connections are not configured yet.")
    try:
        async with asyncio.timeout(20):
            headers = await _headers(client, settings, "connect:tokens:create connect:apps:*")
            await _app(client, provider, headers)
            result = await _json(
                client,
                "POST",
                f"/connect/{settings.project_id}/tokens",
                headers=headers,
                json={
                    "external_user_id": external_user_id(session),
                    "app_id": provider,
                    "allowed_origins": [request.app.state.settings.public_base_url],
                    "expires_in": CONNECT_TTL,
                    "scope": "connect:accounts:read connect:accounts:write connect:apps:*",
                },
            )
            if not isinstance(result, dict) or not isinstance(result.get("expires_at"), str):
                raise unavailable("The connection service returned an invalid response.")
            return {
                "account": session.user_id,
                "provider": provider,
                "connect_url": _hosted_link(result.get("connect_link_url"), provider),
                "expires_at": result["expires_at"],
            }
    except TimeoutError:
        raise unavailable("The connection service timed out. Try again.") from None


@router.post("/{provider}/disconnect")
async def disconnect(
    provider: str, request: Request, session: Session, client: Http
) -> dict[str, Any]:
    provider = _provider(provider)
    await _eligible(request, session)
    await _body(request, confirmation=True)
    settings = load_settings()
    if settings is None:
        raise unavailable("Connections are not configured yet.")
    try:
        async with asyncio.timeout(25):
            headers = await _headers(
                client, settings, "connect:accounts:read connect:accounts:write connect:apps:*"
            )
            await _app(client, provider, headers)
            found = await _accounts(client, settings, session, provider, headers)
            ids = [row.get("id") for row in found]
            if (
                len(ids) > MAX_DISCONNECT_ACCOUNTS
                or any(
                    not isinstance(value, str) or not re.fullmatch(r"apn_[a-zA-Z0-9]{1,120}", value)
                    for value in ids
                )
                or len(set(ids)) != len(ids)
            ):
                raise unavailable("The accounts could not be disconnected safely.")
            for account_id in ids:
                await _json(
                    client,
                    "DELETE",
                    f"/connect/{settings.project_id}/accounts/{account_id}",
                    headers=headers,
                    empty=True,
                )
            if await _accounts(client, settings, session, provider, headers):
                raise unavailable("Some accounts are still connected. Check connections again.")
            return {
                "account": session.user_id,
                "provider": provider,
                "status": "not_connected",
                "disconnected": len(ids),
            }
    except TimeoutError:
        raise unavailable("The connection service timed out. Check connections again.") from None
