"""From extraction's proposed words to propose_word operations (D8 lines 1585 to 1597).

The runner's code calls this before the batch reaches the writer, because the judges are network
calls and the writer opens no network call inside a transaction (D2, line 1668). For each proposal
in order:

1. A name that already resolves mints nothing: the batch writes with the existing word.
2. A proposal the writer would refuse (no neighbours, no distinction, an unmapped status, a field
   out of its list) costs no judge call. A refused meaning rides on the generic connection meaning
   with its phrase kept (line 1596); a refused kind or property name is held for the caller.
3. Past the valve of three new words per batch, a phrase rides on the generic meaning too: the
   valve bounds the registry, not the spend (line 1597).
4. Otherwise both judges run, and the operation cites their recorded judgments. The plan settles
   the word as the writer will (`settle`: an alias, or a mint with the safer value of each
   disputed field). A word that would settle unusable is held, or rides on the generic meaning.

The plan reads the registry once, before the batch: a word minted by the batch is no neighbour of
a later proposal, in the plan and in the writer alike. The writer reads the registry again under
the world lock and checks every operation, so a plan that went stale is refused there, never
applied wrong.

`fit_links` then makes the caller's links fit the plan: a phrase sent to the generic meaning is
rewritten to it with the phrase kept, and a link whose meaning settled as a state (the generic
meaning, a disputed mint, an alias of a state meaning) gets a re-check by from that meaning's
horizon when it carries none, because a link of a state meaning is a claim about now (D4).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from grove.models.errors import InvalidOutput, ModelRefused
from grove.models.judge import judge_word
from grove.models.layer import ModelLayer
from grove.store.connection import Driver
from grove.store.normalise import normalise_phrase
from grove.store.query import WorldQuery
from grove.writer.errors import Refused
from grove.writer.judgments import JudgmentRefused
from grove.writer.ontology import (
    MINT_VALVE,
    OUTCOME_MINT,
    SAFER_HORIZON_DAYS,
    check_proposal,
    disputed_of,
    settle,
    verdict_outcome,
)
from grove.writer.registry import Registry
from grove.writer.resolver import generic_meaning

# A proposal carries the text of its worked example beside the operation's fields; the plan drops
# it before the operation goes to the writer, which reads the evidence from the store.
EXAMPLE = "example"
VALVE_RULE = "mint_valve"
JUDGE_RULE = "judge_failed"
# A proposal the judges could not judge: its own fault, so the plan goes on. A budget stop or a
# run cap is the pass's, and propagates so the requests stay queued.
_PROPOSAL_FAULTS = (JudgmentRefused, InvalidOutput, ModelRefused)


@dataclass(frozen=True)
class PlannedWord:
    """How one planned word settles: `outcome` alias or mint, the `temporal_class` and
    `default_horizon` its links read (an alias reads its neighbour's), and the `disputed` fields
    that took their safer value."""

    word_type: str
    name: str
    kind_id: str | None
    outcome: str
    temporal_class: str | None
    default_horizon: int | None
    disputed: tuple[str, ...]


@dataclass(frozen=True)
class WordPlan:
    """`ops` are propose_word operations, in order, each citing its judgments, and `words` how
    each settles. `generic` holds the meaning phrases that ride on the generic meaning; `held` the
    (word type, name, rule) of a kind or a property name the writer would refuse; `reused` the
    names that resolve already. `generic_meaning` is the generic row's id, temporal class and
    horizon, or None in a world with no single generic meaning."""

    ops: tuple[dict[str, Any], ...]
    generic: tuple[str, ...]
    held: tuple[tuple[str, str, str], ...]
    reused: tuple[str, ...]
    words: tuple[PlannedWord, ...] = ()
    generic_meaning: dict[str, Any] | None = None


def _set_aside(
    word_type: str, name: str, rule: str, generic: list[str], held: list[tuple[str, str, str]]
) -> None:
    if word_type == "meaning":
        generic.append(name)
    else:
        held.append((word_type, name, rule))


def plan_words(
    layer: ModelLayer,
    driver: Driver,
    world_id: str,
    proposals: list[dict[str, Any]],
    *,
    run_id: str | None = None,
    valve: int = MINT_VALVE,
) -> WordPlan:
    registry = Registry.load(WorldQuery(driver, world_id))
    ops: list[dict[str, Any]] = []
    words: list[PlannedWord] = []
    generic: list[str] = []
    held: list[tuple[str, str, str]] = []
    reused: list[str] = []
    mints = 0
    seen: set[tuple[str, str, str]] = set()
    for proposal in proposals:
        fields = {key: value for key, value in proposal.items() if key != EXAMPLE}
        name = str(fields.get("name") or "")
        word_type = str(fields.get("word_type") or "")
        key = (word_type, str(fields.get("kind") or ""), normalise_phrase(name))
        if key in seen:
            # One word per plan: a second proposal of it writes with the first.
            reused.append(name)
            continue
        seen.add(key)
        try:
            checked = check_proposal(registry, fields)
        except Refused as refused:
            if refused.rule == "word_exists":
                reused.append(name)
            else:
                _set_aside(word_type, name, refused.rule, generic, held)
            continue
        if mints >= valve:
            _set_aside(word_type, name, VALVE_RULE, generic, held)
            continue
        try:
            judged = judge_word(
                layer,
                driver,
                world_id,
                checked,
                example=str(proposal.get(EXAMPLE) or ""),
                run_id=run_id,
            )
        except _PROPOSAL_FAULTS:
            _set_aside(word_type, name, JUDGE_RULE, generic, held)
            continue
        outcome = verdict_outcome(checked, judged.same_word)
        disputed = disputed_of(
            checked, {field: [verdict] for field, verdict in judged.fields.items()}
        )
        try:
            columns = settle(registry, checked, outcome, disputed)
        except Refused as refused:
            # The judges settled the word unusable (a disputed property of a kind that is not a
            # record would become a state): the writer would decline the batch for it.
            _set_aside(word_type, name, refused.rule, generic, held)
            continue
        if outcome == OUTCOME_MINT:
            mints += 1
        ops.append({**fields, "op": "propose_word", "judgment_ids": list(judged.judgment_ids)})
        words.append(
            PlannedWord(
                word_type,
                checked.name,
                checked.kind_id,
                outcome,
                columns.get("temporal_class"),
                columns.get("default_horizon"),
                tuple(sorted(disputed)) if outcome == OUTCOME_MINT else (),
            )
        )
    row = generic_meaning(registry)
    generic_row = (
        None
        if row is None
        else {
            "id": row["id"],
            "temporal_class": row.get("temporal_class"),
            "default_horizon": row.get("default_horizon"),
        }
    )
    return WordPlan(
        tuple(ops), tuple(generic), tuple(held), tuple(reused), tuple(words), generic_row
    )


def _recheck_by(now: datetime, temporal_class: Any, horizon: Any) -> str | None:
    if temporal_class != "state":
        return None
    days = horizon if isinstance(horizon, int) and horizon > 0 else SAFER_HORIZON_DAYS
    return (now + timedelta(days=days)).isoformat()


def fit_links(plan: WordPlan, ops: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """Make the caller's create_link operations fit the plan (see the module text). A link that
    already carries a re-check by keeps it. A world with no single generic meaning leaves a
    generic phrase as it is, and the writer refuses that link."""
    wanted = {normalise_phrase(phrase) for phrase in plan.generic}
    settled = {
        normalise_phrase(word.name): word for word in plan.words if word.word_type == "meaning"
    }
    rewritten = []
    for op in ops:
        meaning = op.get("meaning") if op.get("op") == "create_link" else None
        if not isinstance(meaning, str):
            rewritten.append(op)
            continue
        phrase = normalise_phrase(meaning)
        recheck = None
        if phrase in wanted and plan.generic_meaning is not None:
            generic = plan.generic_meaning
            op = {**op, "meaning": generic["id"], "original_phrase": meaning}
            recheck = _recheck_by(now, generic["temporal_class"], generic["default_horizon"])
        elif phrase in settled:
            word = settled[phrase]
            recheck = _recheck_by(now, word.temporal_class, word.default_horizon)
        if recheck is not None and op.get("recheck_by") is None:
            op = {**op, "recheck_by": recheck}
        rewritten.append(op)
    return rewritten
