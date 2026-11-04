"""Real browser-session material endpoints; no provider or model runs in a handler."""

import asyncio
import re
import threading
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from grove.api.errors import ApiError
from grove.api.session import current_session
from grove.api.views import read_json_body
from grove.demo_policy import DemoDenied, is_demo_world
from grove.ingest.material_files import MAX_FILE, MIMES, MaterialInvalid, parse_file
from grove.ingest.materials import bounded_text, input_digest, key_hash, uuid_key
from grove.views import materials as read
from grove.views.session import SESSION_COOKIE, token_hash
from grove.writer import materials as write
from grove.writer.forget import forget
from grove.writer.forget_approvals import mint_approval
from grove.writer.forget_ledger import LedgerUnavailable

PREFIX = "/v1/materials"
router = APIRouter(prefix=PREFIX)
_body_slot = asyncio.Lock()


def _response(value, status=200):
    return JSONResponse(
        value, status_code=status, headers={"Cache-Control": "no-store", "Vary": "Cookie"}
    )


async def _call(func, *args, **kwargs):
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except (write.MaterialMissing, LookupError):
        raise ApiError(404, "not_found", "the material is unavailable") from None
    except write.MaterialConflict as error:
        raise ApiError(409, error.code, "the material changed; reload and try again") from None
    except write.MaterialSessionEnded:
        raise ApiError(401, "unauthenticated", "the session ended") from None
    except DemoDenied:
        raise ApiError(
            403, "demo_restricted", "materials are unavailable in this demo account"
        ) from None
    except LedgerUnavailable:
        raise ApiError(
            503, "ledger_unavailable", "materials require ledger recovery or configuration"
        ) from None
    except (MaterialInvalid, ValueError):
        raise ApiError(
            422,
            "material_invalid",
            "the material is unsupported, unreadable or exceeds a content limit",
        ) from None


async def _session(request, mutation=False):
    if request.headers.get("authorization") is not None:
        raise ApiError(403, "session_required", "materials require a browser session")
    session = await run_in_threadpool(current_session, request)
    if mutation:
        if (
            request.headers.get("origin") != request.app.state.settings.public_base_url
            or request.headers.get("sec-fetch-site") == "cross-site"
            or request.headers.get("x-grove-action") != "materials"
        ):
            raise ApiError(403, "forbidden_origin", "the material action requires the same origin")
        if await run_in_threadpool(is_demo_world, request.app.state.driver, session.world_id):
            raise ApiError(403, "demo_restricted", "materials are unavailable in this demo account")
    return session


def _args(request, session):
    return request.app.state.driver, session, request.cookies.get(SESSION_COOKIE)


def _key(request, session, operation):
    try:
        return key_hash(session.world_id, request.headers.get("idempotency-key"), operation)
    except MaterialInvalid:
        raise ApiError(
            422, "idempotency_required", "a canonical UUID Idempotency-Key is required"
        ) from None


@router.get("")
async def list_materials(
    request: Request, kind: str | None = None, limit: int = 20, cursor: str | None = None
):
    session = await _session(request)
    return _response(
        await _call(read.listing, *_args(request, session), kind=kind, limit=limit, cursor=cursor)
    )


async def _parse(data, mime, title):
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(parse_file, data, mime, title, cancel=cancel))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        try:
            await asyncio.shield(task)
        except MaterialInvalid:
            pass
        raise
    except MaterialInvalid:
        raise ApiError(
            422, "material_invalid", "the document contains unsupported or unreadable content"
        ) from None


async def _save(
    request, session, title, body, key, digest, size, material_id=None, expected_revision=None
):
    value = await _call(
        write.save,
        request.app.state.driver,
        session,
        token_hash(request.cookies[SESSION_COOKIE]),
        title=title,
        body=body,
        request_key_hash=key,
        input_digest=digest,
        input_bytes=size,
        material_id=material_id,
        expected_revision=expected_revision,
    )
    result = await _call(read.detail, *_args(request, session), value["id"])
    return _response(result, 201 if value["fresh"] else 200)


@router.post("/files")
async def file_create(request: Request):
    session = await _session(request, True)
    key = _key(request, session, "file")
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in MIMES:
        raise ApiError(415, "unsupported_format", "use text, Markdown, PDF, DOCX or PPTX")
    name = request.headers.get("x-grove-filename", "")
    if len(name) > 6144:
        raise ApiError(413, "too_large", "the material title exceeds its limit")
    try:
        if re.search(r"%(?![0-9A-Fa-f]{2})", name):
            raise MaterialInvalid()
        title = bounded_text(unquote(name, encoding="utf-8", errors="strict"), title=True)
    except (UnicodeError, MaterialInvalid):
        raise ApiError(422, "invalid_title", "a valid material title is required") from None
    if _body_slot.locked():
        raise ApiError(429, "material_busy", "another material upload is active")
    async with _body_slot:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_FILE:
                raise ApiError(413, "too_large", "the file exceeds 16 MiB")
            raw.extend(chunk)
        data = bytes(raw)
        parsed = await _parse(data, mime, title)
        return await _save(
            request, session, title, parsed, key, input_digest(title, data, mime), len(data)
        )


async def _note(request, material_id=None):
    session = await _session(request, True)
    if material_id is not None:
        try:
            uuid_key(material_id)
        except MaterialInvalid:
            raise ApiError(404, "not_found", "the material is unavailable") from None
    operation = "note" if material_id is None else "edit:" + material_id
    key = _key(request, session, operation)
    if _body_slot.locked():
        raise ApiError(429, "material_busy", "another material upload is active")
    async with _body_slot:
        body = await read_json_body(request, 70 * 1024)
        expected = {"title", "body"} | ({"expected_revision"} if material_id else set())
        if type(body) is not dict or set(body) != expected:
            raise ApiError(422, "invalid_material", "the note fields are invalid")
        try:
            title, text = bounded_text(body["title"], title=True), bounded_text(body["body"])
            revision = body.get("expected_revision")
            if material_id and (type(revision) is not int or not 1 <= revision < 2**31 - 1):
                raise MaterialInvalid()
        except MaterialInvalid:
            raise ApiError(422, "invalid_material", "the note fields exceed their limits") from None
        digest = input_digest(title, text, operation + ":" + str(revision))
        return await _save(
            request, session, title, text, key, digest, len(text.encode()), material_id, revision
        )


@router.post("/notes")
async def note_create(request: Request):
    return await _note(request)


@router.put("/notes/{material_id}")
async def note_edit(material_id: str, request: Request):
    return await _note(request, material_id)


@router.get("/{material_id}")
async def material_detail(material_id: str, request: Request):
    session = await _session(request)
    return _response(await _call(read.detail, *_args(request, session), material_id))


@router.get("/{material_id}/delete-preview")
async def delete_preview(material_id: str, request: Request):
    session = await _session(request)
    return _response(await _call(read.preview, *_args(request, session), material_id))


async def _delete_body(material_id, request, confirmation):
    session = await _session(request, True)
    body = await read_json_body(request, 4096)
    fields = {"expected_revision", "confirm" if confirmation else "approval_token"}
    if (
        type(body) is not dict
        or set(body) != fields
        or type(body["expected_revision"]) is not int
        or body["expected_revision"] < 1
        or confirmation
        and body["confirm"] is not True
    ):
        raise ApiError(422, "invalid_material", "the deletion fields are invalid")
    target = {"type": "material", "material_id": material_id, "revision": body["expected_revision"]}
    return session, body, target


@router.post("/{material_id}/delete-confirmations")
async def delete_confirmation(material_id: str, request: Request):
    session, body, target = await _delete_body(material_id, request, True)
    await _call(read.preview, *_args(request, session), material_id)
    approval = await _call(
        mint_approval,
        request.app.state.driver,
        session.world_id,
        session.user_id,
        target,
        material_session=session,
        session_hash=token_hash(request.cookies[SESSION_COOKIE]),
    )
    return _response(
        {
            "account": session.user_id,
            "approval_token": approval.token,
            "expires_at": approval.expires_at.isoformat(),
            "one_use": True,
        }
    )


@router.post("/{material_id}/delete")
async def material_delete(material_id: str, request: Request):
    session, body, target = await _delete_body(material_id, request, False)
    receipt = await _call(
        forget,
        request.app.state.driver,
        session.world_id,
        target,
        approval_token=body["approval_token"],
        actor=session.user_id,
        material_session=session,
        session_hash=token_hash(request.cookies[SESSION_COOKIE]),
    )
    return _response({"account": session.user_id, "deleted": True, "receipt": receipt})
