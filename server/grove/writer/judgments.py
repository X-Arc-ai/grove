"""The judge's verdicts (S20, B.1 judgments): written only by the model-call layer, from the
constrained output of one judge call, and read by the writer at every gate that needs a verdict.

`record_judgments` stores the verdicts of one recorded model call in one transaction. A subject is
an S32 reference: `<target type>:<id>`, or a pair of two references in sorted order joined by `|`.
A word with no row yet is `value:<history value id>`, and `store_subject_value` writes that value
first. Nothing here reads a clock but the clock provider.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from neo4j import Driver

from grove import clock
from grove.ids import new_id
from grove.store.enums import ENUMS
from grove.store.normalise import reads_blank
from grove.writer.history import compact
from grove.writer.tx import WorldTx, WorldWriter

# S32's reference: a lower-case type, a colon, then an id or a code (a UUIDv7, an S32 event key).
_REF = re.compile(r"[a-z_]{1,40}:[A-Za-z0-9_.:\-]{1,200}")
MAX_SUBJECT_VALUE = 4000

_CALL = "MATCH (c:ModelCall {id: $id}) RETURN c.id AS id"
_ADD = "CREATE (j:Judgment) SET j += $row"
_ADD_VALUE = "CREATE (v:HistoryValue) SET v += $row"


class JudgmentRefused(ValueError):
    """A judgment the writer will not store. The message names the field, never a value."""


@dataclass(frozen=True)
class JudgmentRow:
    role: str
    subject: str
    verdict: str
    stated_confidence: float | None = None
    correlated: bool = False
    evidence_id: str | None = None


def is_subject(subject: object) -> bool:
    """A reference, or two references in sorted order joined by `|` (S32)."""
    if not isinstance(subject, str):
        return False
    parts = subject.split("|")
    if len(parts) > 2 or any(_REF.fullmatch(part) is None for part in parts):
        return False
    return len(parts) == 1 or (parts == sorted(parts) and parts[0] != parts[1])


def _check(row: JudgmentRow) -> dict:
    if row.role not in ENUMS["judgment_role"]:
        raise JudgmentRefused("role is not in the judgment_role enum")
    if row.verdict not in ENUMS["verdict"]:
        raise JudgmentRefused("verdict is not in the verdict enum")
    if not is_subject(row.subject):
        raise JudgmentRefused("subject is not an S32 reference or a sorted pair of two")
    confidence = row.stated_confidence
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise JudgmentRefused("stated_confidence is a number from 0 to 1")
    if not isinstance(row.correlated, bool):
        raise JudgmentRefused("correlated is true or false")
    if row.evidence_id is not None and _REF.fullmatch(f"evidence:{row.evidence_id}") is None:
        raise JudgmentRefused("evidence_id is an id")
    return {
        "role": row.role,
        "subject": row.subject,
        "verdict": row.verdict,
        "stated_confidence": None if confidence is None else float(confidence),
        "correlated": row.correlated,
        "evidence_id": row.evidence_id,
    }


def record_judgments(
    driver: Driver, world_id: str, model_call_id: str, rows: Sequence[JudgmentRow]
) -> list[str]:
    """Store the verdicts of one recorded call, in order, in one transaction; returns their ids.
    The call must be a model_calls row of this world, so a verdict never stands without the call
    that produced it."""
    if not rows:
        raise JudgmentRefused("a judge call records at least one verdict")
    checked = [_check(row) for row in rows]

    def work(tx: WorldTx) -> list[str]:
        if not tx.run(_CALL, id=model_call_id):
            raise JudgmentRefused("no such model call in this world")
        at = clock.now(world_id)
        ids = []
        for columns in checked:
            judgment_id = new_id(world_id)
            tx.run(
                _ADD,
                row=compact(
                    {"id": judgment_id, "model_call_id": model_call_id, **columns, "at": at}
                ),
            )
            ids.append(judgment_id)
        return ids

    return WorldWriter(driver, world_id).transaction(work)


def store_subject_value(driver: Driver, world_id: str, value: str) -> str:
    """S32: a word with no row yet is judged as `value:<history value id>`. The value is the
    proposed name, or a short JSON text the judge path builds; returns the reference."""
    if not isinstance(value, str) or reads_blank(value) or len(value) > MAX_SUBJECT_VALUE:
        raise JudgmentRefused("a subject value is non-empty text of at most 4000 characters")

    def work(tx: WorldTx) -> str:
        value_id = new_id(world_id)
        tx.run(_ADD_VALUE, row={"id": value_id, "value": value})
        return f"value:{value_id}"

    return WorldWriter(driver, world_id).transaction(work)
