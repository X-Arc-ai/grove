"""verify (D4): the only operation that moves last confirmed, and the confirmation at create.

A caller cites, per piece of evidence, the judgment that judged it (or, for the person, the answer
they chose). The writer reads each verdict from the judgments table, and computes from stored rows
only: the capability that lets the piece confirm the claim, each piece's weight, the agreement
scores and the band, and whether the claim is load-bearing. A caller that sends any of these is
refused before the store is read (ops.py). Every check is appended to `verifications`.

The four conditions of a confirmation (spec lines 950 to 962):
1. a supporting piece is admitted only when its row is observed and its source type confirmed;
2. a capability row, or the default for the claim's declared fields, lets its source confirm;
3. agree is at least 1.0 and the ratio at least 0.8 (the band `confirm`);
4. a load-bearing claim also has a second judge that agrees, from another model or recorded as a
   correlated stand-in (D15).

Last confirmed moves to the time the evidence speaks for: the newest admitted supporting piece.
"""

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from grove import clock
from grove.cleanup.access import Access, CleanupRefused, hold
from grove.ids import new_id
from grove.store import capabilities as caps
from grove.store.current import has_ended
from grove.store.primitives import KIND_BELIEF
from grove.writer.clocks import parse_instant
from grove.writer.errors import Refused
from grove.writer.history import Stamp, add_entry, compact, encode
from grove.writer.provenance import calendar_logged, connector_type_of
from grove.writer.tx import WorldWriter

if TYPE_CHECKING:
    from grove.cleanup.legwork import NoSourceDeferralV1
    from grove.writer.apply import _Plan, _Run

# Appendix C: weight 1.0 when newer than the last confirmation, falling to 0.3 at the staleness
# limit and 0 beyond it; confirmed at agree 1.0 and ratio 0.8; a contradiction row from 0.5.
FULL_WEIGHT = 1.0
FALL = 0.7
CONFIRM_AGREE = 1.0
CONFIRM_RATIO = 0.8
CONTRADICTION_RATIO = 0.5
_EPSILON = 1e-9
_EXTRACTION_JOBS = frozenset({"extraction", "backfill_extraction"})

# S52: the bands of line 957, as codes.
CONFIRM = "confirm"
CONTRADICTION = "contradiction"
AGAINST = "against"
ZERO_SIGNAL = "zero_signal"
STALE_SUPPORT = "stale_support"
# The dispositions each band allows with no override reason. `confirmed` needs the band confirm,
# always; `retired` is about mattering, not truth, so any band allows it.
_ALLOWED = {
    CONFIRM: {"confirmed", "retired"},
    CONTRADICTION: {"deferred", "retired"},
    AGAINST: {"moved", "deferred", "retired"},
    ZERO_SIGNAL: {"deferred", "retired"},
    STALE_SUPPORT: {"deferred", "retired"},
}


@dataclass
class Piece:
    """One piece of evidence as the writer judged it."""

    index: int
    columns: dict[str, Any]
    connector_type: str
    connector: dict[str, Any]
    fetched_at: datetime | None
    evidence_id: str | None
    judgment: dict[str, Any] | None
    answer: str | None
    plan: "_Plan | None" = None
    weight: float = 0.0
    capability: str = caps.NONE
    capability_row: str = ""
    admitted: bool = False
    second: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.answer is not None:
            return self.answer
        return (self.judgment or {}).get("verdict") or "not_about_claim"

    @property
    def spoken_for(self) -> datetime:
        return self.columns["source_time"]


# Scores --------------------------------------------------------------------------------------


def weight(
    spoken_for: datetime, reference: datetime | None, now: datetime, limit_days: int | None
) -> float:
    """D4 condition 3: 1.0 when the piece is newer than the reference (the claim's last
    confirmation, or its recorded time); else 1.0 - 0.7 * age / limit within the limit, 0
    beyond it. An always-fresh source (no limit) weighs 1.0 at any age."""
    if limit_days is None:
        return FULL_WEIGHT
    if reference is None or spoken_for > reference:
        return FULL_WEIGHT
    age = max((now - spoken_for).total_seconds(), 0.0) / 86400.0
    if limit_days <= 0 or age > limit_days:
        return 0.0
    return FULL_WEIGHT - FALL * age / limit_days


def band(agree: float, disagree: float) -> tuple[float | None, str]:
    """(ratio, band). ratio = agree / (agree + disagree), the engine's own definition."""
    if agree <= _EPSILON and disagree <= _EPSILON:
        return None, ZERO_SIGNAL
    ratio = agree / (agree + disagree)
    if ratio >= CONFIRM_RATIO - _EPSILON:
        return ratio, CONFIRM if agree >= CONFIRM_AGREE - _EPSILON else STALE_SUPPORT
    if ratio >= CONTRADICTION_RATIO - _EPSILON:
        return ratio, CONTRADICTION
    return ratio, AGAINST


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


# Connector types whose entries are dated ahead by nature: a calendar entry or a reminder for a
# later day. Such a row is planned, so it is never admitted and never weighs (D5).
_DATED_AHEAD = frozenset({"calendar", "reminders"})


def after_its_read(
    columns: dict[str, Any], source: dict[str, Any], connector_type: str, now: datetime
) -> bool:
    """True when a row is dated after now, or, outside the calendar and reminders, after the
    moment its source was read (`sources.received_at`, the fetch time)."""
    spoken_for = columns["source_time"]
    if spoken_for > now:
        return True
    received = source.get("received_at")
    return connector_type not in _DATED_AHEAD and received is not None and spoken_for > received


# Reads inside the batch -----------------------------------------------------------------------


def self_item(run: "_Run") -> str | None:
    if "self_item" not in run.cache:
        rows = (
            run.tx.unscoped()
            .run(
                "CYPHER 25 MATCH (w:World {id: $world_id}) RETURN w.self_item_id AS own",
                world_id=run.stamp.world_id,
            )
            .data()
        )
        run.cache["self_item"] = rows[0]["own"] if rows else None
    return run.cache["self_item"]


def _consequence_meanings(run: "_Run") -> list[str]:
    return [
        meaning_id
        for meaning_id, meaning in run.registry.meanings.items()
        if meaning.get("carries_consequences")
    ]


def load_bearing(run: "_Run", claim_type: str, claim: dict[str, Any]) -> bool:
    """D4 condition 4, from declared fields and stored links only: a claim with active
    consequence links into or out of it (open work among them), a link that carries
    consequences, and the person's core facts (state links on the person's own item)."""
    meanings = _consequence_meanings(run)
    if claim_type == "link":
        meaning = run.registry.meanings.get(claim.get("meaning"), {})
        if meaning.get("carries_consequences"):
            return True
        own = self_item(run)
        return (
            meaning.get("temporal_class") == "state"
            and own is not None
            and own in (claim.get("from_item"), claim.get("to_item"))
        )
    if not meanings:
        return False
    rows = run.tx.run(
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE (a.id = $id OR b.id = $id) "
        "AND l.meaning IN $meanings AND l.active_key IS NOT NULL RETURN count(l) AS n",
        id=claim["id"],
        meanings=meanings,
    )
    return bool(rows and rows[0]["n"])


def _claim_fields(run: "_Run", claim_type: str, claim: dict[str, Any]) -> caps.Claim:
    senders = frozenset(
        row["sender"]
        for row in run.tx.run(
            "MATCH (e:Evidence {target_id: $id, active: true}) WHERE e.sender IS NOT NULL "
            "RETURN DISTINCT e.sender AS sender",
            id=claim["id"],
        )
    )
    if claim_type == "link":
        meaning = run.registry.meanings.get(claim.get("meaning"), {})
        return caps.Claim(
            "link",
            claim.get("meaning", ""),
            temporal_class=meaning.get("temporal_class"),
            senders=senders,
        )
    kind = (
        run.registry.kind(KIND_BELIEF)
        if claim.get("kind") == KIND_BELIEF
        else run.registry.kinds.get(claim.get("kind"), {})
    )
    lifecycle = kind.get("lifecycle_class")
    others = False
    own = self_item(run)
    if lifecycle == "work_with_state" and own is not None:
        own_kind = run.tx.run("MATCH (i:Item {id: $id}) RETURN i.kind AS kind", id=own)
        if own_kind:
            people = run.tx.run(
                "MATCH (w:Item {id: $id})-[l:LINK]->(p:Item {kind: $kind}) "
                "WHERE l.active_key IS NOT NULL RETURN p.id AS id",
                id=claim["id"],
                kind=own_kind[0]["kind"],
            )
            linked = {row["id"] for row in people}
            others = bool(linked) and own not in linked
    return caps.Claim(
        "item",
        claim.get("kind", ""),
        lifecycle_class=lifecycle,
        senders=senders,
        others_work=others,
    )


def _specific_rows(run: "_Run", connector_type: str, registry_id: str) -> tuple[list, set]:
    rows = [
        row["c"]
        for row in run.tx.run(
            "MATCH (c:Capability {connector_type: $type, registry_id: $registry}) RETURN c "
            "ORDER BY c.id",
            type=connector_type,
            registry=registry_id,
        )
    ]
    wanted = [row["accepted_judgment_id"] for row in rows if row.get("accepted_judgment_id")]
    accepted: set[str] = set()
    if wanted:
        accepted = {
            row["id"]
            for row in run.tx.run(
                "MATCH (j:Judgment) WHERE j.id IN $ids AND j.role = 'field_fit' "
                "AND j.verdict = 'fits' RETURN j.id AS id",
                ids=wanted,
            )
        }
    return rows, accepted


def _source_and_connector(run: "_Run", source_id: str) -> tuple[dict, dict, str]:
    key = f"source:{source_id}"
    if key not in run.cache:
        source = run.tx.run("MATCH (s:Source {id: $id}) RETURN s", id=source_id)[0]["s"]
        connector = run.tx.run(
            "MATCH (c:Connector {id: $id}) RETURN c", id=source.get("connector_id")
        )[0]["c"]
        run.cache[key] = (source, connector, connector_type_of(connector))
    return run.cache[key]


def capability_of(
    run: "_Run", row: dict[str, Any], claim_type: str, claim: dict[str, Any]
) -> caps.Resolved:
    """Condition 2 for one evidence row and one claim."""
    _, connector, connector_type = _source_and_connector(run, row["source_id"])
    fields = _claim_fields(run, claim_type, claim)
    specific, accepted = _specific_rows(run, connector_type, fields.registry_id)
    return caps.resolve(row, connector, connector_type, fields, specific, accepted)


def check_calendar_confirmation(run: "_Run", row: dict[str, Any]) -> None:
    """Old confirming rows need the same trusted Calendar binding when cited again."""
    if row.get("source_type") != "confirmed":
        return
    source, connector, connector_type = _source_and_connector(run, row["source_id"])
    if connector_type == "calendar" and not calendar_logged(
        connector,
        source,
        row["source_time"],
        row.get("person_timezone"),
        message_key=row["message_key"],
        locator=row["locator"],
    ):
        raise Refused(
            "invalid_arguments",
            "calendar_binding",
            "Calendar confirmation needs trusted event facts",
            {},
        )


def can_confirm(run: "_Run", row: dict[str, Any], claim_type: str, claim_id: str) -> bool:
    """D5 rule 2's confirming row must also be a source that can prove the claim (D5's table)."""
    found = run.tx.run(
        "MATCH (i:Item {id: $id}) RETURN i AS row"
        if claim_type == "item"
        else "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l AS row",
        id=claim_id,
    )
    if not found:
        return False
    return capability_of(run, row, claim_type, found[0]["row"]).capability == caps.CONFIRMS


# The verify operation -------------------------------------------------------------------------


def _claim(run: "_Run", claim_id: str, *, lock: bool = True) -> tuple[str, dict[str, Any]]:
    from grove.writer import apply

    params = {"id": claim_id, **({"at": run.at} if lock else {})}
    items = run.tx.run(apply._LOCK_ITEM if lock else apply._READ_ITEM, **params)
    if items:
        row = items[0]["i"]
        if row.get("kind") == KIND_BELIEF:
            raise Refused(
                "invalid_arguments",
                "belief_retest",
                "a belief is re-tested through its own operations (D9), not verify",
                {"claim": claim_id},
            )
        return "item", row
    links = run.tx.run(apply._LOCK_LINK if lock else apply._READ_LINK, **params)
    if links:
        return "link", links[0]["l"]
    raise Refused("not_found", "claim_not_found", f"no item or link {claim_id}", {})


_JUDGMENTS = (
    "MATCH (j:Judgment) WHERE j.id IN $ids "
    "OPTIONAL MATCH (c:ModelCall {id: j.model_call_id}) "
    "RETURN j.id AS id, j.verdict AS verdict, j.role AS role, j.subject AS subject, "
    "j.evidence_id AS evidence_id, j.source_id AS source_id, j.locator AS locator, "
    "j.correlated AS correlated, j.model_call_id AS model_call_id, c.model AS model, "
    "c.run_id AS run_id"
)


def _read_judgments(run: "_Run", ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    found = {row["id"]: row for row in run.tx.run(_JUDGMENTS, ids=sorted(set(ids)))}
    missing = sorted(set(ids) - set(found))
    if missing:
        raise Refused(
            "unrecorded_verdict",
            "unrecorded_verdict",
            "a cited judgment is not a recorded judgment of this world",
            {"judgment_ids": missing},
        )
    return found


def _matches(judgment: dict[str, Any], piece_evidence: str | None, columns: dict) -> bool:
    if judgment.get("evidence_id") is not None:
        return judgment["evidence_id"] == piece_evidence
    return (
        judgment.get("source_id") == columns["source_id"]
        and judgment.get("locator") == columns["locator"]
    )


def _check_first(
    judgment: dict[str, Any],
    subject: str,
    piece: Piece,
    allowed_verdicts: frozenset[str] | None = None,
) -> None:
    if judgment.get("subject") != subject:
        raise Refused(
            "unrecorded_verdict",
            "judgment_subject",
            "a cited judgment did not judge this claim",
            {"judgment_id": judgment["id"], "subject": judgment.get("subject")},
        )
    if judgment.get("role") != "first_judge":
        raise Refused(
            "unrecorded_verdict",
            "judgment_role",
            "a piece cites the first judge's verdict; a second judge goes in second_judge",
            {"judgment_id": judgment["id"]},
        )
    if not _matches(judgment, piece.evidence_id, piece.columns):
        raise Refused(
            "unrecorded_verdict",
            "judgment_evidence",
            "a cited judgment judged another evidence row",
            {"judgment_id": judgment["id"]},
        )
    if judgment.get("verdict") not in (
        allowed_verdicts or frozenset({"supports", "contradicts", "not_about_claim"})
    ):
        raise Refused(
            "unrecorded_verdict",
            "judgment_verdict",
            "a verify judgment says supports, contradicts or not_about_claim (S33)",
            {"judgment_id": judgment["id"]},
        )


def _pieces(
    run: "_Run",
    op: dict[str, Any],
    subject: str,
    caller_kind: str | None,
    *,
    allowed_verdicts: frozenset[str] | None = None,
) -> list:
    from grove.writer import apply

    judgments = _read_judgments(
        run, [piece["judgment_id"] for piece in op["pieces"] if "judgment_id" in piece]
    )
    seen: set[str] = set()
    pieces = []
    for index, raw in enumerate(op["pieces"]):
        if "ev_id" in raw:
            columns = run.observation(raw["ev_id"])
            evidence_id = None
        else:
            columns = apply._existing_evidence(run, raw["evidence_id"])
            evidence_id = raw["evidence_id"]
        source, connector, connector_type = _source_and_connector(run, columns["source_id"])
        if after_its_read(columns, source, connector_type, run.at):
            # Evidence speaks for its own time, and a source cannot speak for a time after Grove
            # read it, nor after now: such a date would escape the staleness limit, set last
            # confirmed to a time the source never spoke for, and outweigh every real check.
            raise Refused(
                "invalid_arguments",
                "future_evidence",
                "a piece is dated after its source was read, or after now",
                {"piece": index},
            )
        judgment = None
        if "judgment_id" in raw:
            if raw["judgment_id"] in seen:
                raise Refused(
                    "unrecorded_verdict",
                    "judgment_twice",
                    "one judgment judged one piece",
                    {"judgment_id": raw["judgment_id"]},
                )
            seen.add(raw["judgment_id"])
            judgment = judgments[raw["judgment_id"]]
        elif caller_kind != "person" or connector_type != "person_in_views":
            # Only the person chooses an answer option, in Grove's views (D4, D11).
            raise Refused(
                "unrecorded_verdict",
                "answer_not_person",
                "an answer with no judgment is the person's own choice in Grove's views",
                {},
            )
        piece = Piece(
            index=index,
            columns=columns,
            connector_type=connector_type,
            connector=connector,
            fetched_at=source.get("received_at"),
            evidence_id=evidence_id,
            judgment=judgment,
            answer=raw.get("answer"),
        )
        if judgment is not None:
            _check_first(judgment, subject, piece, allowed_verdicts)
        pieces.append(piece)
    if caller_kind == "ai_host":
        for piece in pieces:
            relayed = piece.connector_type == "ai_host" and piece.columns["source_type"] == (
                "confirmed"
            )
            if not relayed:
                # H verifies only inside an answer or a correction in the person's words (D5).
                raise Refused(
                    "verbatim_required",
                    "verbatim_required",
                    "a host verifies only with the person's own words, in an answer or a "
                    "correction, and the id of the host message",
                    {},
                )
    return pieces


def _second(run: "_Run", op: dict[str, Any], subject: str, pieces: list[Piece]) -> bool:
    """Attach each second-judge verdict to its piece. Returns whether any is correlated."""
    rows = _read_judgments(run, list(op.get("second_judge") or []))
    correlated = False
    for row in rows.values():
        if row.get("subject") != subject or row.get("role") != "second_judge":
            raise Refused(
                "unrecorded_verdict",
                "second_judge_role",
                "a second judge is a recorded second_judge verdict on this claim",
                {"judgment_id": row["id"]},
            )
        target = next(
            (piece for piece in pieces if _matches(row, piece.evidence_id, piece.columns)), None
        )
        if target is None:
            raise Refused(
                "unrecorded_verdict",
                "judgment_evidence",
                "a second judge judged no piece of this verify",
                {"judgment_id": row["id"]},
            )
        first_model = (target.judgment or {}).get("model")
        if row.get("model") == first_model and not row.get("correlated"):
            # A second call of the first judge's model counts only as a recorded stand-in (D15).
            raise Refused(
                "second_judge_required",
                "same_model",
                "a second judge comes from another model, or is recorded as correlated",
                {"judgment_id": row["id"]},
            )
        correlated = correlated or bool(row.get("correlated"))
        target.second.append(row)
    return correlated


def _refuse_confirmation(pieces: list[Piece], band_code: str) -> None:
    supports = [piece for piece in pieces if piece.verdict == "supports"]
    if not supports:
        raise Refused(
            "judge_not_supports",
            "judge_not_supports",
            "no recorded verdict supports the claim: only supports confirms",
            {"verdicts": sorted({piece.verdict for piece in pieces})},
        )
    if not any(piece.admitted for piece in supports):
        if all(piece.columns["derivation"] != "observed" for piece in supports):
            raise Refused(
                "inferred_evidence",
                "inferred_piece",
                "an inferred or rolled-up row never confirms (D5)",
                {},
            )
        if all(piece.columns["source_type"] != "confirmed" for piece in supports):
            raise Refused(
                "not_confirmable",
                "source_type",
                "only a row of source type confirmed confirms; this source proves less",
                {"source_types": sorted({piece.columns["source_type"] for piece in supports})},
            )
        raise Refused(
            "not_confirmable",
            "no_capability",
            "no capability row or default lets this source confirm this claim",
            {"capabilities": sorted({piece.capability for piece in supports})},
        )
    raise Refused(
        "not_confirmable",
        f"band_{band_code}",
        f"the computed band is {band_code}: a confirmation needs agree 1.0 and ratio 0.8",
        {"band": band_code},
    )


def _second_judge_gate(pieces: list[Piece]) -> None:
    """Condition 4: every admitted supporting piece judged by a model has a second verdict that
    agrees. The person's own chosen answer needs none: the person is the judge."""
    judged = [piece for piece in pieces if piece.admitted and piece.answer is None]
    for piece in judged:
        if not piece.second:
            raise Refused(
                "second_judge_required",
                "second_judge_required",
                "a load-bearing confirmation needs an agreeing second judge",
                {},
            )
        if any(row.get("verdict") != "supports" for row in piece.second):
            raise Refused(
                "judges_disagree",
                "judges_disagree",
                "the two judges disagree: defer, or open a contradiction",
                {"verdicts": sorted({row.get("verdict") or "" for row in piece.second})},
            )


def _recheck_value(op: dict[str, Any]) -> datetime | None:
    value = op.get("recheck_by")
    return None if value is None else parse_instant(value)


def verify_op(run: "_Run", index: int, op: dict[str, Any]) -> None:
    from grove.writer import apply

    claim_id = run.ref(op["claim"], "any")
    run.summaries[index] = f"verify {run.label(op['claim'])}"
    # WorldWriter already holds the world lock. Read without a touched_at write until
    # the oscillation guard passes; a refused resolution must have no new effect.
    claim_type, claim = _claim(run, claim_id, lock=False)
    subject = f"{claim_type}:{claim_id}"
    disposition = op["disposition"]
    if run.caller_kind == "cleanup" and disposition in _RESOLUTIONS:
        _refuse_recent_resolution(run.tx, claim_type, claim_id, run.stamp.run_id)
    pieces = _pieces(run, op, subject, run.caller_kind)
    correlated = _second(run, op, subject, pieces)
    ended = has_ended(claim) if claim_type == "item" else not claim.get("active_key")
    if ended and disposition in ("confirmed", "deferred"):
        raise Refused(
            "ended_target",
            "ended_target",
            "an ended claim is not confirmed or deferred: its truth clock stands as it ended",
            {"claim": claim_id},
        )

    reference = claim.get("last_confirmed") or claim.get("recorded")
    now = run.at
    fields = _claim_fields(run, claim_type, claim)
    agree = disagree = 0.0
    for piece in pieces:
        specific, accepted = _specific_rows(run, piece.connector_type, fields.registry_id)
        resolved = caps.resolve(
            piece.columns, piece.connector, piece.connector_type, fields, specific, accepted
        )
        piece.capability, piece.capability_row = resolved.capability, resolved.row_id
        if piece.verdict not in ("supports", "contradicts"):
            continue
        piece.weight = weight(piece.spoken_for, reference, now, resolved.staleness_days)
        if piece.verdict == "contradicts":
            disagree += piece.weight
            continue
        piece.admitted = (
            piece.columns["derivation"] == "observed"
            and piece.columns["source_type"] == "confirmed"
            and resolved.capability == caps.CONFIRMS
        )
        if piece.admitted:
            agree += piece.weight
    ratio, band_code = band(agree, disagree)
    bearing = load_bearing(run, claim_type, claim)

    if disposition == "confirmed":
        if band_code != CONFIRM:
            _refuse_confirmation(pieces, band_code)
        if bearing:
            _second_judge_gate(pieces)
    elif disposition not in _ALLOWED[band_code] and not op.get("override_reason"):
        raise Refused(
            "invalid_arguments",
            "override_reason_required",
            f"the band is {band_code}: a {disposition} disposition needs an override reason",
            {"band": band_code, "allowed": sorted(_ALLOWED[band_code])},
        )
    if disposition == "moved" and claim_id not in run.changed_claims:
        raise Refused(
            "invalid_arguments",
            "moved_needs_change",
            "a moved claim is changed in the same batch, before the verify (D3's operations)",
            {"claim": claim_id},
        )
    if disposition == "retired" and claim_id not in run.retired_claims:
        raise Refused(
            "invalid_arguments",
            "retired_needs_end",
            "a retired claim is ended in the same batch with the end class retired",
            {"claim": claim_id},
        )
    # D6 owns consequence endings after all operations validate. Checking them here would
    # reject a retirement before its R1 effects can run in this same transaction. A failed
    # ending becomes D6's stored cascade_failed proposal rather than discarding retirement.
    recheck = _recheck_value(op)
    if disposition == "deferred" and recheck is None:
        raise Refused(
            "missing_recheck",
            "deferral_recheck",
            "a deferral sets a new re-check by with its reason",
            {},
        )
    if disposition == "confirmed" and recheck is None and claim.get("recheck_by") is not None:
        raise Refused(
            "missing_recheck",
            "confirmation_recheck",
            "a confirmation renews the re-check by",
            {"previous": claim["recheck_by"].isoformat()},
        )

    ordinal = (
        accepted_curator_run(run.tx, run.stamp.run_id, run.at, stamp=run.stamp)
        if run.caller_kind == "cleanup" and run.stamp.run_id is not None
        else None
    )
    _claim(run, claim_id)
    # Writes. The evidence first: a piece that supports or contradicts becomes a row on the claim.
    plans = []
    for piece in pieces:
        if piece.verdict not in ("supports", "contradicts"):
            continue
        piece.plan = apply._Plan(run.new_id(), piece.verdict, dict(piece.columns))
        plans.append(piece.plan)
    reason_id = run.text(op["reason"])
    if plans:
        apply._write_evidence(run, plans, claim_type, claim_id, reason_id)
    verification_id = run.new_id()
    admitted = [piece for piece in pieces if piece.admitted]
    confirmed_at = max(piece.spoken_for for piece in admitted) if admitted else None
    world_time = confirmed_at or (max(piece.spoken_for for piece in pieces) if pieces else None)
    row = compact(
        {
            "id": verification_id,
            "attempt_kind": "scored",
            "attempt_id": verification_id,
            "claim_type": claim_type,
            "claim_version": claim.get("version"),
            "run_ordinal": ordinal,
            "change_set_id": run.stamp.change_set_id,
            "claim_id": claim_id,
            "run_id": run.stamp.run_id,
            "at": run.at,
            "evidence_pieces": json.dumps([_piece_json(piece) for piece in pieces], sort_keys=True),
            "second_judge_judgment_ids": [row["id"] for piece in pieces for row in piece.second]
            or None,
            "correlated": True if correlated else None,
            "agree": _round(agree),
            "disagree": _round(disagree),
            "ratio": _round(ratio),
            "band": band_code,
            "loadbearing": bearing,
            "disposition": disposition,
            "override_reason": run.text(op["override_reason"])
            if op.get("override_reason")
            else None,
            "recheck_by_before": claim.get("recheck_by"),
            "recheck_by_after": recheck if recheck is not None else claim.get("recheck_by"),
            "reason": reason_id,
            "request_id": run.stamp.request_id,
            "commit_id": run.stamp.commit_id,
        }
    )
    run.tx.run("CREATE (v:Verification) SET v += $row", row=row)
    if disposition == "confirmed" and not bearing:
        # A later operation of this batch can add a consequence link: the batch ends with the
        # claim load-bearing, so the end of the batch checks condition 4 again.
        run.late_bearing.append(LateBearing(index, claim_type, claim_id, verification_id, pieces))
    columns: dict[str, Any] = {}
    if disposition == "confirmed":
        for name, old, new in (
            ("last_confirmed", claim.get("last_confirmed"), confirmed_at),
            ("confirmed_against", claim.get("confirmed_against"), verification_id),
        ):
            run.entry(
                entity_type=claim_type,
                entity_id=claim_id,
                field=name,
                old=old,
                new=new,
                world_time=world_time,
                reason_id=reason_id,
                evidence_id=admitted[0].plan.id if admitted and admitted[0].plan else None,
            )
        columns.update({"last_confirmed": confirmed_at, "confirmed_against": verification_id})
    if recheck is not None and disposition in ("confirmed", "deferred"):
        if recheck != claim.get("recheck_by"):
            run.entry(
                entity_type=claim_type,
                entity_id=claim_id,
                field="recheck_by",
                old=claim.get("recheck_by"),
                new=recheck,
                world_time=world_time,
                reason_id=reason_id,
            )
            columns["recheck_by"] = recheck
        if op.get("recheck_how") is not None:
            columns["recheck_how"] = json.dumps(op["recheck_how"], sort_keys=True)
    run.entry(
        entity_type=claim_type,
        entity_id=claim_id,
        field="verified",
        new=verification_id,
        world_time=world_time,
        reason_id=reason_id,
    )
    _set_verification_sequence(run.tx, verification_id)
    if run.caller_kind == "cleanup" and disposition == "deferred":
        _pending_escalation(
            run.tx,
            run.stamp,
            claim_type,
            claim_id,
            verification_id,
            verification_streak(run.tx, claim_type, claim_id),
            reason_id=reason_id,
            person_only=False,
        )
    if columns:
        setter = apply._SET_ITEM if claim_type == "item" else apply._SET_LINK
        bump = apply._BUMP_ITEM if claim_type == "item" else apply._BUMP_LINK
        run.tx.run(setter, id=claim_id, columns=columns)
        run.tx.run(bump, id=claim_id)
    if claim_type == "item":
        run.touch(claim_id, index)


def _piece_json(piece: Piece) -> dict[str, Any]:
    plan_id = piece.plan.id if piece.plan is not None else None
    return {
        "evidence_id": plan_id or piece.evidence_id,
        "source_id": piece.columns["source_id"],
        "locator": piece.columns["locator"],
        "fetched_at": piece.fetched_at.isoformat() if piece.fetched_at else None,
        "time_spoken_for": piece.spoken_for.isoformat(),
        "source_type": piece.columns["source_type"],
        "judgment_id": (piece.judgment or {}).get("id"),
        "answer": piece.answer,
        "verdict": piece.verdict,
        "weight": _round(piece.weight),
        "capability": piece.capability,
        "capability_row": piece.capability_row or None,
        "admitted": piece.admitted,
    }


@dataclass
class LateBearing:
    index: int
    claim_type: str
    claim_id: str
    verification_id: str
    pieces: list


def check_late_bearing(run: "_Run", pending: LateBearing) -> None:
    """Condition 4 at the end of the batch, for a confirmation made while the claim was a leaf."""
    from grove.writer import apply

    read = apply._one(
        run,
        apply._READ_ITEM if pending.claim_type == "item" else apply._READ_LINK,
        pending.claim_type,
        pending.claim_id,
        lock=False,
    )
    if not load_bearing(run, pending.claim_type, read):
        return
    _second_judge_gate(pending.pieces)
    run.tx.run(
        "MATCH (v:Verification {id: $id}) SET v.loadbearing = true", id=pending.verification_id
    )


# The confirmation at create ---------------------------------------------------------------------


@dataclass
class CreateConfirm:
    index: int
    op: dict[str, Any]
    claim_type: str
    claim_id: str
    plans: list


def _extraction_call(run: "_Run", call_id: str) -> dict[str, Any]:
    rows = run.tx.run(
        "MATCH (c:ModelCall {id: $id}) RETURN c.job AS job, c.outcome AS outcome, "
        "c.run_id AS run_id, c.at AS at",
        id=call_id,
    )
    if not rows:
        raise Refused(
            "unrecorded_verdict",
            "extraction_call",
            "confirmed_by_call names no recorded model call of this world",
            {},
        )
    call = rows[0]
    if call["job"] not in _EXTRACTION_JOBS or call["outcome"] != "ok":
        raise Refused(
            "unrecorded_verdict",
            "extraction_call",
            "confirmed_by_call names the extraction call that read the evidence",
            {},
        )
    if run.stamp.run_id is None or call["run_id"] != run.stamp.run_id:
        raise Refused(
            "unrecorded_verdict",
            "extraction_pass",
            "a confirmation at create cites an extraction call of this pass (the batch's run)",
            {},
        )
    return call


def confirm_at_create(run: "_Run", pending: CreateConfirm) -> None:
    """D4: last confirmed on create only when the creating evidence is itself confirming, was
    read in this pass and cites the extraction call that read it, and never for a claim that is
    load-bearing at creation (it starts unconfirmed and takes the full verify in the lane).
    Runs at the end of the batch, so a link that the batch adds counts as load-bearing."""
    from grove.writer import apply

    call = _extraction_call(run, pending.op["confirmed_by_call"])
    read = apply._one(
        run,
        apply._READ_ITEM if pending.claim_type == "item" else apply._READ_LINK,
        pending.claim_type,
        pending.claim_id,
        lock=False,
    )
    if load_bearing(run, pending.claim_type, read):
        raise Refused(
            "second_judge_required",
            "load_bearing_at_create",
            "a load-bearing claim starts unconfirmed: send it without confirmed_by_call, and "
            "verify it in the lane",
            {},
        )
    admitted = []
    for plan in pending.plans:
        if plan.polarity != "supports":
            continue
        columns = plan.columns
        source, _, connector_type = _source_and_connector(run, columns["source_id"])
        received = source.get("received_at")
        if (
            columns["derivation"] == "observed"
            and columns["source_type"] == "confirmed"
            and not after_its_read(columns, source, connector_type, run.at)
            and received is not None
            and call["at"] is not None
            and received <= call["at"]
            and capability_of(run, columns, pending.claim_type, read).capability == caps.CONFIRMS
        ):
            admitted.append(plan)
    if not admitted:
        raise Refused(
            "not_confirmable",
            "creating_evidence",
            "the creating evidence does not confirm this claim: it is a row of source type "
            "confirmed, read in this pass, from a source that can prove it",
            {},
        )
    confirmed_at = max(plan.source_time for plan in admitted)
    verification_id = run.new_id()
    pieces = [
        {
            "evidence_id": plan.id,
            "source_id": plan.columns["source_id"],
            "locator": plan.columns["locator"],
            "time_spoken_for": plan.source_time.isoformat(),
            "source_type": plan.columns["source_type"],
            "extraction_call_id": pending.op["confirmed_by_call"],
            "verdict": "supports",
            "weight": FULL_WEIGHT,
            "capability": caps.CONFIRMS,
            "admitted": True,
        }
        for plan in admitted
    ]
    reason_id = run.text(pending.op["reason"])
    run.tx.run(
        "CREATE (v:Verification) SET v += $row",
        row=compact(
            {
                "id": verification_id,
                "claim_id": pending.claim_id,
                "run_id": run.stamp.run_id,
                "at": run.at,
                "evidence_pieces": json.dumps(pieces, sort_keys=True),
                "agree": float(len(admitted)),
                "disagree": 0.0,
                "ratio": 1.0,
                "band": CONFIRM,
                "loadbearing": False,
                "disposition": "confirmed",
                "recheck_by_before": None,
                "recheck_by_after": read.get("recheck_by"),
                "reason": reason_id,
                "request_id": run.stamp.request_id,
                "commit_id": run.stamp.commit_id,
            }
        ),
    )
    for name, new in (("last_confirmed", confirmed_at), ("confirmed_against", verification_id)):
        run.entry(
            entity_type=pending.claim_type,
            entity_id=pending.claim_id,
            field=name,
            new=new,
            world_time=confirmed_at,
            reason_id=reason_id,
            evidence_id=admitted[0].id,
        )
    run.entry(
        entity_type=pending.claim_type,
        entity_id=pending.claim_id,
        field="verified",
        new=verification_id,
        world_time=confirmed_at,
        reason_id=reason_id,
    )
    setter = apply._SET_ITEM if pending.claim_type == "item" else apply._SET_LINK
    run.tx.run(
        setter,
        id=pending.claim_id,
        columns={"last_confirmed": confirmed_at, "confirmed_against": verification_id},
    )


# Horizons -------------------------------------------------------------------------------------

PLAN_MARGIN = timedelta(days=1)
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def scheduled_latest(value: Any) -> datetime | None:
    """The latest instant a scheduled value allows: a clock's latest bound, or an ISO instant."""
    from grove.writer.clocks import ClockError, parse_clock

    if isinstance(value, str):
        if _DAY.fullmatch(value.strip()):
            # A bare date is a day: its latest instant is the day's end in UTC, and the plan's
            # one-day margin covers any timezone (S55).
            try:
                day = date.fromisoformat(value.strip())
            except ValueError:
                return None
            return datetime.combine(day + timedelta(days=1), time(0, 0), UTC) - timedelta(
                microseconds=1
            )
        try:
            return parse_instant(value)
        except ClockError:
            return None
    if isinstance(value, dict):
        try:
            clock = parse_clock(value)
        except ClockError:
            return None
        return clock.latest or clock.earliest
    return None


def plan_horizon(values: list[Any]) -> datetime | None:
    """Appendix C: a plan's re-check by is its scheduled time plus one day (the earliest of its
    scheduled times, by each one's latest bound)."""
    instants = [found for value in values if (found := scheduled_latest(value)) is not None]
    if not instants:
        return None
    return min(instants) + PLAN_MARGIN


# Inactive Curator command. The owner must supply an Access lease; there is no
# production default. All helpers below operate under the same WorldWriter lock.
_RESOLUTIONS = frozenset({"confirmed", "moved", "retired"})
_STRUCTURAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+~=-]{0,127}\Z")


def _identifier(value):
    if type(value) is not str or _STRUCTURAL_ID.fullmatch(value) is None:
        raise CleanupRefused("curator_reference_invalid")
    return value


def accepted_curator_run(tx, run_id: str, at: datetime, *, stamp=None) -> int:
    """Allocate one ordinal for accepted work, never for a mere started run.

    RunRecord is deliberately not required here: legacy scored callers use
    synthetic run identities. The protected no-source entry validates its run
    separately. A rollback also rolls back this allocation.
    """
    _identifier(run_id)
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise CleanupRefused("curator_time_invalid")
    tx.lock_world()
    rows = tx.run("MATCH (r:CuratorRunOrder {run_id: $id}) RETURN r", id=run_id)
    if rows:
        ordinal = rows[0]["r"]["ordinal"]
        if len(rows) != 1 or type(ordinal) is not int or ordinal < 1:
            raise CleanupRefused("curator_run_order_invalid")
        return ordinal
    rows = tx.run("MATCH (r:CuratorRunOrder) RETURN coalesce(max(r.ordinal), 0) AS ordinal")
    previous = rows[0]["ordinal"]
    if type(previous) is not int or not 0 <= previous < 2**63 - 1:
        raise CleanupRefused("curator_run_order_exhausted")
    row = {
        "id": new_id(tx.world_id),
        "world_id": tx.world_id,
        "run_id": run_id,
        "ordinal": previous + 1,
        "accepted_at": at,
        "version": 1,
        "touched_at": at,
    }
    tx.run("CREATE (r:CuratorRunOrder) SET r += $row", row=row)
    stamp = stamp or Stamp(
        tx.world_id, at, "curator", new_id(tx.world_id), new_id(tx.world_id), run_id=run_id
    )
    _question_event(tx, stamp, "run_accepted", "CuratorRunOrder", None, row)
    return previous + 1


def _ordered_verifications(tx, claim_type: str, claim_id: str) -> list[dict]:
    """Use the verified ChangeEntry, including for rows before this extension.

    A wall-clock tie, reordered snapshot, or duplicate attempt cannot affect the
    streak. Refuse inconsistent history rather than inventing an order.
    """
    rows = tx.run(
        "MATCH (v:Verification {claim_id: $id}), "
        "(e:ChangeEntry {entity_id: $id, entity_type: $kind, field: 'verified'}), "
        "(h:HistoryValue) WHERE e.new_value_id = h.id AND e.commit_id = v.commit_id "
        "RETURN v, e.sequence AS sequence, h.value AS value ORDER BY e.sequence",
        id=claim_id,
        kind=claim_type,
    )
    ordered, seen = [], {}
    for row in rows:
        verification, sequence = row["v"], row["sequence"]
        if row["value"] != encode(verification["id"]):
            continue
        if (
            type(sequence) is not int
            or sequence < 1
            or verification.get("claim_type", claim_type) != claim_type
            or verification.get("commit_sequence", sequence) != sequence
        ):
            raise CleanupRefused("curator_verification_history_invalid")
        identity = verification.get("attempt_id") or verification["id"]
        if identity in seen:
            if seen[identity] != verification:
                raise CleanupRefused("curator_attempt_history_conflict")
            continue
        seen[identity] = verification
        ordered.append({**verification, "commit_sequence": sequence})
    return ordered


def verification_streak(tx, claim_type: str, claim_id: str) -> tuple[dict, ...]:
    """Trailing unique deferred attempts, oldest first, from durable history."""
    if claim_type not in {"item", "link"}:
        raise CleanupRefused("curator_claim_type_invalid")
    _identifier(claim_id)
    streak = []
    for row in _ordered_verifications(tx, claim_type, claim_id):
        if row["disposition"] in _RESOLUTIONS:
            streak.clear()
        elif row["disposition"] == "deferred":
            streak.append(row)
        else:
            raise CleanupRefused("curator_verification_history_invalid")
    return tuple(streak)


def _recent_resolution(tx, claim_type, claim_id, run_id):
    if run_id is None:
        return None
    # Include the three latest accepted runs, including the current run if it
    # already accepted other work. Merely starting this run does not age history.
    recent = tx.run(
        "MATCH (r:CuratorRunOrder) RETURN r.run_id AS run_id ORDER BY r.ordinal DESC LIMIT 3"
    )
    ids = {row["run_id"] for row in recent}
    for row in reversed(_ordered_verifications(tx, claim_type, claim_id)):
        if row["disposition"] in _RESOLUTIONS and row.get("run_id") in ids:
            return row
    return None


def _refuse_recent_resolution(tx, claim_type, claim_id, run_id):
    prior = _recent_resolution(tx, claim_type, claim_id, run_id)
    if prior is not None:
        raise Refused(
            "invalid_arguments",
            "curator_oscillation",
            "a recent Curator resolution needs an open oscillation finding",
            {
                "resolution_id": prior["id"],
                "run_id": prior["run_id"],
                "prerequisite": "record_no_source_deferral",
            },
        )


def guard_scored_resolutions(tx, stamp: Stamp, ops: list[dict]) -> None:
    """Preflight for the shared writer seam, before any batch graph operation.

    The API owner must call this before apply_ops. In particular, moved/retired
    operations otherwise precede verify in the batch. verify_op repeats the guard
    and ordinary batch rollback remains mandatory on every refusal.
    """
    from grove.writer import apply
    from grove.writer.errors import BatchDeclined
    from grove.writer.ops import op_label

    if stamp.caller_kind != "cleanup" or stamp.run_id is None:
        return
    refusals = []
    for op in ops:
        if (
            not isinstance(op, dict)
            or op.get("op") != "verify"
            or op.get("disposition") not in _RESOLUTIONS
            or not isinstance(op.get("claim"), str)
        ):
            continue
        for kind, query in (("item", apply._READ_ITEM), ("link", apply._READ_LINK)):
            if tx.run(query, id=op["claim"]):
                try:
                    _refuse_recent_resolution(tx, kind, op["claim"], stamp.run_id)
                except Refused as error:
                    refusals.append(error.refusal(*op_label(op)))
                break
    if refusals:
        raise BatchDeclined(refusals, batch_size=len(ops), request_id=stamp.request_id)


def _set_verification_sequence(tx, verification_id):
    found = tx.run(
        "MATCH (v:Verification {id: $id}), (e:ChangeEntry {field: 'verified'}), "
        "(h:HistoryValue) WHERE e.new_value_id = h.id AND h.value = $value "
        "AND e.entity_id = v.claim_id AND e.commit_id = v.commit_id "
        "RETURN e.sequence AS sequence",
        id=verification_id,
        value=encode(verification_id),
    )
    if len(found) != 1:
        raise CleanupRefused("curator_verification_marker_missing")
    sequence = found[0]["sequence"]
    tx.run(
        "MATCH (v:Verification {id: $id}) SET v.commit_sequence = $sequence",
        id=verification_id,
        sequence=sequence,
    )
    return sequence


def _question_event(
    tx, stamp, event, label, before, after, *, work_id=None, reason=None, finding=None
):
    payload = {"label": label, "before": before, "after": after}
    if finding is not None:
        payload["finding"] = finding
    snapshot = encode(payload)
    if len(snapshot.encode("utf-8")) > 256 * 1024:
        raise CleanupRefused("question_history_bound")
    tx.run(
        "CREATE (e:QuestionEvent) SET e += $row",
        row=compact(
            {
                "id": new_id(tx.world_id),
                "question_id": after.get("question_id"),
                "work_id": work_id,
                "event": event,
                "commit_id": stamp.commit_id,
                "change_set_id": stamp.change_set_id,
                "sequence": tx.next_sequence(),
                "at": stamp.at,
                "reason": reason,
                "version": 1,
                "touched_at": stamp.at,
                "snapshot": snapshot,
            }
        ),
    )


def _checked_legwork(tx, command):
    from grove.cleanup.legwork import LegworkAttemptV1
    from grove.cleanup.question_lifecycle import canonical_json, decode_json

    if not command.legwork_ids:
        raise CleanupRefused("no_source_checked_legwork_required")
    attempts = []
    for legwork_id in command.legwork_ids:
        found = tx.run("MATCH (l:CuratorLegwork {id: $id}) RETURN l", id=legwork_id)
        if len(found) != 1:
            raise CleanupRefused("no_source_checked_legwork_required")
        row = found[0]["l"]
        # Decode through the same closed codec used by the authorized producer.
        attempt = decode_json(LegworkAttemptV1, row["attempt"])
        if (
            attempt.attempt_id != legwork_id
            or attempt.work_id != command.work_id
            or attempt.run_id != command.run_id
            or attempt.target != command.target
            or row.get("work_id") != attempt.work_id
            or row.get("run_id") != attempt.run_id
            or row.get("result") != attempt.result
            or row.get("target") != canonical_json(attempt.target)
            or row.get("reason") != attempt.reason_value_id
        ):
            raise CleanupRefused("no_source_legwork_binding_invalid")
        if attempt.result not in {"no_match", "no_eligible_source", "person_only"}:
            raise CleanupRefused("no_source_legwork_incomplete")
        attempts.append(attempt)
    return attempts


def _pending_escalation(
    tx, stamp, claim_type, claim_id, attempt_id, streak, *, reason_id, person_only, work_id=None
):
    if len(streak) < 3:
        return None
    start = streak[0].get("attempt_id") or streak[0]["id"]
    existing = tx.run(
        "MATCH (e:CuratorEscalation {claim_type: $kind, claim_id: $id, "
        "streak_start: $start}) RETURN e",
        kind=claim_type,
        id=claim_id,
        start=start,
    )
    if existing:
        return existing[0]["e"]["id"]
    destination = "question" if person_only else "retirement_proposal"
    row = {
        "id": new_id(tx.world_id),
        "world_id": tx.world_id,
        "claim_id": claim_id,
        "claim_type": claim_type,
        "streak_start": start,
        "attempt_id": attempt_id,
        "destination": destination,
        "state": "pending",
        "prerequisite": "bound_question_plan" if person_only else "retirement_proposal_owner",
        "created_at": stamp.at,
        "version": 1,
        "touched_at": stamp.at,
    }
    tx.run("CREATE (e:CuratorEscalation) SET e += $row", row=row)
    _question_event(
        tx,
        stamp,
        "escalation_pending",
        "CuratorEscalation",
        None,
        row,
        work_id=work_id,
        reason=reason_id,
    )
    return row["id"]


def record_no_source_deferral(
    driver, world_id: str, caller, command: "NoSourceDeferralV1", *, access: Access | None = None
) -> str:
    """Record one checked failed source check; return its durable Verification ID.

    Exact retries return the original row under current authority. A recent
    resolution records an oscillation event and pending work without moving any
    claim clock. Pending escalations are intents, never invented destinations.
    """
    from grove.cleanup.legwork import NoSourceDeferralV1
    from grove.cleanup.question_lifecycle import canonical_json
    from grove.store import lane
    from grove.writer import apply
    from grove.writer.cleanup import _caller, _ordinary

    _caller(caller, {"cleanup"})
    if type(command) is not NoSourceDeferralV1:
        raise CleanupRefused("no_source_command_invalid")
    encoded = canonical_json(command)

    def work(tx):
        _ordinary(tx)
        # Access covers replay disclosure, including references held by legwork.
        attempts = _checked_legwork(tx, command)
        with hold(
            access,
            world_id,
            command.run_id,
            "no_source_legwork",
            *(a.authority_id for a in attempts),
            *(ref for a in attempts for ref in a.scope_ids),
            *(
                ref
                for a in attempts
                for source in a.source_refs
                for ref in (source.source_id, source.episode_id)
            ),
        ):
            return commit(tx, attempts)

    def commit(tx, attempts):
        previous = tx.run(
            "MATCH (v:Verification {attempt_id: $id}) RETURN v", id=command.attempt_id
        )
        if previous:
            row = previous[0]["v"]
            if (
                len(previous) != 1
                or row.get("attempt_kind") != "no_source"
                or row.get("command") != encoded
            ):
                raise CleanupRefused("idempotency_conflict")
            history = _ordered_verifications(tx, command.target.kind, command.target.id)
            if not any(v["id"] == row["id"] for v in history):
                raise CleanupRefused("curator_verification_marker_missing")
            return row["id"]
        at = clock.now(world_id)
        runs = tx.run("MATCH (r:RunRecord {id: $id}) RETURN r", id=command.run_id)
        if (
            len(runs) != 1
            or runs[0]["r"].get("agent") != "curator"
            or runs[0]["r"].get("outcome") != "running"
            or runs[0]["r"].get("started") is None
            or runs[0]["r"]["started"] > at
        ):
            raise CleanupRefused("no_source_run_invalid")
        if command.next_check <= at:
            raise CleanupRefused("no_source_recheck_not_future")
        # D4 has no calibrated maximum horizon: retain its explicit-instant
        # policy and never synthesize a default from a retry or a run timestamp.
        parse_instant(command.next_check.isoformat())
        reason = tx.run(
            "MATCH (h:HistoryValue {id: $id}) RETURN h.value AS value",
            id=command.reason_value_id,
        )
        if (
            len(reason) != 1
            or not isinstance(reason[0]["value"], str)
            or not reason[0]["value"].strip()
            or len(reason[0]["value"]) > 2000
            or reason[0]["value"] == "[forgotten]"
        ):
            raise CleanupRefused("no_source_reason_invalid")
        stamp = Stamp(
            world_id,
            at,
            caller.actor,
            new_id(world_id),
            new_id(world_id),
            run_id=command.run_id,
            caller_kind=caller.kind,
        )
        run = apply._Run(tx, stamp, {}, caller.kind)
        kind, claim = _claim(run, command.target.id, lock=False)
        if kind != command.target.kind or command.target.slot != "claim":
            raise CleanupRefused("no_source_target_invalid")
        if claim.get("version") != command.target.version:
            raise CleanupRefused("no_source_target_stale")
        if has_ended(claim) or (kind == "link" and not claim.get("active_key")):
            raise CleanupRefused("no_source_target_ended")

        class TransactionRead:
            def read(self, query, **parameters):
                return tx.run(query, **parameters)

        if not any(
            d.claim_type == kind and d.claim_id == claim["id"]
            for d in lane.pool(TransactionRead(), at=at)
        ):
            raise CleanupRefused("no_source_target_not_due")
        found = tx.run("MATCH (w:QuestionWork {id: $id}) RETURN w", id=command.work_id)
        before = found[0]["w"] if found else None
        target = canonical_json(command.target)
        if before and (
            before.get("target") != target
            or before.get("state") in {"resolved", "cancelled"}
            or (before.get("lease_owner") not in {None, command.run_id})
        ):
            raise CleanupRefused("no_source_work_conflict")
        prior = _recent_resolution(tx, kind, claim["id"], command.run_id)
        ordinal = accepted_curator_run(tx, command.run_id, at, stamp=stamp)
        verification_id = new_id(world_id)
        row = compact(
            {
                "id": verification_id,
                "claim_id": claim["id"],
                "claim_type": kind,
                "claim_version": command.target.version,
                "run_id": command.run_id,
                "run_ordinal": ordinal,
                "at": at,
                "attempt_kind": "no_source",
                "attempt_id": command.attempt_id,
                "work_id": command.work_id,
                "legwork_ids": list(command.legwork_ids),
                "command": encoded,
                "change_set_id": stamp.change_set_id,
                "commit_id": stamp.commit_id,
                "evidence_pieces": "[]",
                "agree": 0.0,
                "disagree": 0.0,
                "band": ZERO_SIGNAL,
                "loadbearing": load_bearing(run, kind, claim),
                "disposition": "deferred",
                "reason": command.reason_value_id,
                "recheck_by_before": claim.get("recheck_by"),
                "recheck_by_after": claim.get("recheck_by") if prior else command.next_check,
            }
        )
        tx.run("CREATE (v:Verification) SET v += $row", row=row)
        columns = {}
        if prior is None:
            columns["recheck_by"] = command.next_check
            if command.recheck_how is not None:
                columns["recheck_how"] = json.dumps(command.recheck_how, sort_keys=True)
            columns = {key: value for key, value in columns.items() if value != claim.get(key)}
            for key, value in columns.items():
                old = claim.get(key)
                # D3 stores the semantic JSON value, not its serialized column.
                if key == "recheck_how":
                    old = json.loads(old) if old is not None else None
                    value = json.loads(value)
                add_entry(
                    tx,
                    stamp,
                    entity_type=kind,
                    entity_id=claim["id"],
                    field=key,
                    old=old,
                    new=value,
                    world_time=None,
                    reason_id=command.reason_value_id,
                )
            if columns:
                tx.run(
                    apply._SET_ITEM if kind == "item" else apply._SET_LINK,
                    id=claim["id"],
                    columns={**columns, "touched_at": at},
                )
                tx.run(apply._BUMP_ITEM if kind == "item" else apply._BUMP_LINK, id=claim["id"])
        add_entry(
            tx,
            stamp,
            entity_type=kind,
            entity_id=claim["id"],
            field="verified",
            new=verification_id,
            world_time=None,
            reason_id=command.reason_value_id,
        )
        _set_verification_sequence(tx, verification_id)
        after = {
            **(before or {}),
            "id": command.work_id,
            "world_id": world_id,
            "group_id": (before or {}).get("group_id", command.work_id),
            "target": target,
            "state": "pending" if prior else "deferred",
            "retry_at": command.next_check,
            "reason": command.reason_value_id,
            "version": (before or {}).get("version", 0) + 1,
            "touched_at": at,
        }
        if columns:
            after["target"] = canonical_json(
                replace(command.target, version=command.target.version + 1)
            )
        after.pop("lease_owner", None)
        after.pop("lease_until", None)
        if before:
            tx.run(
                "MATCH (w:QuestionWork {id: $id}) SET w += $row",
                id=command.work_id,
                row={**{key: None for key in before if key not in after}, **after},
            )
        else:
            tx.run("CREATE (w:QuestionWork) SET w += $row", row=after)
        _question_event(
            tx,
            stamp,
            "no_source_deferred",
            "QuestionWork",
            before,
            after,
            work_id=command.work_id,
            reason=command.reason_value_id,
        )
        if prior:
            # This event is a structural finding, not a successful resolution.
            _question_event(
                tx,
                stamp,
                "oscillation",
                "QuestionWork",
                after,
                after,
                work_id=command.work_id,
                reason=command.reason_value_id,
                finding={
                    "resolution_id": prior["id"],
                    "resolution_run": prior["run_id"],
                    "flag_attempt": command.attempt_id,
                    "flag_run": command.run_id,
                    "target": command.target.to_json(),
                },
            )
        _pending_escalation(
            tx,
            stamp,
            kind,
            claim["id"],
            command.attempt_id,
            verification_streak(tx, kind, claim["id"]),
            work_id=command.work_id,
            reason_id=command.reason_value_id,
            person_only=any(a.result == "person_only" for a in attempts),
        )
        return verification_id

    with hold(
        access,
        world_id,
        command.run_id,
        "record_no_source_deferral",
        command.target.id,
        command.work_id,
        command.reason_value_id,
        *command.legwork_ids,
    ):
        return WorldWriter(driver, world_id).transaction(work)
