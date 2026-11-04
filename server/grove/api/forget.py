"""Authenticated D14 operations. A host never grants its own person approval."""

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.errors import ApiError, invalid_arguments, unauthenticated
from grove.api.mcp import bearer_token
from grove.api.session import current_session
from grove.api.views import read_json_body
from grove.oauth.secrets import token_hash
from grove.oauth.store import find_access_token
from grove.privacy import flow_statement
from grove.store.replay import ReplayRefused
from grove.writer.errors import HTTP_STATUS, InvalidRecord
from grove.writer.forget_approvals import mint_approval, validate_target
from grove.writer.forget_ledger import LedgerUnavailable

PREFIX = "/v1/forget"
MAX_BODY_BYTES = 16 * 1024
LEDGER_ENV = "GROVE_FORGET_LEDGER_DIR"
router = APIRouter(prefix=PREFIX)
public_router = APIRouter()


@public_router.get("/v1/privacy")
def privacy_statement(request: Request) -> dict[str, Any]:
    return flow_statement(
        runtime=True, google_configured=bool(request.app.state.settings.google_oauth_file)
    )


def _origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if (origin is not None and origin != request.app.state.settings.public_base_url) or (
        request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise ApiError(403, "forbidden_origin", "the request is from another site")


def _ledger_dir() -> Path:
    raw = os.environ.get(LEDGER_ENV)
    if not raw:
        raise ApiError(503, "unavailable", "forget is not configured", retryable=False)
    return Path(raw)


def _safe_error(error: InvalidRecord) -> ApiError:
    code = error.code if error.code in HTTP_STATUS else "invalid_arguments"
    message = (
        "a new approval from the person is required"
        if code == "approval_required"
        else "the forget request was refused"
    )
    return ApiError(HTTP_STATUS[code], code, message)


async def _actor(request: Request) -> tuple[str, str]:
    """A present Authorization header never falls back to the browser cookie."""
    header = request.headers.get("authorization")
    if header is None:
        session = await run_in_threadpool(current_session, request)
        return session.world_id, session.user_id
    token = bearer_token(header)
    row = None
    if token:
        row = await run_in_threadpool(
            find_access_token, request.app.state.driver, token_hash(token), clock.now()
        )
    if row is None or row.resource != request.app.state.settings.mcp_url:
        raise unauthenticated("the access token is not valid")
    if "grove.sensitive_write" not in row.scopes:
        raise ApiError(403, "scope_denied", "forget requires the sensitive-write scope")
    return row.world_id, row.user_id


@router.post("/approve")
async def approve(request: Request) -> dict[str, Any]:
    # A host cannot acquire an approval even if a browser cookie accompanies its token.
    if request.headers.get("authorization") is not None:
        raise ApiError(403, "approval_required", "only the signed-in person can approve forget")
    session = await run_in_threadpool(current_session, request)
    _origin(request)
    _ledger_dir()
    body = await read_json_body(request, MAX_BODY_BYTES)
    if (
        not isinstance(body, dict)
        or set(body) != {"target", "confirm"}
        or body["confirm"] is not True
    ):
        raise invalid_arguments("the person must confirm this target", "confirm")
    try:
        target = validate_target(body["target"])
        approval = await run_in_threadpool(
            mint_approval, request.app.state.driver, session.world_id, session.user_id, target
        )
    except InvalidRecord as error:
        raise _safe_error(error) from None
    return {
        "approval_token": approval.token,
        "expires_at": approval.expires_at.isoformat(),
        "one_use": True,
    }


def _execute(driver, world_id, target, *, approval_token, actor, ledger_dir):
    # The cascade module owns the operation and consumes approval in its transaction.
    from grove.writer.forget import forget

    return forget(
        driver, world_id, target, approval_token=approval_token, actor=actor, ledger_dir=ledger_dir
    )


@router.post("")
async def forget(request: Request) -> dict[str, Any]:
    world_id, actor = await _actor(request)
    _origin(request)
    ledger_dir = _ledger_dir()
    body = await read_json_body(request, MAX_BODY_BYTES)
    if not isinstance(body, dict) or set(body) != {"target", "approval_token"}:
        raise invalid_arguments("the body needs a target and an approval token", "body")
    if not isinstance(body["approval_token"], str) or len(body["approval_token"]) > 512:
        raise ApiError(403, "approval_required", "a new approval from the person is required")
    try:
        target = validate_target(body["target"])
        return await run_in_threadpool(
            _execute,
            request.app.state.driver,
            world_id,
            target,
            approval_token=body["approval_token"],
            actor=actor,
            ledger_dir=ledger_dir,
        )
    except InvalidRecord as error:
        raise _safe_error(error) from None
    except LedgerUnavailable:
        raise ApiError(503, "unavailable", "forget is not available", retryable=False) from None
    except ReplayRefused as error:
        if error.rule == "row_value":
            raise invalid_arguments("select one content field for this value", "target") from None
        raise ApiError(409, "conflict", "the forget request was refused", retryable=False) from None
