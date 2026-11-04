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
from grove.config import ConfigError
from grove.demo.screening import lumen_fixture, new_demo_screener
from grove.demo_policy import is_demo_world
from grove.ingest.connector import NotAnExport, Upload, providers
from grove.ingest.take_in import take_in
from grove.ingest.tokens import find_upload_grant
from grove.screening import Screener, ScreeningConfig
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.session import ViewSession

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

_CONNECTORS = (
    "MATCH (c:Connector) "
    "RETURN c.id AS connector_id, c.type AS type, c.provider AS provider, "
    "c.last_attempted_read AS last_attempted_read, "
    "c.last_successful_read AS last_successful_read, c.last_outcome AS last_outcome, "
    "c.read_count AS read_count, c.failed_count AS failed_count, "
    "c.oldest_unprocessed_item AS oldest_unprocessed_item, c.watermark AS watermark "
    "ORDER BY c.type, c.provider, c.id"
)

router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]


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


def _screener() -> Screener:
    try:
        return Screener(ScreeningConfig.from_environment())
    except ConfigError:
        # The ConfigError text names only the variable, but the answer stays generic anyway.
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
    try:
        screener = await run_in_threadpool(_screener)
    except ApiError as missing_policy:
        return await _invented_upload(request, world_id, provider, missing_policy)
    cap = _cap()
    _check_declared_length(request, cap)
    if not UPLOAD_SLOTS.acquire(blocking=False):
        raise _busy()
    spool: IO[bytes] | None = None
    try:
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
    return result.to_api()


async def _invented_upload(
    request: Request, world_id: str, provider: str, missing_policy: ApiError
) -> dict[str, Any]:
    """The sole fixed-byte exception when ordinary screening is unavailable.

    The route already resolved a real owner session and refused designated Cedar worlds.
    An upload token, another provider, or any different bytes retain the ordinary refusal.
    """
    if (
        "authorization" in request.headers
        or provider != "claude_ai"
        or request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json"
        or "content-encoding" in request.headers
    ):
        raise missing_policy
    fixture = lumen_fixture()
    if not UPLOAD_SLOTS.acquire(blocking=False):
        raise _busy()
    spool = None
    try:
        try:
            _check_declared_length(request, len(fixture))
            spool, size = await _spool_body(request, len(fixture))
        except ApiError:
            raise missing_policy from None
        if size != len(fixture) or spool.read() != fixture:
            raise missing_policy
        spool.seek(0)
        result = await run_in_threadpool(
            take_in,
            request.app.state.driver,
            world_id,
            provider,
            Upload(spool, size),
            screener=new_demo_screener(),
        )
        return result.to_api()
    finally:
        if spool is not None:
            spool.close()
        UPLOAD_SLOTS.release()


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
    }


@router.get("/connectors")
def get_connectors(request: Request, session: Session) -> dict[str, Any]:
    """The session's world only. An upload token is no session, so it gets a 401 here."""
    rows = WorldQuery(request.app.state.driver, session.world_id).read(_CONNECTORS)
    return {"connectors": [_connector(row) for row in rows]}
