"""The model-call layer's other write: the verdicts of one judge call, as `judgments` rows (S20).

Only grove.models calls `record_judgments`, right after the call it judged was recorded. A
judgment names the call that made it, the role it played, the subject it judged (S32), and the
evidence row it read, or, for a fetch that has no evidence row yet, the stored source and the
locator it read (S46). The writer reads verdicts only from these rows, so an agent can cite a
verdict and never make one up (D4).
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from neo4j import Driver

from grove.ids import new_id
from grove.store.enums import ENUMS
from grove.writer.tx import WorldTx, WorldWriter

_ROLES = frozenset(ENUMS["judgment_role"])
_VERDICTS = frozenset(ENUMS["verdict"])
_SUBJECT = re.compile(
    r"(item|link|belief|value|pair|kind|meaning|property_name):[0-9A-Za-z_:|-]{1,300}"
)
_MAX_ROWS = 200

_CALL = "MATCH (c:ModelCall {id: $id}) RETURN c.outcome AS outcome, c.model AS model"
_EVIDENCE = "MATCH (e:Evidence) WHERE e.id IN $ids RETURN e.id AS id"
_SOURCES = "MATCH (s:Source) WHERE s.id IN $ids RETURN s.id AS id"
_ADD = "UNWIND $rows AS row CREATE (j:Judgment) SET j += row"


class JudgmentRefused(ValueError):
    """A judgment the writer will not store. The message names the field, never a value."""


@dataclass(frozen=True)
class JudgmentRow:
    role: str
    subject: str
    verdict: str
    at: datetime
    evidence_id: str | None = None
    source_id: str | None = None
    locator: str | None = None
    stated_confidence: float | None = None
    correlated: bool = False


def _check(row: JudgmentRow) -> dict:
    if row.role not in _ROLES:
        raise JudgmentRefused("role is not a judgment role")
    if row.verdict not in _VERDICTS:
        raise JudgmentRefused("verdict is not in the verdict enum")
    if not isinstance(row.subject, str) or not _SUBJECT.fullmatch(row.subject):
        raise JudgmentRefused("subject is a reference of the form <type>:<id> (S32)")
    if not isinstance(row.at, datetime) or row.at.utcoffset() is None:
        raise JudgmentRefused("at is an aware time from the clock provider")
    if row.evidence_id is not None and (row.source_id is not None or row.locator is not None):
        raise JudgmentRefused("a judgment names an evidence row or a source and a locator")
    if (row.source_id is None) != (row.locator is None):
        raise JudgmentRefused("a source and a locator go together")
    confidence = row.stated_confidence
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise JudgmentRefused("stated_confidence is a number")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise JudgmentRefused("stated_confidence runs from 0 to 1")
    if not isinstance(row.correlated, bool):
        raise JudgmentRefused("correlated is true or false")
    fields = {
        "role": row.role,
        "subject": row.subject,
        "verdict": row.verdict,
        "at": row.at.astimezone(UTC),
        "evidence_id": row.evidence_id,
        "source_id": row.source_id,
        "locator": row.locator,
        "stated_confidence": None if confidence is None else float(confidence),
        # Stored only when true, as model_calls stores usage_estimated (S16: no empty values).
        "correlated": True if row.correlated else None,
    }
    return {key: value for key, value in fields.items() if value is not None}


def record_judgments(
    driver: Driver, world_id: str, model_call_id: str, rows: list[JudgmentRow]
) -> list[str]:
    """Store the verdicts of one recorded call in one transaction, and return their ids in
    order. The call must be a recorded call of this world that answered (`ok`); a cited evidence
    row or source must be of this world."""
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_ROWS:
        raise JudgmentRefused(f"one call records from 1 to {_MAX_ROWS} judgments")
    checked = [_check(row) for row in rows]

    def work(tx: WorldTx) -> list[str]:
        found = tx.run(_CALL, id=model_call_id)
        if not found:
            raise JudgmentRefused("no such model call in this world")
        if found[0]["outcome"] != "ok":
            raise JudgmentRefused("only a call that answered records judgments")
        evidence = sorted({row["evidence_id"] for row in checked if "evidence_id" in row})
        if evidence and len(tx.run(_EVIDENCE, ids=evidence)) != len(evidence):
            raise JudgmentRefused("a judged evidence row is not in this world")
        sources = sorted({row["source_id"] for row in checked if "source_id" in row})
        if sources and len(tx.run(_SOURCES, ids=sources)) != len(sources):
            raise JudgmentRefused("a judged source is not in this world")
        ids = [new_id(world_id) for _ in checked]
        tx.run(
            _ADD,
            rows=[
                {**row, "id": row_id, "model_call_id": model_call_id}
                for row_id, row in zip(ids, checked, strict=True)
            ],
        )
        return ids

    return WorldWriter(driver, world_id).transaction(work)
