"""The internal routes: the runner service and the `brain` CLI (spec lines 3042 and 3204).

No host can hold these credentials. `/v1/internal/runner/*` takes the runner token, which only
the runner service holds; `/v1/internal/brain/*` takes a pass token, which the API mints for one
pass of one world and refuses once that pass's run record has ended. A pass token never names a
world in its requests: every read and write goes to the world the token binds.
"""

import re
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from grove.api.errors import ApiError, invalid_arguments, not_found, unauthenticated, unavailable
from grove.api.views import read_json_body
from grove.runner import brain
from grove.runner.passes import (
    PassBusy,
    PassSettings,
    RunnerAuth,
    caller_for,
    due_work,
    end_pass,
    run_record,
    start_pass,
)
from grove.runner.tokens import (
    AGENTS,
    PassClaims,
    TokenRefused,
    runner_token_matches,
    verify_pass_token,
)
from grove.store.query import WorldQuery
from grove.store.scope import check_world_id
from grove.writer import BatchDeclined, apply_batch, apply_request, finish_request
from grove.writer import runs as run_rows
from grove.writer.receipts import Receipt, render_receipt
from grove.writer.requests import check_request_id

PREFIX = "/v1/internal"
MAX_BATCH_BYTES = 2 * 1024 * 1024
MAX_SMALL_BODY = 16 * 1024
OUTCOMES = frozenset({"ok", "failed", "killed", "timeout", "stopped", "preempted", "budget"})
router = APIRouter(prefix=PREFIX)
_ITEM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def runner_auth(request: Request) -> RunnerAuth:
    auth = getattr(request.app.state, "runner_auth", None)
    if auth is None:
        auth = RunnerAuth.from_environment()
        request.app.state.runner_auth = auth
    return auth


def pass_settings(request: Request) -> PassSettings:
    return getattr(request.app.state, "pass_settings", None) or PassSettings()


def runner_service(request: Request) -> RunnerAuth:
    auth = runner_auth(request)
    if auth.runner_token_sha256 is None or auth.gateway_secret is None:
        raise unavailable("the runner is not configured on this server")
    if not runner_token_matches(_bearer(request), auth.runner_token_sha256):
        raise unauthenticated("a runner token is required")
    return auth


@dataclass(frozen=True)
class LivePass:
    claims: PassClaims
    record: dict[str, Any]

    @property
    def world_id(self) -> str:
        return self.claims.world_id


def live_pass(request: Request) -> LivePass:
    auth = runner_auth(request)
    if auth.gateway_secret is None:
        raise unavailable("the runner is not configured on this server")
    try:
        claims = verify_pass_token(auth.gateway_secret, _bearer(request))
    except TokenRefused:
        raise unauthenticated("a live pass token is required") from None
    record = run_record(request.app.state.driver, claims.world_id, claims.run_id)
    if (
        record is None
        or record.get("outcome") != run_rows.RUNNING
        or record.get("agent") != claims.agent
    ):
        raise unauthenticated("this pass has ended")
    return LivePass(claims, record)


Service = Annotated[RunnerAuth, Depends(runner_service)]
Pass = Annotated[LivePass, Depends(live_pass)]


def _world_field(body: Any) -> str:
    if not isinstance(body, dict):
        raise invalid_arguments("the body must be a JSON object", "body")
    try:
        return check_world_id(body.get("world_id"))
    except Exception:
        raise invalid_arguments("world_id is not a world id", "world_id") from None


# The runner service ---------------------------------------------------------------------------


@router.get("/runner/due")
def runner_due(request: Request, _: Service) -> dict[str, Any]:
    return {"due": due_work(request.app.state.driver, settings=pass_settings(request))}


@router.post("/runner/passes")
async def runner_start(request: Request, auth: Service) -> JSONResponse:
    body = await read_json_body(request, MAX_SMALL_BODY)
    world_id = _world_field(body)
    agent = body.get("agent")
    if agent is not None and agent not in AGENTS:
        raise invalid_arguments(f"agent must be one of {sorted(AGENTS)}", "agent")
    settings = pass_settings(request)

    def start() -> Any:
        try:
            return start_pass(
                request.app.state.driver, auth, world_id, agent=agent, settings=settings
            )
        except PassBusy:
            return "busy"

    grant = await run_in_threadpool(start)
    if grant == "busy":
        return JSONResponse({"grant": None, "reason": "busy"})
    if grant is None:
        return JSONResponse({"grant": None, "reason": "nothing_due"})
    return JSONResponse({"grant": grant.to_api()}, status_code=201)


@router.post("/runner/passes/{run_id}/end")
async def runner_end(run_id: str, request: Request, _: Service) -> dict[str, Any]:
    body = await read_json_body(request, MAX_SMALL_BODY)
    world_id = _world_field(body)
    outcome = body.get("outcome")
    if outcome not in OUTCOMES:
        raise invalid_arguments(f"outcome must be one of {sorted(OUTCOMES)}", "outcome")
    summary = body.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise invalid_arguments("summary is text", "summary")
    ended = await run_in_threadpool(
        end_pass, request.app.state.driver, world_id, run_id, outcome=outcome, summary=summary
    )
    if ended is None:
        raise not_found("no such run in this world")
    return {"run_id": run_id, "outcome": ended.get("outcome")}


# The brain CLI --------------------------------------------------------------------------------


@router.get("/brain/status")
def brain_status(request: Request, live: Pass) -> dict[str, Any]:
    return brain.status(request.app.state.driver, live.world_id, live.record)


@router.get("/brain/pass")
def brain_pass(request: Request, live: Pass) -> dict[str, Any]:
    return brain.pass_input(request.app.state.driver, live.world_id, live.record)


@router.post("/brain/recall")
async def brain_recall(request: Request, live: Pass) -> dict[str, Any]:
    """The text comes in the body: a URL, and so an access log line, never holds it."""
    body = await read_json_body(request, MAX_SMALL_BODY)
    if not isinstance(body, dict):
        raise invalid_arguments("the body is {q, limit?}", "body")
    q, limit = body.get("q"), body.get("limit", 10)
    if not isinstance(q, str) or not q.strip() or len(q) > 500:
        raise invalid_arguments("q is 1 to 500 characters of text to look up", "q")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= brain.RECALL_LIMIT
    ):
        raise invalid_arguments(f"limit is 1 to {brain.RECALL_LIMIT}", "limit")
    return await run_in_threadpool(
        brain.recall, request.app.state.driver, live.world_id, q, limit=limit
    )


@router.get("/brain/items/{item_id}")
def brain_get(item_id: str, request: Request, live: Pass) -> dict[str, Any]:
    if _ITEM_ID.fullmatch(item_id) is None:
        raise not_found("no item with that id in this world")
    found = brain.get_item(request.app.state.driver, live.world_id, item_id)
    if found is None:
        raise not_found("no item with that id in this world")
    return found


def _own_request(live: LivePass, request_id: Any) -> str:
    try:
        check_request_id(request_id)
    except ValueError:
        raise invalid_arguments("request_id is not a request id", "request_id") from None
    if request_id not in (live.record.get("input_ids") or []):
        raise not_found("that request is not an input of this pass")
    return request_id


def _still_held(driver: Any, live: LivePass, request_id: str) -> None:
    """The pass still holds the request: it is `applying` under this run, or this run's batch was
    declined and the writer's one repair is still open. A lease that passed to another pass
    (another `claimed_by`) or a request already saved is no longer this pass's."""
    rows = WorldQuery(driver, live.world_id).read(
        "MATCH (r:Request {id: $id}) RETURN r.status AS status, r.claimed_by AS worker, "
        "r.run_id AS run, r.repairs AS repairs",
        id=request_id,
    )
    row = rows[0] if rows else {}
    run_id = live.claims.run_id
    held = row.get("status") == "applying" and row.get("worker") == run_id
    repair = row.get("status") == "declined" and row.get("run") == run_id and not row.get("repairs")
    if not (held or repair):
        raise ApiError(
            409,
            "invalid_arguments",
            "this pass no longer holds that request",
            details={"rule": "lease_lost"},
        )


def _declined(error: BatchDeclined) -> JSONResponse:
    refusals = [
        {"op_id": r.op_id, "op": r.op, "code": r.code, "rule": r.rule, "message": r.message}
        for r in error.refusals
    ]
    return JSONResponse(
        {
            "state": "declined",
            "request_id": error.request_id,
            "retryable": error.retryable,
            "refusals": refusals,
        },
        status_code=422,
    )


@router.post("/brain/propose")
async def brain_propose(request: Request, live: Pass) -> JSONResponse:
    body = await read_json_body(request, MAX_BATCH_BYTES)
    if not isinstance(body, dict) or not isinstance(body.get("batch"), dict):
        raise invalid_arguments("the body is {request_id?, batch}", "batch")
    driver = request.app.state.driver
    caller = caller_for(live.claims.agent)
    run_id = live.claims.run_id
    batch = body["batch"]
    request_id = body.get("request_id")
    if live.claims.agent == "learner" and request_id is None:
        raise invalid_arguments("a Learner pass proposes under one of its requests", "request_id")

    def propose() -> JSONResponse:
        if request_id is not None:
            own = _own_request(live, request_id)
            _still_held(driver, live, own)
            try:
                state = apply_request(driver, live.world_id, caller, own, batch, run_id=run_id)
            except BatchDeclined as error:
                run_rows.note_proposal(
                    driver,
                    live.world_id,
                    run_id,
                    batch=batch,
                    request_id=own,
                    state="declined",
                    decline_op_ids=[r.op_id for r in error.refusals],
                )
                return _declined(error)
            answer = state.to_api()
            declined_ops = [d.get("op_id") for d in answer.get("declines") or []]
            run_rows.note_proposal(
                driver,
                live.world_id,
                run_id,
                batch=batch,
                request_id=own,
                state=answer["state"],
                decline_op_ids=[op for op in declined_ops if op],
            )
            if state.receipt is not None and answer["state"] == "saved":
                answer["receipt_text"] = _receipt_block(driver, live.world_id, state.receipt)
            return JSONResponse(answer, status_code=200 if answer["state"] != "declined" else 422)
        try:
            receipt = apply_batch(driver, live.world_id, caller, batch, run_id=run_id)
        except BatchDeclined as error:
            run_rows.note_proposal(
                driver,
                live.world_id,
                run_id,
                batch=batch,
                request_id=error.request_id,
                state="declined",
                decline_op_ids=[r.op_id for r in error.refusals],
            )
            return _declined(error)
        run_rows.note_proposal(
            driver,
            live.world_id,
            run_id,
            batch=batch,
            request_id=receipt.request_id,
            state="saved",
            decline_op_ids=[],
        )
        return JSONResponse(
            {
                "state": "saved",
                "request_id": receipt.request_id,
                "receipt": receipt.to_json(),
                "receipt_text": _receipt_block(driver, live.world_id, receipt),
            }
        )

    return await run_in_threadpool(propose)


def _receipt_block(driver: Any, world_id: str, receipt: Receipt) -> str:
    from grove.prompts import DataBlock, render_blocks

    return render_blocks(
        [DataBlock("receipt", "automated", None, None, render_receipt(driver, world_id, receipt))]
    )


@router.post("/brain/finish")
async def brain_finish(request: Request, live: Pass) -> dict[str, Any]:
    """A request this pass read and found nothing to write for: saved with no commit."""
    body = await read_json_body(request, MAX_SMALL_BODY)
    if not isinstance(body, dict):
        raise invalid_arguments("the body is {request_id}", "body")
    own = _own_request(live, body.get("request_id"))
    caller = caller_for(live.claims.agent)

    def finish() -> dict[str, Any]:
        try:
            state = finish_request(
                request.app.state.driver, live.world_id, caller, own, worker=live.claims.run_id
            )
        except BatchDeclined as error:
            raise ApiError(409, "invalid_arguments", error.refusals[0].message) from None
        return state.to_api()

    return await run_in_threadpool(finish)
