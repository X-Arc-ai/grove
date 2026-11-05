"""The source endpoints under /v1/sources/: the upload of an export, and the connectors' coverage.

POST /v1/sources/upload takes the file as the raw body, never multipart. It accepts the session
cookie, or an upload-only token (`Authorization: Bearer gut_...`) that serves this route alone and
one world. A cross-site form cannot send the accepted body types without a preflight, and the
cookie is SameSite=Lax, so a page elsewhere cannot upload with the person's cookie.

No answer of this module holds message text, a file name, a token or a value: ids, counts and
codes only. docs/ingest.md has the contract.
"""

import os
import tempfile
import threading
from datetime import datetime
from typing import IO, Annotated, Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

import grove.ingest  # noqa: F401  Registers the parsers, so providers() lists them.
from grove import clock
from grove.api.errors import (
    INVALID_ARGUMENTS,
    RATE_LIMITED,
    UNAVAILABLE,
    ApiError,
    invalid_arguments,
    unauthenticated,
)
from grove.api.session import current_session
from grove.api.views import check_json_type, read_json_body
from grove.config import ConfigError
from grove.demo_policy import is_demo_world
from grove.ingest.connector import NotAnExport, Upload, providers
from grove.ingest.take_in import take_in
from grove.ingest.tokens import find_upload_grant
from grove.ingest.upload_policy import screener_for_upload
from grove.screening import Screener
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.onboarding import (
    InvalidPendingExports,
    PendingExportDenied,
    PendingExportSessionEnded,
    capture_pending_export,
    pending_exports,
    require_provider,
)
from grove.views.session import SESSION_COOKIE, ViewSession, token_hash
from grove.writer.onboarding import complete_export, request_export

PREFIX = "/v1/sources"

UPLOAD_TYPES = frozenset(
    {
        "application/zip",
        "application/json",
        "application/x-ndjson",
        "application/jsonl",
        "application/zstd",
        "application/octet-stream",
    }
)
MAX_BYTES_VAR = "GROVE_UPLOAD_MAX_BYTES"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
# Past this size the spool moves to a temporary file, so a large export does not sit in memory.
SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
MAX_PARALLEL_UPLOADS = 1
RETRY_AFTER_SECONDS = 30

# Parsed exports and screening need more memory than their wire size (docs/ingest.md).
# Keep one upload at a time inside the api's 4 GiB limit, which includes its tmpfs.
UPLOAD_SLOTS = threading.BoundedSemaphore(MAX_PARALLEL_UPLOADS)

# What the page can highlight on the map. The counts are never capped.
MAX_CONTRIBUTION_ITEM_IDS = 500

# One read for all connectors. Evidence names its Source by id, so the targets come from the
# world's sources (docs/store-map.md). Each locator test lets a composite index serve the read.
_CONNECTORS = (
    "MATCH (c:Connector) "
    "WITH c, COLLECT { MATCH (s:Source {connector_id: c.id}) "
    "WHERE s.external_locator IS NOT NULL AND coalesce(s.forget_state, 'kept') <> 'forgotten' "
    "RETURN s.id } AS source_ids "
    "WITH c, source_ids, COLLECT { MATCH (e:Evidence) "
    "WHERE e.source_id IN source_ids AND e.locator IS NOT NULL AND e.active = true "
    "RETURN DISTINCT e.target_id } AS targets "
    "RETURN c.id AS connector_id, c.type AS type, c.provider AS provider, "
    "c.last_attempted_read AS last_attempted_read, "
    "c.last_successful_read AS last_successful_read, c.last_outcome AS last_outcome, "
    "c.read_count AS read_count, c.failed_count AS failed_count, "
    "c.oldest_unprocessed_item AS oldest_unprocessed_item, c.watermark AS watermark, "
    "size(source_ids) AS sources, "
    "COUNT { MATCH (i:Item) WHERE i.id IN targets AND i.end_class IS NULL } AS nodes, "
    "COUNT { MATCH (:Item)-[l:LINK]->(:Item) "
    "WHERE l.id IN targets AND l.active_key IS NOT NULL } AS edges, "
    "COLLECT { MATCH (i:Item) WHERE i.id IN targets AND i.end_class IS NULL "
    "RETURN i.id AS id ORDER BY id LIMIT $item_cap } AS item_ids "
    "ORDER BY c.type, c.provider, c.id"
)

router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]


def _reminder_error(error: Exception) -> ApiError:
    if isinstance(error, PendingExportSessionEnded):
        return unauthenticated()
    if isinstance(error, PendingExportDenied):
        return ApiError(403, "scope_denied", "export reminders are unavailable for this account")
    return _not_configured("export reminders are unavailable")


@router.get("/pending-exports")
def get_pending_exports(request: Request, session: Session) -> dict:
    if "authorization" in request.headers:
        raise unauthenticated("export reminders require a session")
    if request.query_params:
        raise invalid_arguments("query fields are not accepted", "query")
    try:
        return pending_exports(request.app.state.driver, session)
    except (InvalidPendingExports, PendingExportDenied) as error:
        raise _reminder_error(error) from None


@router.post("/pending-exports/{provider}")
async def post_pending_export(request: Request, session: Session, provider: str) -> dict:
    if "authorization" in request.headers:
        raise unauthenticated("export reminders require a session")
    origin = request.headers.get("origin")
    if (origin and origin != request.app.state.settings.public_base_url) or request.headers.get(
        "sec-fetch-site"
    ) == "cross-site":
        raise ApiError(403, "forbidden", "request origin is not allowed")
    if request.query_params:
        raise invalid_arguments("query fields are not accepted", "query")
    try:
        require_provider(provider)
    except InvalidPendingExports:
        raise invalid_arguments("unknown export provider", "provider") from None
    check_json_type(request)
    body = await read_json_body(request, 1024)
    if not isinstance(body, dict) or body:
        raise invalid_arguments("an empty object is required", "body")
    try:
        return await run_in_threadpool(
            request_export,
            request.app.state.driver,
            session,
            token_hash(request.cookies[SESSION_COOKIE]),
            provider,
        )
    except (InvalidPendingExports, PendingExportDenied, PendingExportSessionEnded) as error:
        raise _reminder_error(error) from None


async def _capture_reminder(request: Request, world_id: str, provider: str):
    try:
        return await run_in_threadpool(
            capture_pending_export, request.app.state.driver, world_id, provider
        )
    except (InvalidPendingExports, PendingExportDenied) as error:
        raise _reminder_error(error) from None


async def _finish_upload(request: Request, result, captured) -> dict:
    body = result.to_api()
    if (
        captured is None
        or body.get("state") not in ("taken_in", "nothing_new")
        or type(body.get("failed_chats")) is not int
        or body["failed_chats"] != 0
    ):
        return body
    try:
        state = await run_in_threadpool(complete_export, request.app.state.driver, captured)
    except Exception:
        # The source transaction already committed. Report its real result and retain the
        # reminder; never turn a metadata failure into a false source import failure.
        body["reminder"] = {"status": "unavailable"}
    else:
        body["reminder"] = {"status": "updated"}
        # Upload-only tokens must not acquire access to the account's reminder list.
        if "authorization" not in request.headers:
            body["reminder"]["snapshot"] = state
    return body


def upload_cap() -> int:
    """The largest body this process takes, from GROVE_UPLOAD_MAX_BYTES."""
    raw = os.environ.get(MAX_BYTES_VAR)
    if raw is None:
        return DEFAULT_MAX_BYTES
    text = raw.strip()
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise ConfigError(f"{MAX_BYTES_VAR} must be a positive whole number of bytes")
    return int(text)


def _not_configured(message: str) -> ApiError:
    # Not retryable: the same request fails the same way until an operator fixes the box.
    return ApiError(503, UNAVAILABLE, message, retryable=False)


def _busy() -> ApiError:
    return ApiError(
        429,
        RATE_LIMITED,
        "another upload is running: try again soon",
        details={"retry_after_seconds": RETRY_AFTER_SECONDS},
        retryable=True,
    )


async def _upload_world(request: Request) -> str:
    """The world this upload goes to. A present Authorization header decides alone: a bad token
    never falls back to the cookie, so a caller always knows which credential was used."""
    header = request.headers.get("authorization")
    if header is None:
        session = await run_in_threadpool(current_session, request)
        return session.world_id
    scheme, _, token = header.strip().partition(" ")
    grant = None
    if scheme.lower() == "bearer":
        platform = PlatformQuery(request.app.state.driver)
        grant = await run_in_threadpool(find_upload_grant, platform, token.strip(), clock.now())
    if grant is None:
        raise unauthenticated("the upload token is not valid")
    return grant.world_id


def _check_upload_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in UPLOAD_TYPES:
        raise invalid_arguments(
            "the body must be the file itself, sent as one of the accepted types", "content-type"
        )


def _provider(request: Request) -> str:
    provider = request.query_params.get("provider", "")
    known = providers()
    if provider not in known:
        raise ApiError(
            400,
            INVALID_ARGUMENTS,
            "provider must be one of the valid codes",
            details={"field": "provider", "valid": list(known)},
        )
    return provider


def _screener(world_id: str) -> Screener:
    try:
        result = screener_for_upload(world_id)
        if not isinstance(result, Screener):
            raise TypeError("upload policy must provide a Screener")
        return result
    except (ConfigError, TypeError, ValueError):
        # Never expose policy values or a malformed internal World id in the answer.
        raise _not_configured("screening is not configured") from None


def _cap() -> int:
    try:
        return upload_cap()
    except ConfigError:
        raise _not_configured("uploads are not configured") from None


def _check_declared_length(request: Request, cap: int) -> None:
    declared = request.headers.get("content-length", "").strip()
    if declared.isascii() and declared.isdigit() and int(declared) > cap:
        raise invalid_arguments("the body is too large", "body")


async def _spool_body(request: Request, cap: int) -> tuple[IO[bytes], int]:
    """The body in a spool that moves to disk past SPOOL_MEMORY_BYTES. The cap counts the bytes
    that arrive, because a chunked body declares no length."""
    spool = tempfile.SpooledTemporaryFile(max_size=min(SPOOL_MEMORY_BYTES, cap))
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > cap:
                raise invalid_arguments("the body is too large", "body")
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool, size


@router.post("/upload")
async def post_upload(request: Request) -> dict[str, Any]:
    """Parse, screen and store one export. Nothing is read from the body until the caller, the
    type, the provider and the screening settings have passed."""
    world_id = await _upload_world(request)
    if await run_in_threadpool(is_demo_world, request.app.state.driver, world_id):
        raise ApiError(403, "scope_denied", "source imports are unavailable for this demo")
    _check_upload_type(request)
    provider = _provider(request)
    screener = await run_in_threadpool(_screener, world_id)
    cap = _cap()
    _check_declared_length(request, cap)
    if not UPLOAD_SLOTS.acquire(blocking=False):
        raise _busy()
    spool: IO[bytes] | None = None
    try:
        captured = await _capture_reminder(request, world_id, provider)
        spool, size = await _spool_body(request, cap)
        if size == 0:
            raise invalid_arguments("the body is empty", "body")
        try:
            result = await run_in_threadpool(
                take_in,
                request.app.state.driver,
                world_id,
                provider,
                Upload(spool, size),
                screener=screener,
            )
        except NotAnExport as refused:
            # A parser's message names the format it expected, never a value from the file.
            raise invalid_arguments(str(refused), "file") from None
    finally:
        if spool is not None:
            spool.close()
        UPLOAD_SLOTS.release()
    return await _finish_upload(request, result, captured)


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def _connector(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": row["connector_id"],
        "type": row["type"],
        "provider": row["provider"],
        "last_attempted_read": _iso(row["last_attempted_read"]),
        "last_successful_read": _iso(row["last_successful_read"]),
        "last_outcome": row["last_outcome"],
        "read_count": row["read_count"] or 0,
        "failed_count": row["failed_count"] or 0,
        "oldest_unprocessed_item": _iso(row["oldest_unprocessed_item"]),
        "watermark": row["watermark"],
        "contribution": {
            "sources": row["sources"],
            "nodes": row["nodes"],
            "edges": row["edges"],
            "item_ids": row["item_ids"],
        },
    }


@router.get("/connectors")
def get_connectors(request: Request, session: Session) -> dict[str, Any]:
    """The session's world only. An upload token is no session, so it gets a 401 here."""
    rows = WorldQuery(request.app.state.driver, session.world_id).read(
        _CONNECTORS, item_cap=MAX_CONTRIBUTION_ITEM_IDS
    )
    return {"connectors": [_connector(row) for row in rows]}
