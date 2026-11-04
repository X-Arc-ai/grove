"""A read-only, local-clone connector (A.4, D5, D10).

The caller selects an allowed clone; this module never clones or fetches one. A temporary
bare metadata directory reads only that clone's object database, without its configuration,
hooks, replacement refs, worktree, mailmap, filters or remotes. Linked worktrees, shallow
clones, reftable refs and object alternates require an ordinary complete clone first.

A commit is evidence that the commit exists. Its message does not prove a deployment.
The caller screens these ParsedChats before storing them and saves head only after success.
"""

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from grove import clock
from grove.ingest import (
    MAX_MESSAGE_CHARS,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    ReadBudget,
    bounded_id,
    clean_text,
    in_time_order,
    message_key,
)

MAX_COMMITS = 5_000
MAX_COMMIT_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_REFS_BYTES = 4 * 1024 * 1024
READ_SECONDS = 30.0
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF = re.compile(r"refs/[A-Za-z0-9_./-]+\Z")
_IDENTITY = re.compile(rb"(.*?) <([^<>\r\n]*)> (-?[0-9]{1,12}) ([+-][0-9]{4})\Z")
_FAIL = "the local repository cannot be read"
_LIMIT = "the local repository exceeds one read's limits"


class GitReadError(NotAnExport):
    """A failed read, with no path, command output, object text or credential in the error."""


@dataclass(frozen=True)
class GitRead:
    chats: list[ParsedChat]
    head: str


def _file(path: Path, limit: int) -> bytes:
    """Bound a regular file before reading it and refuse symlinks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise GitReadError(_LIMIT)
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise GitReadError(_LIMIT)
            return data
    except OSError:
        raise GitReadError(_FAIL) from None


def _metadata(path: Path) -> Path:
    try:
        root = path.resolve(strict=True)
        metadata = root / ".git" if (root / ".git").exists() else root
        if not root.is_dir() or metadata.is_symlink() or not metadata.is_dir():
            raise GitReadError(_FAIL)
        objects = metadata / "objects"
        if objects.is_symlink() or not objects.is_dir():
            raise GitReadError(_FAIL)
        # These can redirect reads outside the selected clone or make history incomplete.
        for name in ("commondir", "shallow", "reftable"):
            if (metadata / name).exists() or (metadata / name).is_symlink():
                raise GitReadError(_FAIL)
        info = objects / "info"
        if info.is_symlink():
            raise GitReadError(_FAIL)
        for name in ("alternates", "http-alternates"):
            if (info / name).exists() or (info / name).is_symlink():
                raise GitReadError(_FAIL)
        return metadata
    except (OSError, RuntimeError):
        raise GitReadError(_FAIL) from None


def _head(metadata: Path) -> str:
    value = _file(metadata / "HEAD", 4096).strip()
    for _ in range(8):
        try:
            text = value.decode("ascii")
        except UnicodeError:
            raise GitReadError(_FAIL) from None
        if _SHA.fullmatch(text):
            return text
        if not text.startswith("ref: "):
            break
        ref = text[5:]
        if (
            not _REF.fullmatch(ref)
            or ".." in ref
            or any(part.startswith(".") or part.endswith((".lock", ".")) for part in ref.split("/"))
        ):
            break
        target = metadata
        for part in ref.split("/"):
            target = target / part
            if target.is_symlink():
                raise GitReadError(_FAIL)
        if target.is_file():
            value = _file(target, 4096).strip()
            continue
        packed = metadata / "packed-refs"
        if packed.exists():
            for row in _file(packed, MAX_REFS_BYTES).splitlines():
                fields = row.split(b" ", 1)
                if len(fields) == 2 and fields[1] == ref.encode("ascii"):
                    value = fields[0]
                    break
            else:
                break
            continue
        break
    raise GitReadError(_FAIL)


def _environment(objects: Path) -> dict[str, str]:
    # Whitelist rather than copy: no inherited GIT_CONFIG_*, tracing, SSH or credential vars.
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OBJECT_DIRECTORY": str(objects),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "",
    }


def _run(
    metadata: Path,
    env: dict[str, str],
    args: list[str],
    *,
    limit: int,
    deadline: float,
    input_data: bytes = b"",
    allowed: tuple[int, ...] = (0,),
) -> tuple[int, bytes]:
    """Read bounded output while writing bounded input; never retain stderr."""
    result = bytearray()
    try:
        with subprocess.Popen(
            ["git", "--no-pager", "--no-replace-objects", f"--git-dir={metadata}", *args],
            cwd=metadata,
            env=env,
            stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(process.stdout.fileno(), False)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    offset = 0
                    if input_data:
                        os.set_blocking(process.stdin.fileno(), False)
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    while selector.get_map():
                        remaining = deadline - clock.monotonic()
                        if remaining <= 0:
                            raise GitReadError(_LIMIT)
                        events = selector.select(remaining)
                        if not events:
                            raise GitReadError(_LIMIT)
                        for key, _ in events:
                            if key.fileobj is process.stdin:
                                offset += os.write(key.fd, input_data[offset : offset + 65536])
                                if offset == len(input_data):
                                    selector.unregister(process.stdin)
                                    process.stdin.close()
                            else:
                                chunk = os.read(key.fd, min(65536, limit + 1 - len(result)))
                                if not chunk:
                                    selector.unregister(process.stdout)
                                result.extend(chunk)
                                if len(result) > limit:
                                    raise GitReadError(_LIMIT)
                    code = process.wait(timeout=max(0.001, deadline - clock.monotonic()))
                    if code not in allowed:
                        raise GitReadError(_FAIL)
                    return code, bytes(result)
            except BaseException:
                process.kill()
                process.wait()
                raise
    except (OSError, subprocess.SubprocessError):
        raise GitReadError(_FAIL) from None


def _decode(raw: bytes, encoding: str) -> str:
    try:
        return clean_text(raw.decode(encoding, "replace"))
    except (LookupError, UnicodeError):
        return clean_text(raw.decode("utf-8", "replace"))


def _identity(raw: bytes, encoding: str) -> tuple[str, str, datetime]:
    match = _IDENTITY.fullmatch(raw)
    if match is None:
        raise GitReadError(_FAIL)
    name, address, stamp, zone = match.groups()
    hour, minute = int(zone[1:3]), int(zone[3:5])
    if hour > 23 or minute > 59:
        raise GitReadError(_FAIL)
    offset = (hour * 60 + minute) * (-1 if zone.startswith(b"-") else 1)
    try:
        at = datetime.fromtimestamp(int(stamp), timezone(timedelta(minutes=offset)))
    except (ValueError, OverflowError, OSError):
        raise GitReadError(_FAIL) from None
    return _decode(name, encoding), _decode(address, encoding), at


def _chat(repository_id: str, sha: str, raw: bytes, budget: ReadBudget) -> ParsedChat:
    digest = hashlib.sha256 if len(sha) == 64 else hashlib.sha1
    if digest(f"commit {len(raw)}\0".encode() + raw).hexdigest() != sha:
        raise GitReadError(_FAIL)
    headers, separator, body = raw.partition(b"\n\n")
    if not separator:
        raise GitReadError(_FAIL)
    fields: dict[bytes, list[bytes]] = {}
    for line in headers.splitlines():
        if line.startswith(b" "):
            continue  # A signature's continuation, not another header.
        name, space, value = line.partition(b" ")
        if not space:
            raise GitReadError(_FAIL)
        fields.setdefault(name, []).append(value)
    if any(len(fields.get(name, [])) != 1 for name in (b"tree", b"author", b"committer")):
        raise GitReadError(_FAIL)
    try:
        parents = [value.decode("ascii") for value in fields.get(b"parent", [])]
        tree = fields[b"tree"][0].decode("ascii")
    except UnicodeError:
        raise GitReadError(_FAIL) from None
    if any(not _SHA.fullmatch(value) or len(value) != len(sha) for value in [tree, *parents]):
        raise GitReadError(_FAIL)
    encoding = fields.get(b"encoding", [b"utf-8"])[0].decode("ascii", "replace")
    author, author_email, authored = _identity(fields[b"author"][0], encoding)
    committer, committer_email, committed = _identity(fields[b"committer"][0], encoding)
    message = _decode(body, encoding)
    text = json.dumps(
        {
            "artifact": "merge" if len(parents) > 1 else "commit",
            "repository": clean_text(repository_id),
            "commit": sha,
            "parents": parents,
            "author": author,
            "author_email": author_email,
            "authored_at": authored.isoformat(),
            "committer": committer,
            "committer_email": committer_email,
            "committed_at": committed.isoformat(),
            "message": clean_text(message),
            "proves": "This commit exists; its message does not prove a feature is live.",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    if len(text) > MAX_MESSAGE_CHARS:
        # Keep the complete metadata envelope; do not truncate a commit into partial JSON.
        raise GitReadError(_LIMIT)
    budget.take_chat()
    budget.take_message(len(text))
    # Hash each identity component once; a raw repository ID never lengthens the chat ID.
    key = message_key("git", repository_id, sha)
    messages = in_time_order(
        [ParsedMessage(key, text, authored, "third_party", sender=author or None)]
    )
    return ParsedChat(key, tuple(messages))


def read_repository(path: Path, repository_id: str, *, previous_head: str | None = None) -> GitRead:
    """Read commits reachable from a fixed HEAD, oldest source timestamp first.

    A missing/non-ancestor previous head triggers full replay with the same message keys.
    A malformed cursor or incomplete read raises GitReadError; it is never an empty success.
    The clone has no pull-request records: a PR title is never treated as commit evidence.
    """
    if not isinstance(path, Path) or not isinstance(repository_id, str) or not repository_id:
        raise GitReadError(_FAIL)
    bounded_id(repository_id)
    if previous_head is not None and (
        not isinstance(previous_head, str) or not _SHA.fullmatch(previous_head)
    ):
        raise GitReadError(_FAIL)
    metadata = _metadata(path)
    head = _head(metadata)
    env = _environment(metadata / "objects")
    deadline = clock.monotonic() + READ_SECONDS
    with tempfile.TemporaryDirectory(prefix="grove-git-read-") as directory:
        isolated = Path(directory)
        (isolated / "refs").mkdir()
        (isolated / "objects").mkdir()
        (isolated / "HEAD").write_text(head + "\n", encoding="ascii")
        config = "[core]\n\tbare = true\n\trepositoryformatversion = "
        config += "1\n[extensions]\n\tobjectformat = sha256\n" if len(head) == 64 else "0\n"
        (isolated / "config").write_text(config, encoding="ascii")

        def run(args, limit, **options):
            return _run(isolated, env, args, limit=limit, deadline=deadline, **options)

        if run(["cat-file", "-t", head], 32)[1] != b"commit\n":
            raise GitReadError(_FAIL)
        revision = [head]
        if previous_head is not None and len(previous_head) == len(head):
            code, _ = run(
                ["merge-base", "--is-ancestor", previous_head, head], 4096, allowed=(0, 1, 128)
            )
            if code == 0:
                revision.append(f"^{previous_head}")
        _, listed = run(
            ["rev-list", "--reverse", "--topo-order", f"--max-count={MAX_COMMITS + 1}", *revision],
            (MAX_COMMITS + 1) * 65,
        )
        try:
            shas = listed.decode("ascii").splitlines()
        except UnicodeError:
            raise GitReadError(_FAIL) from None
        if len(shas) > MAX_COMMITS:
            raise GitReadError(_LIMIT)
        if any(not _SHA.fullmatch(sha) or len(sha) != len(head) for sha in shas):
            raise GitReadError(_FAIL)
        if not shas:
            return GitRead([], head)
        _, sizes = run(["cat-file", "--batch-check"], len(shas) * 100, input_data=listed)
        lines = sizes.splitlines()
        if len(lines) != len(shas):
            raise GitReadError(_FAIL)
        budget = ReadBudget(max_bytes=MAX_TOTAL_BYTES, max_chats=MAX_COMMITS)
        expected = []
        for sha, line in zip(shas, lines, strict=True):
            fields = line.split()
            if (
                len(fields) != 3
                or fields[:2] != [sha.encode(), b"commit"]
                or not fields[2].isdigit()
            ):
                raise GitReadError(_FAIL)
            size = int(fields[2])
            if size > MAX_COMMIT_BYTES:
                raise GitReadError(_LIMIT)
            budget.take_bytes(size)
            expected.append(size)
        _, objects = run(
            ["cat-file", "--batch"], sum(expected) + len(shas) * 100, input_data=listed
        )
        chats = []
        offset = 0
        for sha, size in zip(shas, expected, strict=True):
            prefix = f"{sha} commit {size}\n".encode()
            if objects[offset : offset + len(prefix)] != prefix:
                raise GitReadError(_FAIL)
            offset += len(prefix)
            raw = objects[offset : offset + size]
            offset += size
            if len(raw) != size or objects[offset : offset + 1] != b"\n":
                raise GitReadError(_FAIL)
            offset += 1
            chats.append(_chat(repository_id, sha, raw, budget))
        if offset != len(objects):
            raise GitReadError(_FAIL)
        chats.sort(key=lambda chat: chat.messages[0].at)
        return GitRead(chats, head)
