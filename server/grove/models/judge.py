"""The verification judges (D4, "Verification judges"; D15): fixed prompts that code runs.

The judge is code, never an agent's own reasoning (D11). It takes one claim and the excerpts of
its evidence, builds the fixed prompt through the one prompt builder (the claim and each excerpt in
a labelled data block, so stored text never reaches the instruction part), asks for a verdict per
excerpt as constrained JSON, and records each verdict as a `judgments` row of the call it came
from. That record is what verify reads: an agent can cite a verdict and never make one up, so this
module and nothing else outside the writer calls `record_judgments`.

The first judge is Spark. Plain verification waits at the day's ceiling; a load-bearing claim keeps
its own job, which continues, because a wrong load-bearing claim costs more than the call (D15).
When sources conflict the judge runs at high effort; otherwise at its job's own.

The second judge, for a load-bearing claim, is Glimmer: a different model keeps the two judges'
errors apart. When Glimmer cannot be reached, a Spark call on the second prompt stands in, and its
verdicts are recorded as correlated, so verify counts them only as the same-model stand-in that
they are (D15).
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from neo4j import Driver

from grove import clock
from grove.models.errors import (
    EgressRefused,
    InvalidOutput,
    NotConfigured,
    ProviderError,
    RateLimited,
)
from grove.models.layer import ModelLayer, Result
from grove.models.shape import Usage
from grove.prompts import DataBlock, build_prompt
from grove.store.query import WorldQuery
from grove.writer.judgments import JudgmentRow, record_judgments

FIRST_PROMPT = "grove.judge.v1"
SECOND_PROMPT = "grove.judge-second.v1"

JOB = "verification"
JOB_LOAD_BEARING = "verification_load_bearing"
JOB_SECOND = "second_opinion_load_bearing"
JOB_SECOND_CORRELATED = "second_opinion_correlated"
CONFLICT_EFFORT = "high"

FIRST_JUDGE = "first_judge"
SECOND_JUDGE = "second_judge"
VERDICTS = ("supports", "contradicts", "not_about_claim")

# An excerpt is what the judge reads of one piece: enough for the claim's sentence and its
# context, small enough that a batch of pieces stays a small call (D4: a judgment is about the
# size of an extraction).
EXCERPT_LIMIT = 4000
# The judgments writer stores at most this many rows for one call.
MAX_PIECES = 200

# Glimmer unreachable: not set up, refused, rate-limited past the retries, or off the allowlist.
# A wrong answer (InvalidOutput) is no reason to ask another model: it raises.
UNREACHABLE = (NotConfigured, ProviderError, RateLimited, EgressRefused)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "confidence": {"type": "number"},
                },
                "required": ["block", "verdict", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_ITEM = (
    "MATCH (i:Item) WHERE i.id = $id "
    "RETURN i.title AS title, i.kind AS kind, i.status AS status, i.properties AS properties"
)
_LINK = (
    "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.id = $id "
    "RETURN a.title AS from_title, l.meaning AS meaning, b.title AS to_title"
)
_KIND_NAME = "MATCH (k:Kind) WHERE k.id = $id RETURN k.name AS name"
_MEANING_NAME = "MATCH (m:Meaning) WHERE m.id = $id RETURN m.name AS name"
_SOURCE = "MATCH (s:Source) WHERE s.id = $id RETURN s.content AS content"
_EVIDENCE = (
    "MATCH (e:Evidence) WHERE e.id = $id RETURN e.source_id AS source_id, e.locator AS locator, "
    "e.speaker_kind AS speaker_kind, e.source_type AS source_type"
)
_LINES = re.compile(r"(\d+)(?:-(\d+))?")
_CHARS = re.compile(r"(\d+)-(\d+)")


class JudgeInputError(ValueError):
    """The judge cannot be asked: no such claim or source, or a locator that does not resolve.
    Raised before any call, so nothing is billed. The message names the field, never a value."""


class VerdictsMismatch(InvalidOutput):
    """The answer fit the schema but not the blocks: a piece with no verdict or with two, a block
    that holds no piece, or a confidence outside 0 to 1. The call is recorded; no judgment is."""


@dataclass(frozen=True)
class Piece:
    """One piece of evidence as the judge reads it: an evidence row, or, for a fetch that has no
    row yet, the stored source and the locator read (S46)."""

    text: str
    speaker_kind: str
    source_type: str
    evidence_id: str | None = None
    source_id: str | None = None
    locator: str | None = None

    def __post_init__(self) -> None:
        by_row = self.evidence_id is not None
        by_source = self.source_id is not None or self.locator is not None
        whole_source = self.source_id is not None and self.locator is not None
        if by_row == by_source or (by_source and not whole_source):
            raise JudgeInputError("a piece names an evidence row, or a source and a locator")

    @property
    def ref(self) -> str:
        """The id in the block's start tag: the evidence row, else the source."""
        return str(self.evidence_id if self.evidence_id is not None else self.source_id)


@dataclass(frozen=True)
class Verdict:
    piece: Piece
    block: int
    verdict: str
    confidence: float
    judgment_id: str


@dataclass(frozen=True)
class Judged:
    """One judge call: its model_calls row and the judgments rows it recorded, in piece order."""

    model_call_id: str
    model: str
    effort: str | None
    job: str
    role: str
    correlated: bool
    subject: str
    verdicts: tuple[Verdict, ...]
    usage: Usage

    @property
    def judgment_ids(self) -> tuple[str, ...]:
        return tuple(verdict.judgment_id for verdict in self.verdicts)


# Read helpers ---------------------------------------------------------------------------------


def _claim(query: WorldQuery, claim_id: str) -> tuple[str, str]:
    """(claim type, the claim in words). An item is read with its kind's name, its status and its
    properties, so the judge sees what the claim says, not only its title."""
    items = query.read(_ITEM, id=claim_id)
    if items:
        row = items[0]
        kind = query.read(_KIND_NAME, id=row["kind"])
        lines = [row["title"] or "", f"kind: {kind[0]['name'] if kind else row['kind']}"]
        if row.get("status") is not None:
            lines.append(f"status: {row['status']}")
        properties = json.loads(row.get("properties") or "{}")
        if properties:
            shown = json.dumps(properties, ensure_ascii=False, sort_keys=True)
            lines.append(f"properties: {shown}")
        return "item", "\n".join(lines)
    links = query.read(_LINK, id=claim_id)
    if links:
        row = links[0]
        meaning = query.read(_MEANING_NAME, id=row["meaning"])
        name = meaning[0]["name"] if meaning else row["meaning"]
        return "link", f"{row['from_title']} -[{name}]-> {row['to_title']}"
    raise JudgeInputError("claim_id names no item or link of this world")


def claim_text(query: WorldQuery, claim_id: str) -> str:
    """The claim as the judge reads it in block 1."""
    return _claim(query, claim_id)[1]


def cut(content: str, locator: str) -> str:
    """The part of a stored text that a locator names, capped at EXCERPT_LIMIT characters.
    `line:N` and `line:N-M` are 1-based and inclusive, `char:A-B` is a slice, `key:<token>` the
    line that holds the token, and `whole` the text itself."""
    scheme, _, rest = locator.partition(":") if isinstance(locator, str) else ("", "", "")
    if scheme == "whole" and not rest:
        text = content
    elif scheme == "line" and (found := _LINES.fullmatch(rest)):
        lines = content.splitlines()
        first, last = int(found.group(1)), int(found.group(2) or found.group(1))
        if not 1 <= first <= last <= len(lines):
            raise JudgeInputError("the line locator is outside the source")
        text = "\n".join(lines[first - 1 : last])
    elif scheme == "char" and (found := _CHARS.fullmatch(rest)):
        start, end = int(found.group(1)), int(found.group(2))
        if not 0 <= start < end <= len(content):
            raise JudgeInputError("the char locator is outside the source")
        text = content[start:end]
    elif scheme == "key" and rest:
        held = [line for line in content.splitlines() if rest in line]
        if not held:
            raise JudgeInputError("the key does not occur in the source")
        text = held[0]
    else:
        raise JudgeInputError("the locator is line:, char:, key: or whole")
    return text[:EXCERPT_LIMIT]


def excerpt(query: WorldQuery, source_id: str, locator: str) -> str:
    """The excerpt of a stored source that a locator names. A source held as a pointer has no
    stored text to cut, so it gives no excerpt; the lane fetches it first (D4: fetching is code)."""
    rows = query.read(_SOURCE, id=source_id)
    if not rows:
        raise JudgeInputError("source_id names no source of this world")
    content = rows[0]["content"]
    if content is None:
        raise JudgeInputError("a source held as a pointer has no stored text to read")
    return cut(content, locator)


def evidence_piece(query: WorldQuery, evidence_id: str) -> Piece:
    """A piece read from a stored evidence row: its excerpt, speaker kind and source type."""
    rows = query.read(_EVIDENCE, id=evidence_id)
    if not rows:
        raise JudgeInputError("evidence_id names no evidence row of this world")
    row = rows[0]
    return Piece(
        text=excerpt(query, row["source_id"], row["locator"]),
        speaker_kind=row["speaker_kind"],
        source_type=row["source_type"],
        evidence_id=evidence_id,
    )


# The calls ------------------------------------------------------------------------------------


def _blocks(claim_id: str, text: str, pieces: list[Piece]) -> list[DataBlock]:
    claim = DataBlock(kind="item", speaker="automated", source_type=None, ref=claim_id, text=text)
    return [claim] + [
        DataBlock(
            kind="source",
            speaker=piece.speaker_kind,
            source_type=piece.source_type,
            ref=piece.ref,
            text=piece.text,
        )
        for piece in pieces
    ]


def _checked_pieces(pieces: object) -> list[Piece]:
    if not isinstance(pieces, list | tuple) or not 1 <= len(pieces) <= MAX_PIECES:
        raise JudgeInputError(f"a judge call reads from 1 to {MAX_PIECES} pieces")
    if any(type(piece) is not Piece for piece in pieces):
        raise JudgeInputError("each piece is a Piece")
    return list(pieces)


def _by_block(result: Result, count: int) -> dict[int, dict[str, Any]]:
    """Each piece block's one verdict. Blocks 2 to count + 1 hold the pieces; block 1 the claim."""
    expected = set(range(2, count + 2))
    seen: dict[int, dict[str, Any]] = {}
    doubled, unknown, bad_confidence = set(), set(), set()
    for entry in result.data["verdicts"]:
        block = entry["block"]
        if block not in expected:
            unknown.add(block)
        elif block in seen:
            doubled.add(block)
        else:
            seen[block] = entry
        confidence = entry["confidence"]
        if not (math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
            bad_confidence.add(block)
    missing = expected - set(seen)
    if missing or doubled or unknown or bad_confidence:
        raise VerdictsMismatch(
            "the verdicts do not give each piece exactly one verdict",
            details={
                "model_call_id": result.model_call_id,
                "missing": sorted(missing),
                "doubled": sorted(doubled),
                "unknown": sorted(unknown),
                "confidence_out_of_range": sorted(bad_confidence),
            },
        )
    return seen


def _record(
    driver: Driver,
    world_id: str,
    result: Result,
    *,
    job: str,
    role: str,
    subject: str,
    pieces: list[Piece],
    correlated: bool,
) -> Judged:
    entries = _by_block(result, len(pieces))
    at = clock.now(world_id)
    rows = [
        JudgmentRow(
            role=role,
            subject=subject,
            verdict=entries[n]["verdict"],
            at=at,
            evidence_id=piece.evidence_id,
            source_id=None if piece.evidence_id is not None else piece.source_id,
            locator=None if piece.evidence_id is not None else piece.locator,
            stated_confidence=float(entries[n]["confidence"]),
            correlated=correlated,
        )
        for n, piece in enumerate(pieces, start=2)
    ]
    ids = record_judgments(driver, world_id, result.model_call_id, rows)
    verdicts = tuple(
        Verdict(
            piece=piece,
            block=n,
            verdict=entries[n]["verdict"],
            confidence=float(entries[n]["confidence"]),
            judgment_id=judgment_id,
        )
        for (n, piece), judgment_id in zip(enumerate(pieces, start=2), ids, strict=True)
    )
    return Judged(
        model_call_id=result.model_call_id,
        model=result.model,
        effort=result.effort,
        job=job,
        role=role,
        correlated=correlated,
        subject=subject,
        verdicts=verdicts,
        usage=result.usage,
    )


def judge(
    layer: ModelLayer,
    driver: Driver,
    world_id: str,
    *,
    claim_id: str,
    pieces: list[Piece],
    run_id: str | None = None,
    conflict: bool = False,
    load_bearing: bool = False,
) -> Judged:
    """The first judge on Spark: one call, one recorded judgment per piece."""
    pieces = _checked_pieces(pieces)
    claim_type, text = _claim(WorldQuery(driver, world_id), claim_id)
    prompt = build_prompt(FIRST_PROMPT, _blocks(claim_id, text, pieces))
    job = JOB_LOAD_BEARING if load_bearing else JOB
    result = layer.respond(
        world_id,
        job=job,
        instructions=prompt.instructions,
        input=prompt.data,
        schema=SCHEMA,
        schema_name="verdicts",
        effort=CONFLICT_EFFORT if conflict else None,
        run_id=run_id,
    )
    return _record(
        driver,
        world_id,
        result,
        job=job,
        role=FIRST_JUDGE,
        subject=f"{claim_type}:{claim_id}",
        pieces=pieces,
        correlated=False,
    )


def second_judge(
    layer: ModelLayer,
    driver: Driver,
    world_id: str,
    *,
    claim_id: str,
    pieces: list[Piece],
    run_id: str | None = None,
) -> Judged:
    """The second judge of a load-bearing claim: Glimmer, or, when Glimmer cannot be reached, the
    same second prompt on Spark, recorded as correlated."""
    pieces = _checked_pieces(pieces)
    claim_type, text = _claim(WorldQuery(driver, world_id), claim_id)
    prompt = build_prompt(SECOND_PROMPT, _blocks(claim_id, text, pieces))
    subject = f"{claim_type}:{claim_id}"
    try:
        result = layer.chat(
            world_id,
            job=JOB_SECOND,
            messages=prompt.messages(),
            schema=SCHEMA,
            schema_name="verdicts",
            run_id=run_id,
        )
    except UNREACHABLE:
        result = layer.respond(
            world_id,
            job=JOB_SECOND_CORRELATED,
            instructions=prompt.instructions,
            input=prompt.data,
            schema=SCHEMA,
            schema_name="verdicts",
            run_id=run_id,
        )
        job, correlated = JOB_SECOND_CORRELATED, True
    else:
        job, correlated = JOB_SECOND, False
    return _record(
        driver,
        world_id,
        result,
        job=job,
        role=SECOND_JUDGE,
        subject=subject,
        pieces=pieces,
        correlated=correlated,
    )
