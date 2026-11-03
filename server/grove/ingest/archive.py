"""Safe readers for uploads: zip members, zstd frames and JSON lines, each with a size cap.

Nothing here writes a file: a zip member is read into memory, never extracted, so a member
name such as `../x` reaches no path. Every cap counts the bytes actually read, because a zip's
declared sizes can lie. No error message holds a name from the archive or a byte of its content.

Memory: the upload's ReadBudget (connector.py) counts every byte read out of a zip member or a
zstd stream and every JSON line, over the whole upload. A JSON document or line is parsed only
when the count of its values (each one follows a comma or opens a container) is under a cap,
because a parsed value takes many times the bytes that spell it ("[]" is 2 bytes and 64 in
memory).
"""

import io
import json
import lzma
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import zstandard

from grove.ingest.connector import MAX_READ_BYTES, NotAnExport, ReadBudget, Upload

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# Caps for one upload. The API caps the upload itself (GROVE_UPLOAD_MAX_BYTES).
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = MAX_READ_BYTES
MAX_MEMBERS = 20_000
# The values one JSON document or one JSON line may hold. A real export of MAX_MEMBER_BYTES
# holds fewer; at the cap the parsed values take well under a GiB.
MAX_DOCUMENT_VALUES = 12_000_000
MAX_LINE_VALUES = 1_000_000
_CHUNK = 1024 * 1024
# What a zip member's decompressor raises on bad data, besides OSError and EOFError: deflate,
# LZMA, and a method this Python does not have.
_MEMBER_ERRORS = (
    zipfile.BadZipFile,
    OSError,
    ValueError,
    RuntimeError,
    EOFError,
    NotImplementedError,
    zlib.error,
    lzma.LZMAError,
)


def is_zip(upload: Upload) -> bool:
    return upload.head(4) in ZIP_MAGIC


def is_zstd(data_head: bytes) -> bool:
    return data_head[:4] == ZSTD_MAGIC


@dataclass(frozen=True)
class Member:
    """One file of a zip: its path inside the archive (never stored or logged) and its bytes."""

    path: str
    data: bytes

    def __repr__(self) -> str:
        return f"Member(size={len(self.data)})"


class _Budget:
    def __init__(self, total: int) -> None:
        self.left = total

    def take(self, count: int) -> None:
        self.left -= count
        if self.left < 0:
            raise NotAnExport("the archive holds more than this format allows")


def _read_capped(
    handle: Any, cap: int, budget: _Budget, upload_budget: ReadBudget | None = None
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = handle.read(_CHUNK)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > cap:
            raise NotAnExport("a file in the archive is larger than this format allows")
        budget.take(len(chunk))
        if upload_budget is not None:
            upload_budget.take_bytes(len(chunk))
        chunks.append(chunk)


def value_bound(data: bytes) -> int:
    """An upper bound on the JSON values (and keys) in `data`: each one is the first, opens a
    container or follows a comma. Commas and brackets inside strings only raise the bound."""
    return 1 + data.count(b",") + data.count(b"[") + data.count(b"{")


def _skipped(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    # macOS writes resource forks under __MACOSX/ and as ._ files: they are no export.
    return parts[0] == "__MACOSX" or parts[-1].startswith("._") or not parts[-1]


def zip_members(
    upload: Upload,
    *,
    suffixes: tuple[str, ...],
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> Iterator[Member]:
    """The zip's regular files whose name ends with one of `suffixes` (lower case), in archive
    order, one at a time. A nested archive is not opened. A broken zip is NotAnExport. The bytes
    read count against the upload's budget too."""
    upload.stream.seek(0)
    try:
        archive = zipfile.ZipFile(upload.stream)
    except _MEMBER_ERRORS as error:
        raise NotAnExport("the file is not a readable zip") from _no_context(error)
    budget = _Budget(max_total_bytes)
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise NotAnExport("the archive holds more files than this format allows")
        for info in infos:
            if info.is_dir() or _skipped(info.filename):
                continue
            if not info.filename.lower().endswith(suffixes):
                continue
            if info.file_size > max_member_bytes:
                raise NotAnExport("a file in the archive is larger than this format allows")
            try:
                with archive.open(info) as handle:
                    data = _read_capped(handle, max_member_bytes, budget, upload.budget)
            except NotAnExport:
                raise
            except _MEMBER_ERRORS as error:
                # RuntimeError: an encrypted member. EOFError: a cut archive. zlib and LZMA:
                # bad compressed data. NotImplementedError: a method Python lacks.
                raise NotAnExport("a file in the archive cannot be read") from _no_context(error)
            yield Member(path=info.filename, data=data)
            # Drop this member's bytes before the next is read: one member in memory at a time.
            del data


def zstd_decompress(
    data: bytes, *, cap: int = MAX_MEMBER_BYTES, budget: ReadBudget | None = None
) -> bytes:
    """One `.zst` stream (one or more frames), decompressed up to `cap` bytes. With the upload's
    `budget`, the bytes count against the whole upload: each of many members has no cap of its
    own past it."""
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
    try:
        return _read_capped(reader, cap, _Budget(cap), budget)
    except zstandard.ZstdError as error:
        raise NotAnExport("the compressed file cannot be read") from _no_context(error)
    finally:
        reader.close()


def json_document(data: bytes, *, max_values: int | None = None) -> Any:
    """One JSON document, or NotAnExport. A byte order mark is dropped first. A document of more
    than `max_values` values (MAX_DOCUMENT_VALUES) is refused before it is parsed."""
    limit = MAX_DOCUMENT_VALUES if max_values is None else max_values
    if value_bound(data) > limit:
        raise NotAnExport("the file holds more than this format allows")
    try:
        return json.loads(data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "surrogatepass"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise NotAnExport("the file is not a JSON document") from _no_context(error)


def _lines(text: bytes) -> Iterator[bytes]:
    """The lines of `text` one at a time, without a list of them all."""
    start = 0
    while True:
        end = text.find(b"\n", start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


def json_lines(
    data: bytes, *, budget: ReadBudget | None = None, max_line_values: int | None = None
) -> Iterator[tuple[int, Any]]:
    """Each line of a JSON Lines file as (its 1-based line number, the parsed object), one at a
    time. A blank line is skipped; a line that does not parse is skipped too, because a session
    file that is still being written ends in a cut line, and so is a line of more than
    `max_line_values` values (MAX_LINE_VALUES). Line numbers count every line, kept or not. Each
    line that is not blank counts against the upload's `budget`."""
    limit = MAX_LINE_VALUES if max_line_values is None else max_line_values
    text = data.removeprefix(b"\xef\xbb\xbf")
    for number, raw in enumerate(_lines(text), start=1):
        line = raw.strip()
        if not line:
            continue
        if budget is not None:
            budget.take_record()
        if value_bound(line) > limit:
            continue
        try:
            yield number, json.loads(line.decode("utf-8", "surrogatepass"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue


def _no_context(error: BaseException) -> None:
    """`raise ... from None`, named: the original error can hold a member name or a path."""
    del error
    return None
