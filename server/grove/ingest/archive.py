"""Safe readers for uploads: zip members, zstd frames and JSON lines, each with a size cap.

Nothing here writes a file: a zip member is read into memory, never extracted, so a member
name such as `../x` reaches no path. Every cap counts the bytes actually read, because a zip's
declared sizes can lie. No error message holds a name from the archive or a byte of its content.
"""

import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import zstandard

from grove.ingest.connector import NotAnExport, Upload

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# Caps for one upload. The API caps the upload itself (GROVE_UPLOAD_MAX_BYTES).
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 768 * 1024 * 1024
MAX_MEMBERS = 20_000
_CHUNK = 1024 * 1024


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


def _read_capped(handle: Any, cap: int, budget: _Budget) -> bytes:
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
        chunks.append(chunk)


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
    order. A nested archive is not opened. A broken zip is NotAnExport."""
    upload.stream.seek(0)
    try:
        archive = zipfile.ZipFile(upload.stream)
    except (zipfile.BadZipFile, OSError, ValueError) as error:
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
                    data = _read_capped(handle, max_member_bytes, budget)
            except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, EOFError) as error:
                # RuntimeError: an encrypted member. EOFError: a cut archive.
                raise NotAnExport("a file in the archive cannot be read") from _no_context(error)
            yield Member(path=info.filename, data=data)


def zstd_decompress(data: bytes, *, cap: int = MAX_MEMBER_BYTES) -> bytes:
    """One `.zst` stream (one or more frames), decompressed up to `cap` bytes."""
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
    try:
        return _read_capped(reader, cap, _Budget(cap))
    except zstandard.ZstdError as error:
        raise NotAnExport("the compressed file cannot be read") from _no_context(error)
    finally:
        reader.close()


def json_document(data: bytes) -> Any:
    """One JSON document, or NotAnExport. A byte order mark is dropped first."""
    try:
        return json.loads(data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "surrogatepass"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise NotAnExport("the file is not a JSON document") from _no_context(error)


def json_lines(data: bytes) -> Iterator[tuple[int, Any]]:
    """Each line of a JSON Lines file as (its 1-based line number, the parsed object). A blank
    line is skipped; a line that does not parse is skipped too, because a session file that is
    still being written ends in a cut line. Line numbers count every line, kept or not."""
    text = data.removeprefix(b"\xef\xbb\xbf")
    for number, raw in enumerate(text.split(b"\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            yield number, json.loads(line.decode("utf-8", "surrogatepass"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue


def _no_context(error: BaseException) -> None:
    """`raise ... from None`, named: the original error can hold a member name or a path."""
    del error
    return None
