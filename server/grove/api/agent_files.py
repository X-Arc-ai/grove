"""The files and notes a person adds in setup, kept where the world's OpenCode passes run.

Each world has one folder under GROVE_AGENT_FILES_DIR. The API writes it and the runner starts
every pass of that world in it (grove.runner.loop.pass_folder), so an added file is in the
working folder of the agent. Nothing here reads the content, screens it, or writes the store.

POST /v1/agent-files takes the file as the raw body, never multipart: a cross-site form cannot
send application/octet-stream without a preflight, and the session cookie is SameSite=Lax.
"""

import os
import re
import unicodedata
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from grove.api.errors import ApiError, invalid_arguments, unauthenticated, unavailable
from grove.api.session import current_session
from grove.api.views import read_json_body
from grove.demo_policy import is_demo_world
from grove.views.session import ViewSession

PREFIX = "/v1/agent-files"
FOLDER_VAR = "GROVE_AGENT_FILES_DIR"
FILE_TYPE = "application/octet-stream"
# Match client_max_body_size of this route in deploy/nginx/grove-https.conf.template.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_NOTE_BYTES = 70 * 1024
MAX_TITLE_CHARS = 100
MAX_NAME_BYTES = 200
MAX_WORLD_BYTES = 1024 * 1024 * 1024
MAX_WORLD_FILES = 200
NOTES = "notes"
TEMP_PREFIX = ".upload-"
KINDS = {"file": "", "note": NOTES}

router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]


def clean_name(raw: str) -> str:
    """One plain file name: no folder, no control character, no hidden or reserved name."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable()).strip().lstrip(".").strip()
    if not name or name == NOTES:
        raise invalid_arguments("The file needs a name.", "name")
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        stem, dot, suffix = name.rpartition(".")
        name = stem[:-1] + dot + suffix if dot and len(suffix) <= 16 and stem else name[:-1]
    return name


def world_folder(session: ViewSession, *, create: bool) -> Path:
    root = os.environ.get(FOLDER_VAR)
    if not root or not Path(root).is_dir():
        raise unavailable("Files are not available yet.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", session.world_id) or session.world_id[0] == ".":
        raise unavailable("Files are not available yet.")
    folder = Path(root) / session.world_id
    if create:
        # No chmod: the root folder's setgid bit gives the runner's group to what is made here.
        (folder / NOTES).mkdir(parents=True, mode=0o750, exist_ok=True)
    return folder


def _rows(folder: Path, kind: str) -> list[dict[str, Any]]:
    path = folder / KINDS[kind]
    if not path.is_dir():
        return []
    found = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
            found.append((info.st_mtime_ns, entry.name, info.st_size))
    return [
        {"name": name, "kind": kind, "bytes": size}
        for _, name, size in sorted(found, key=lambda row: (-row[0], row[1]))
    ]


def _listing(folder: Path) -> list[dict[str, Any]]:
    return _rows(folder, "file") + _rows(folder, "note")


def _check_room(folder: Path, size: int, name: str, kind: str) -> None:
    rows = _listing(folder)
    others = [row for row in rows if (row["kind"], row["name"]) != (kind, name)]
    if (
        len(others) >= MAX_WORLD_FILES
        or sum(row["bytes"] for row in others) + size > MAX_WORLD_BYTES
    ):
        raise ApiError(413, "too_large", "Your Grove has no room for this file.")


def _write(folder: Path, kind: str, name: str, data: bytes) -> None:
    target = folder / KINDS[kind] / name
    temp = folder / (TEMP_PREFIX + uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


async def _eligible(request: Request, session: ViewSession) -> None:
    if "authorization" in request.headers:
        raise unauthenticated("files require a browser session")
    if await run_in_threadpool(is_demo_world, request.app.state.driver, session.world_id):
        raise ApiError(403, "scope_denied", "This account cannot add files.")


def _no_query(request: Request, allowed: frozenset[str] = frozenset()) -> None:
    if set(request.query_params) - allowed:
        raise invalid_arguments("query fields are not accepted", "query")


@router.get("")
async def list_files(request: Request, session: Session) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request)
    folder = world_folder(session, create=False)
    return {"account": session.user_id, "files": _listing(folder)}


@router.post("")
async def add_file(request: Request, session: Session) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request, frozenset({"name"}))
    name = clean_name(request.query_params.get("name", ""))
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != FILE_TYPE:
        raise invalid_arguments(f"the body must be {FILE_TYPE}", "content-type")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise ApiError(413, "too_large", "This file is larger than 32 MiB.")
    folder = world_folder(session, create=True)
    temp = folder / (TEMP_PREFIX + uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ApiError(413, "too_large", "This file is larger than 32 MiB.")
                out.write(chunk)
        if size == 0:
            raise invalid_arguments("The file is empty.", "body")
        _check_room(folder, size, name, "file")
        os.replace(temp, folder / name)
    finally:
        temp.unlink(missing_ok=True)
    return {"account": session.user_id, "file": {"name": name, "kind": "file", "bytes": size}}


@router.post("/notes")
async def add_note(request: Request, session: Session) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request)
    body = await read_json_body(request, MAX_NOTE_BYTES)
    if not isinstance(body, dict) or set(body) != {"title", "body"}:
        raise invalid_arguments("the body must be an object with title and body only", "body")
    title, text = body["title"], body["body"]
    if not isinstance(title, str) or not isinstance(text, str) or not text.strip():
        raise invalid_arguments("A note needs a title and some text.", "body")
    name = clean_name(title[:MAX_TITLE_CHARS]) + ".md"
    data = f"# {title.strip()}\n\n{text.strip()}\n".encode()
    folder = world_folder(session, create=True)
    _check_room(folder, len(data), name, "note")
    _write(folder, "note", name, data)
    return {"account": session.user_id, "file": {"name": name, "kind": "note", "bytes": len(data)}}


@router.delete("/{kind}/{name}")
async def remove_file(request: Request, session: Session, kind: str, name: str) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request)
    if kind not in KINDS or clean_name(name) != name:
        raise invalid_arguments("Choose a file from the list.", "name")
    target = world_folder(session, create=False) / KINDS[kind] / name
    if target.is_symlink() or not target.is_file():
        raise ApiError(404, "not_found", "This file is no longer here.")
    target.unlink()
    return {"account": session.user_id, "removed": name}
