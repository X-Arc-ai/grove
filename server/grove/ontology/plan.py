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
   the word as the writer will (`settle`: an alias whose ends fit its neighbour, or a mint with
   the safer value of each disputed field). A word that would settle unusable is held, or rides
   on the generic meaning.

The plan checks every proposal against its own copy of the registry, which learns each planned
word as the writer's batch registry will (`add_to_registry`): a later proposal of the same word,
in any form the resolver takes to it (a normal form, a copula, an alias, another spelling of the
kind), is reused, and a word the plan minted is no neighbour of a later proposal, in the plan and
in the writer alike. The writer reads the registry again under the world lock and checks every
operation, so a plan that went stale is refused there, never applied wrong.

`fit_links` then makes the caller's links fit the plan. It resolves each link phrase with the
resolver against the plan's registry: a phrase that resolves only through a planned word gets a
re-check by from that word's horizon when it settled as a state (D4: a link of a state meaning is
a claim about now), and a phrase sent to the generic meaning is rewritten to it with the phrase
kept. A phrase that resolves to an existing word is the caller's and stays as it is.
"""

import copy
from dataclasses import dataclass, field
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
    OUTCOME_ALIAS,
    OUTCOME_MINT,
    SAFER_HORIZON_DAYS,
    add_to_registry,
    check_proposal,
    disputed_of,
    settle,
    verdict_outcome,
)
from grove.writer.registry import Registry
from grove.writer.resolver import NONE, generic_meaning, resolve_word

# A proposal carries the text of its worked example beside the operation's fields; the plan drops
# it before the operation goes to the writer, which reads the evidence from the store.
EXAMPLE = "example"
VALVE_RULE = "mint_valve"
JUDGE_RULE = "judge_failed"
# A property name of a kind this plan mints: its field-fit subject would bind the planned kind,
# which the writer gives another id, so it waits for a batch after the kind exists.
PLANNED_KIND_RULE = "kind_in_this_plan"
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
    # The registry as read before the batch, and the plan's copy that learned each planned word:
    # fit_links resolves link phrases against both.
    base: Registry | None = field(default=None, compare=False, repr=False)
    registry: Registry | None = field(default=None, compare=False, repr=False)


def _bare(name: str) -> str:
    """A name's normal form without a leading "is" or "are", as the resolver's copula rule."""
    normalised = normalise_phrase(name)
    first, _, rest = normalised.partition(" ")
    return rest if first in ("is", "are") and rest else normalised


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
    base = Registry.load(WorldQuery(driver, world_id))
    # One read: the plan's copy starts equal to the registry the base records.
    registry = copy.deepcopy(base)
    ops: list[dict[str, Any]] = []
    words: list[PlannedWord] = []
    generic: list[str] = []
    held: list[tuple[str, str, str]] = []
    reused: list[str] = []
    mints = 0
    fresh: set[str] = set()
    # A word the judges set aside stays aside for the rest of the plan: a second proposal of it
    # is not judged again, so two verdicts on one word never disagree inside one plan.
    judged_aside: dict[tuple[str, str | None, str], str] = {}
    for index, proposal in enumerate(proposals):
        fields = {key: value for key, value in proposal.items() if key != EXAMPLE}
        name = str(fields.get("name") or "")
        word_type = str(fields.get("word_type") or "")
        try:
            checked = check_proposal(registry, fields, fresh=frozenset(fresh))
        except Refused as refused:
            if refused.rule == "word_exists":
                # One word per plan: a later proposal of a planned word writes with it.
                reused.append(name)
            else:
                _set_aside(word_type, name, refused.rule, generic, held)
            continue
        if checked.kind_id in fresh:
            _set_aside(word_type, name, PLANNED_KIND_RULE, generic, held)
            continue
        identity = (word_type, checked.kind_id, _bare(checked.name))
        if identity in judged_aside:
            _set_aside(word_type, name, judged_aside[identity], generic, held)
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
            judged_aside[identity] = JUDGE_RULE
            _set_aside(word_type, name, JUDGE_RULE, generic, held)
            continue
        outcome = verdict_outcome(checked, judged.same_word)
        disputed = disputed_of(
            checked, {name_: [verdict] for name_, verdict in judged.fields.items()}
        )
        try:
            columns = settle(registry, checked, outcome, disputed)
        except Refused as refused:
            # The judges settled the word unusable: a disputed property of a kind that is not a
            # record would become a state, or an alias's ends fit its neighbour only reversed.
            judged_aside[identity] = refused.rule
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
        _learn(registry, checked, outcome, columns, index, fresh)
    row = generic_meaning(base)
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
        tuple(ops),
        tuple(generic),
        tuple(held),
        tuple(reused),
        tuple(words),
        generic_row,
        base,
        registry,
    )


def _learn(
    registry: Registry,
    checked: Any,
    outcome: str,
    columns: dict[str, Any],
    index: int,
    fresh: set[str],
) -> None:
    """The plan's registry learns a planned word as the writer's batch registry will: a mint as a
    provisional row of its settled columns, an alias as an alias row onto its neighbour."""
    if outcome == OUTCOME_ALIAS:
        add_to_registry(
            registry,
            "alias",
            {
                "id": f"planned-alias:{index}",
                "phrase": checked.name,
                "normalised_phrase": normalise_phrase(checked.name),
                "target_type": checked.word_type,
                "target_id": checked.neighbours[0].row["id"],
                "reversed": checked.reversed,
                "status": "canonical",
            },
        )
        return
    row_id = f"planned:{index}"
    add_to_registry(
        registry,
        checked.word_type,
        {**columns, "id": row_id, "name": checked.name, "status": "provisional"},
    )
    fresh.add(row_id)


def _recheck_by(now: datetime, temporal_class: Any, horizon: Any) -> str | None:
    if temporal_class != "state":
        return None
    days = horizon if isinstance(horizon, int) and horizon > 0 else SAFER_HORIZON_DAYS
    return (now + timedelta(days=days)).isoformat()


def _generic_phrases(plan: WordPlan) -> Registry:
    """The phrases sent to the generic meaning, as rows the resolver can match."""
    rows = {
        f"generic:{index}": {"id": f"generic:{index}", "name": phrase, "status": "provisional"}
        for index, phrase in enumerate(plan.generic)
    }
    return Registry({}, rows, [], [])


def _resolves(registry: Registry | None, phrase: str) -> Any:
    if registry is None:
        return None
    try:
        return resolve_word(registry, "meaning", phrase)
    except Refused:
        return None


def fit_links(plan: WordPlan, ops: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """Make the caller's create_link operations fit the plan (see the module text). A link that
    already carries a re-check by keeps it. A world with no single generic meaning leaves a
    generic phrase as it is, and the writer refuses that link."""
    generic_phrases = _generic_phrases(plan)
    rewritten = []
    for op in ops:
        meaning = op.get("meaning") if op.get("op") == "create_link" else None
        before = _resolves(plan.base, meaning) if isinstance(meaning, str) else None
        if before is None or before.outcome != NONE:
            # Not a link phrase, or one that names an existing word: the caller's, untouched.
            rewritten.append(op)
            continue
        after = _resolves(plan.registry, meaning)
        target = None
        # A planned row's id and a generic phrase's row id are the plan's own: never a phrase.
        if after is not None and after.target is not None and after.via != "id":
            target = after.target
        elif plan.generic_meaning is not None and (
            (found := _resolves(generic_phrases, meaning)) is not None
            and found.outcome != NONE
            and found.via != "id"
        ):
            op = {**op, "meaning": plan.generic_meaning["id"], "original_phrase": meaning}
            target = plan.generic_meaning
        if target is not None and op.get("recheck_by") is None:
            recheck = _recheck_by(now, target.get("temporal_class"), target.get("default_horizon"))
            if recheck is not None:
                op = {**op, "recheck_by": recheck}
        rewritten.append(op)
    return rewritten
