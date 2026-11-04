"""The view endpoints under /v1/views/: session-only, the simulation's shapes. docs/views.md."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.errors import invalid_arguments, not_found
from grove.api.session import current_session
from grove.store.query import WorldQuery
from grove.views.graph import (
    Window,
    graph_payload,
    health_payload,
    node_payload,
    schema_payload,
    session_payload,
)
from grove.views.session import ViewSession
from grove.views.times import InvalidInstant, parse_instant
from grove.views.workers import worker_activity_payload
from grove.writer.positions import InvalidPositions, place_items

PREFIX = "/v1/views"
JSON_TYPE = "application/json"
MAX_BODY_BYTES = 2 * 1024 * 1024

router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]


def _queries(request: Request, session: ViewSession) -> tuple[WorldQuery, WorldQuery]:
    driver = request.app.state.driver
    return WorldQuery(driver, session.world_id), WorldQuery.primitives(driver)


def _window(request: Request) -> Window:
    params = request.query_params
    try:
        return Window(
            as_of=parse_instant(params.get("as_of"), "as_of"),
            known_as_of=parse_instant(params.get("known_as_of"), "known_as_of"),
        )
    except InvalidInstant as refused:
        raise invalid_arguments(str(refused), refused.field) from None


@router.get("/session")
def get_session(request: Request, session: Session) -> dict[str, Any]:
    world, _ = _queries(request, session)
    return session_payload(world, session)


@router.get("/graph")
def get_graph(request: Request, session: Session) -> dict[str, Any]:
    window = _window(request)
    world, primitives = _queries(request, session)
    return graph_payload(
        world,
        primitives,
        window=window,
        self_item_id=session.self_item_id,
        now=clock.now(session.world_id),
    )


@router.get("/node/{item_id}")
def get_node(request: Request, session: Session, item_id: str) -> dict[str, Any]:
    window = _window(request)
    world, primitives = _queries(request, session)
    found = node_payload(
        world, primitives, item_id, window=window, self_item_id=session.self_item_id
    )
    if found is None:
        raise not_found("no such item")
    return found


@router.get("/schema")
def get_schema(request: Request, session: Session) -> dict[str, Any]:
    world, primitives = _queries(request, session)
    return schema_payload(world, primitives)


@router.get("/health")
def get_health(request: Request, session: Session) -> dict[str, Any]:
    world, primitives = _queries(request, session)
    return health_payload(world, primitives, now=clock.now(session.world_id))


@router.get("/workers")
def get_workers(request: Request, session: Session) -> dict[str, Any]:
    if request.query_params:
        raise invalid_arguments("worker activity accepts no query parameters", "query")
    return worker_activity_payload(
        request.app.state.driver,
        session,
        now=clock.now(session.world_id),
        settings=request.app.state.pass_settings,
    )


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not a number JSON allows")


async def _capped_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise invalid_arguments("the body is too large", "body")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise invalid_arguments("the body is too large", "body")
        chunks.append(chunk)
    return b"".join(chunks)


def check_json_type(request: Request) -> None:
    """A JSON body is required, which a cross-site form cannot send without a preflight."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != JSON_TYPE:
        raise invalid_arguments("the body must be application/json", "content-type")


async def read_json_body(request: Request, limit: int) -> Any:
    check_json_type(request)
    try:
        return json.loads(await _capped_body(request, limit), parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        # RecursionError: a body nested deeper than the parser's limit, which fits in the cap.
        raise invalid_arguments("the body is not valid JSON", "body") from None


@router.post("/positions")
async def post_positions(request: Request, session: Session) -> dict[str, int]:
    """Insert-only: an item keeps its first position; ids that are no item here write nothing."""
    body = await read_json_body(request, MAX_BODY_BYTES)
    if not isinstance(body, dict) or set(body) != {"positions"}:
        raise invalid_arguments("the body must be an object with positions only", "body")
    try:
        return await run_in_threadpool(
            place_items, request.app.state.driver, session.world_id, body["positions"]
        )
    except InvalidPositions as refused:
        raise invalid_arguments(str(refused), refused.field) from None
