"""Bounded in-memory material readers; uploaded dates never become evidence time."""

import hashlib
import json
import os
import selectors
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from grove import clock
from grove.ingest.connector import ReadBudget, clean_text
from grove.ingest.documents import _sections
from grove.source_facts import SourceLocator, SourceTimeFacts, TextSourceFacts

MAX_FILE = 16 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
MAX_PARTS = 1000
MAX_BLOCK = 30_000
MIMES = {
    "text/plain": "text",
    "text/markdown": "markdown",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


class MaterialInvalid(ValueError):
    """A document cannot be retained completely within the accepted limits."""


@dataclass(frozen=True, repr=False)
class MaterialPart:
    key: str
    text: str = field(repr=False)
    source_facts: TextSourceFacts = field(repr=False)


@dataclass(frozen=True, repr=False)
class ParsedMaterial:
    format: str
    digest: str
    parser: str
    parts: tuple[MaterialPart, ...] = field(repr=False)


def _child(data: bytes, kind: str, cancel=None) -> dict:
    worker = Path(__file__).with_name("_pdf_worker.py" if kind == "pdf" else "_office_worker.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(worker), kind],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
        )
    except OSError:
        raise MaterialInvalid("the document reader is unavailable") from None
    output = bytearray()
    cursor = 0
    deadline = clock.monotonic() + 20
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(proc.stdin.fileno(), False)
            os.set_blocking(proc.stdout.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE)
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if (cancel is not None and cancel.is_set()) or clock.monotonic() >= deadline:
                    raise MaterialInvalid("the document reader exceeded its limit")
                for key, _ in selector.select(min(0.2, max(0, deadline - clock.monotonic()))):
                    if key.fileobj is proc.stdin:
                        try:
                            cursor += os.write(key.fd, data[cursor : cursor + 65536])
                        except BrokenPipeError:
                            cursor = len(data)
                        if cursor == len(data):
                            selector.unregister(proc.stdin)
                            proc.stdin.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(proc.stdout)
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise MaterialInvalid("the document reader exceeded its limit")
            if proc.wait(timeout=max(0.01, deadline - clock.monotonic())) != 0:
                raise MaterialInvalid("the document contains unsupported or unreadable content")
        return json.loads(output)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise MaterialInvalid("the document contains unsupported or unreadable content") from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if not proc.stdin.closed:
            proc.stdin.close()
        proc.stdout.close()


def parse_file(data: bytes, mime: str, filename: str = "", *, cancel=None) -> ParsedMaterial:
    if cancel is not None and cancel.is_set():
        raise MaterialInvalid("the document read was cancelled")
    kind = MIMES.get(mime.split(";", 1)[0].strip().lower())
    if kind is None:
        raise MaterialInvalid("unsupported document format")
    if type(data) is not bytes or not 0 < len(data) <= MAX_FILE:
        raise MaterialInvalid("the document is empty or exceeds its limit")
    digest = hashlib.sha256(data).hexdigest()
    parts = []

    def add(text, locator):
        if type(text) is not str or not text.strip():
            raise MaterialInvalid("the document contains an unresolved nontext part")
        text = clean_text(text).strip()
        if not text or len(text) > MAX_BLOCK or len(parts) >= MAX_PARTS:
            raise MaterialInvalid("a document part exceeds its limit")
        parts.append(
            MaterialPart(
                f"part:{len(parts) + 1}",
                text,
                TextSourceFacts(digest, locator, SourceTimeFacts("absent")),
            )
        )

    if kind in ("text", "markdown"):
        try:
            data.decode("utf-8")
            for start, end in _sections(data, kind == "markdown", ReadBudget()):
                if data[start:end].strip():
                    add(
                        data[start:end].decode("utf-8"),
                        SourceLocator("bytes", byte_start=start, byte_end=end),
                    )
        except (UnicodeError, ValueError):
            raise MaterialInvalid("the document is not supported UTF-8 text") from None
    elif kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise MaterialInvalid("the document format does not match its bytes")
        response = _child(data, kind, cancel)
        pages = response.get("pages")
        if type(pages) is not list or not 0 < len(pages) <= MAX_PARTS:
            raise MaterialInvalid("the document has invalid pages")
        for page, text in enumerate(pages, 1):
            add(text, SourceLocator("page", page=page))
    else:
        response = _child(data, kind, cancel)
        blocks = response.get("blocks")
        if type(blocks) is not list or not 0 < len(blocks) <= MAX_PARTS:
            raise MaterialInvalid("the document has invalid blocks")
        for block in blocks:
            if type(block) is not dict or set(block) != {"text", "block", "slide"}:
                raise MaterialInvalid("the document has invalid blocks")
            add(
                block["text"],
                SourceLocator(
                    "block" if kind == "docx" else "slide_block",
                    block=block["block"],
                    slide=block["slide"],
                ),
            )
    if not parts:
        raise MaterialInvalid("the document contains no text")
    parser = "document.text.v1" if kind in ("text", "markdown") else f"document.{kind}.v1"
    return ParsedMaterial(kind, digest, parser, tuple(parts))
