"""Delete my Grove: POST /v1/world/delete. docs/forget-api.md has the contract.

Only the signed-in person can do this, and only for their own world: a host cannot, and the world
comes from the session, never from the request. grove.writer.world_delete does the removing.
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from grove.api import pipedream
from grove.api.auth import _STORE_FAILURES, _set_session_cookie, _sign_in
from grove.api.errors import ApiError, invalid_arguments, store_unavailable, unavailable
from grove.api.forget import _origin
from grove.api.session import current_session
from grove.api.views import read_json_body
from grove.views.session import ViewSession
from grove.writer.accounts import METHODS
from grove.writer.forget_ledger import LedgerUnavailable
from grove.writer.world_delete import (
    ErasureRefused,
    FilesUnavailable,
    begin_erasure,
    erase_world_rows,
    remove_world_files,
)

PREFIX = "/v1/world"
MAX_BODY_BYTES = 1024
CONFIRMATION = "DELETE"
router = APIRouter(prefix=PREFIX)


async def _remove_accounts(client: httpx.AsyncClient, session: ViewSession) -> None:
    try:
        await pipedream.remove_all_accounts(client, session)
    except ApiError:
        raise ApiError(
            502, "unavailable", "Your connected apps could not be removed.", retryable=True
        ) from None


@router.post("/delete")
async def delete_world(request: Request, client: pipedream.Http) -> JSONResponse:
    # A host cannot do this even if a browser cookie accompanies its token.
    if request.headers.get("authorization") is not None:
        raise ApiError(403, "scope_denied", "only the signed-in person can delete a Grove")
    session = await run_in_threadpool(current_session, request)
    _origin(request)
    if request.query_params:
        raise invalid_arguments("query fields are not accepted", "query")
    body = await read_json_body(request, MAX_BODY_BYTES)
    if not isinstance(body, dict) or set(body) != {"confirm"} or body["confirm"] != CONFIRMATION:
        raise invalid_arguments("type DELETE to confirm", "confirm")
    driver = request.app.state.driver
    world_id, user_id = session.world_id, session.user_id
    try:
        hashed = await run_in_threadpool(begin_erasure, driver, world_id, user_id)
        await _remove_accounts(client, session)
        await run_in_threadpool(remove_world_files, world_id)
        clear_after = await run_in_threadpool(erase_world_rows, driver, world_id, user_id)
        # Sessions are gone by now. With no answer here the next sign-in makes the same new world.
        method = session.method if session.method in METHODS else "email"
        token = await run_in_threadpool(_sign_in, request, hashed, method=method)
    except ErasureRefused:
        raise ApiError(403, "scope_denied", "This account cannot delete its Grove.") from None
    except LedgerUnavailable:
        raise ApiError(503, "unavailable", "Delete is not available.", retryable=False) from None
    except FilesUnavailable:
        raise unavailable("Your files could not be removed. Try again.") from None
    except _STORE_FAILURES:
        raise store_unavailable() from None
    response = JSONResponse(
        {"deleted": True, "backups_clear_after": clear_after, "signed_in": True}
    )
    _set_session_cookie(response, token, request.app.state.settings)
    return response
