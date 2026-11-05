"""The files and notes a person adds in setup, kept where the world's OpenCode passes run.

Each world has one folder under GROVE_AGENT_FILES_DIR. The API writes it and the runner starts
every Learner pass of that world in it (grove.runner.loop.pass_folder), so an added file is in
the working folder of the agent. Nothing here reads the content, screens it, or writes the store.

POST /v1/agent-files takes the file as the raw body, never multipart: a cross-site form cannot
send application/octet-stream without a preflight, and the session cookie is SameSite=Lax.
"""

import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.errors import ApiError, invalid_arguments, unauthenticated, unavailable
from grove.api.session import current_session
from grove.api.views import read_json_body
from grove.api.world_activity import world_write
from grove.demo_policy import is_demo_world
from grove.instruction_names import TEMP_PREFIX, is_instruction_name
from grove.views.session import ViewSession

PREFIX = "/v1/agent-files"
FOLDER_VAR = "GROVE_AGENT_FILES_DIR"
FILE_TYPE = "application/octet-stream"
# Match client_max_body_size of this route in deploy/nginx/grove-https.conf.template.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_NOTE_BYTES = 70 * 1024
MAX_TITLE_CHARS = 100
MAX_NAME_BYTES = 200
NOTE_SUFFIX = ".md"
# An earlier build added the suffix after the cut, so a stored note name can be this long.
MAX_STORED_NAME_BYTES = MAX_NAME_BYTES + len(NOTE_SUFFIX)
MAX_WORLD_BYTES = 1024 * 1024 * 1024
MAX_WORLD_FILES = 200
NOTES = "notes"
# Longer than any upload runs, so a temporary file of this age is what a killed API left.
TEMP_MAX_AGE_SECONDS = 3600
KINDS = {"file": "", "note": NOTES}

router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]


def _utf8(text: str, field: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, which JSON can carry
        raise invalid_arguments("The text holds a broken character.", field) from None


def clean_name(raw: str, room: int = 0) -> str:
    """One plain file name: no folder, no control character, no hidden name.

    `room` is the bytes of a suffix that the caller adds after. A URL's decoder turns a lone
    surrogate into U+FFFD, so that character is refused as the surrogate is."""
    _utf8(raw, "name")
    if "\ufffd" in raw:
        raise invalid_arguments("The text holds a broken character.", "name")
    name = "".join(char for char in raw if char.isprintable())
    name = unicodedata.normalize("NFC", name).replace("\\", "/").rsplit("/", 1)[-1]
    name = name.lstrip(". ").rstrip(" ")
    if not name:
        raise invalid_arguments("The file needs a name.", "name")
    while len(name.encode("utf-8")) > MAX_NAME_BYTES - room:
        stem, dot, suffix = name.rpartition(".")
        name = stem[:-1] + dot + suffix if dot and len(suffix) <= 16 and stem else name[:-1]
    return name.rstrip(" ")


def _allowed(name: str) -> str:
    """The name, unless OpenCode would act on a file of that name in the world's folder."""
    if name == NOTES or is_instruction_name(name):
        raise invalid_arguments("This name is kept for the agent. Choose another name.", "name")
    return name


def _listable(name: str) -> bool:
    """A name the listing could give. It is never cleaned: a stored name comes off as it is."""
    return (
        name.isprintable()
        and not name.startswith(".")
        and not {"/", "\\"} & set(name)
        and len(name.encode("utf-8")) <= MAX_STORED_NAME_BYTES
    )


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


def _disk_use(folder: Path, temp: Path | None) -> dict[Path, int]:
    """Every file on disk under the world's folder and its size, hidden ones too.

    `temp` is the running request's own upload, which the caller counts by size. The temporary
    file of another request counts while it runs. One that is older than any upload is what a
    killed API left: no listing shows it and no DELETE reaches it, so it goes here."""
    oldest = clock.now().timestamp() - TEMP_MAX_AGE_SECONDS
    found = {}
    for path in folder.rglob("*"):
        try:
            info = path.lstat()
            if path == temp or stat.S_ISDIR(info.st_mode):
                continue
            if (
                path.parent == folder
                and path.name.startswith(TEMP_PREFIX)
                and info.st_mtime < oldest
            ):
                path.unlink(missing_ok=True)
                continue
        except FileNotFoundError:  # another request finished with it
            continue
        found[path] = info.st_size
    return found


def _check_room(folder: Path, size: int, name: str, kind: str, temp: Path | None = None) -> None:
    others = _disk_use(folder, temp)
    others.pop(folder / KINDS[kind] / name, None)
    if len(others) >= MAX_WORLD_FILES or sum(others.values()) + size > MAX_WORLD_BYTES:
        raise ApiError(413, "too_large", "Your Grove has no room for this file.")


def _write(folder: Path, kind: str, name: str, data: bytes) -> None:
    target = folder / KINDS[kind] / name
    temp = folder / (TEMP_PREFIX + uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        # A link never replaces a file, so of two requests for one name only one gets it.
        os.link(temp, target)
    except FileExistsError:
        message = "A note with this title already exists. Choose another title."
        raise ApiError(409, "conflict", message) from None
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
    with world_write(session.world_id):
        await run_in_threadpool(current_session, request)
        return await _take_file(request, session)


async def _take_file(request: Request, session: ViewSession) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request, frozenset({"name"}))
    name = _allowed(clean_name(request.query_params.get("name", "")))
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
        _check_room(folder, size, name, "file", temp)
        os.replace(temp, folder / name)
    finally:
        temp.unlink(missing_ok=True)
    return {"account": session.user_id, "file": {"name": name, "kind": "file", "bytes": size}}


@router.post("/notes")
async def add_note(request: Request, session: Session) -> dict[str, Any]:
    with world_write(session.world_id):
        await run_in_threadpool(current_session, request)
        return await _take_note(request, session)


async def _take_note(request: Request, session: ViewSession) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request)
    body = await read_json_body(request, MAX_NOTE_BYTES)
    if not isinstance(body, dict) or set(body) != {"title", "body"}:
        raise invalid_arguments("the body must be an object with title and body only", "body")
    title, text = body["title"], body["body"]
    if not isinstance(title, str) or not isinstance(text, str) or not text.strip():
        raise invalid_arguments("A note needs a title and some text.", "body")
    data = _utf8(f"# {title.strip()}\n\n{text.strip()}\n", "body")
    # A slash in a title is text, not a folder: Q1/plan and Q2/plan are two notes.
    stem = re.sub(r"[/\\]", "-", title[:MAX_TITLE_CHARS])
    name = _allowed(clean_name(stem, len(NOTE_SUFFIX)) + NOTE_SUFFIX)
    folder = world_folder(session, create=True)
    _check_room(folder, len(data), name, "note")
    _write(folder, "note", name, data)
    return {"account": session.user_id, "file": {"name": name, "kind": "note", "bytes": len(data)}}


@router.delete("/{kind}/{name}")
async def remove_file(request: Request, session: Session, kind: str, name: str) -> dict[str, Any]:
    await _eligible(request, session)
    _no_query(request)
    if kind not in KINDS or not _listable(name):
        raise invalid_arguments("Choose a file from the list.", "name")
    target = world_folder(session, create=False) / KINDS[kind] / name
    if target.is_symlink() or not target.is_file():
        raise ApiError(404, "not_found", "This file is no longer here.")
    target.unlink()
    return {"account": session.user_id, "removed": name}
