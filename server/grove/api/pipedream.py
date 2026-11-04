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


async def _json(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
    """Never expose provider bodies, URLs, tokens or transport exception text in errors."""
    try:
        async with client.stream(method, path, **kwargs) as response:
            if response.status_code == 429:
                raise unavailable("Connections are busy. Try again shortly.")
            if response.status_code in (401, 403):
                raise unavailable(
                    "Connections are unavailable. The server credentials need attention."
                )
            if response.status_code < 200 or response.status_code >= 300:
                raise unavailable("Connection status could not be checked. Try again.")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise unavailable("The connection service returned an invalid response.")
            return json.loads(data)
    except (httpx.HTTPError, ValueError):
        raise unavailable("The connection service could not be reached. Try again.") from None


async def _headers(client: httpx.AsyncClient, settings: Settings) -> dict[str, str]:
    result = await _json(
        client,
        "POST",
        "/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "scope": "connect:tokens:create connect:accounts:read",
        },
    )
    token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token or len(token) > 8192 or not token.isprintable():
        raise unavailable("The connection service returned an invalid response.")
    return {"Authorization": "Bearer " + token, "x-pd-environment": settings.environment}


def _provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise invalid_arguments("Choose Gmail, Google Calendar or Notion.", "provider")
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


@router.get("/accounts")
async def accounts(request: Request, session: Session, client: Http) -> dict[str, Any]:
    if await run_in_threadpool(is_demo_world, request.app.state.driver, session.world_id):
        raise ApiError(403, "scope_denied", "This account cannot connect external apps.")
    settings = load_settings()
    rows = [
        {"provider": key, "name": name, "status": "unavailable"} for key, name in PROVIDERS.items()
    ]
    if settings is None:
        return {"account": session.user_id, "configured": False, "providers": rows}
    try:
        async with asyncio.timeout(25):
            headers = await _headers(client, settings)
            path = f"/connect/{settings.project_id}/users/{external_user_id(session)}/accounts"
            for row in rows:
                found = await _json(
                    client,
                    "GET",
                    path,
                    headers=headers,
                    params={"app": row["provider"], "include_credentials": "false"},
                )
                if not isinstance(found, list) or len(found) > 100:
                    raise unavailable("The connection service returned an invalid response.")
                # The path is owner-scoped; only the requested app can affect its status.
                if any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("app"), dict)
                    or item["app"].get("name_slug") != row["provider"]
                    for item in found
                ):
                    raise unavailable("The connection service returned an invalid response.")
                row["status"] = "not_connected" if not found else "attention"
                if any(
                    item.get("healthy") is True and item.get("dead") in (False, None)
                    for item in found
                ):
                    row["status"] = "connected"
    except TimeoutError:
        raise unavailable("The connection service timed out. Try again.") from None
    return {"account": session.user_id, "configured": True, "providers": rows}


@router.post("/{provider}/connect")
async def connect(
    provider: str, request: Request, session: Session, client: Http
) -> dict[str, Any]:
    provider = _provider(provider)
    # Designation is persisted and can only be created with a fresh account. Its absence
    # cannot turn into designation during this request through an ordinary account update.
    if await run_in_threadpool(is_demo_world, request.app.state.driver, session.world_id):
        raise ApiError(403, "scope_denied", "This account cannot connect external apps.")
    base = request.app.state.settings.public_base_url
    if request.headers.get("origin", base) != base:
        raise ApiError(403, "forbidden", "Open connections from Grove.")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise invalid_arguments("Use a JSON request.", "body")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024:
            raise invalid_arguments("The connection request is too large.", "body")
    try:
        empty = json.loads(body) == {}
    except ValueError:
        empty = False
    if not empty or request.query_params:
        raise invalid_arguments("The connection request accepts no identity or options.", "body")
    settings = load_settings()
    if settings is None:
        raise unavailable("Connections are not configured yet.")
    try:
        async with asyncio.timeout(20):
            headers = await _headers(client, settings)
            result = await _json(
                client,
                "POST",
                f"/connect/{settings.project_id}/tokens",
                headers=headers,
                json={
                    "external_user_id": external_user_id(session),
                    "app_id": provider,
                    "allowed_origins": [base],
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
