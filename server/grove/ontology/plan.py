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
4. Otherwise both judges run, and the operation cites their recorded judgments.

The plan reads the registry once. The writer reads it again under the world lock and checks every
operation, so a plan that went stale is refused there, never applied wrong.
"""

from dataclasses import dataclass
from typing import Any

from grove.models.judge import judge_word
from grove.models.layer import ModelLayer
from grove.store.connection import Driver
from grove.store.normalise import normalise_phrase
from grove.store.query import WorldQuery
from grove.writer.errors import Refused
from grove.writer.ontology import MINT_VALVE, SAME, check_proposal
from grove.writer.registry import Registry
from grove.writer.resolver import generic_meaning

# A proposal carries the text of its worked example beside the operation's fields; the plan drops
# it before the operation goes to the writer, which reads the evidence from the store.
EXAMPLE = "example"
VALVE_RULE = "mint_valve"


@dataclass(frozen=True)
class WordPlan:
    """`ops` are propose_word operations, in order, each citing its judgments. `generic` holds the
    meaning phrases that ride on the generic meaning; `held` the (word type, name, rule) of a kind
    or a property name the writer would refuse; `reused` the names that resolve already."""

    ops: tuple[dict[str, Any], ...]
    generic: tuple[str, ...]
    held: tuple[tuple[str, str, str], ...]
    reused: tuple[str, ...]


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
    generic: list[str] = []
    held: list[tuple[str, str, str]] = []
    reused: list[str] = []
    mints = 0
    for proposal in proposals:
        fields = {key: value for key, value in proposal.items() if key != EXAMPLE}
        name = str(fields.get("name") or "")
        word_type = str(fields.get("word_type") or "")
        try:
            checked = check_proposal(registry, fields)
        except Refused as refused:
            if refused.rule == "word_exists":
                reused.append(name)
            elif word_type == "meaning":
                generic.append(name)
            else:
                held.append((word_type, name, refused.rule))
            continue
        if mints >= valve:
            if word_type == "meaning":
                generic.append(name)
            else:
                held.append((word_type, name, VALVE_RULE))
            continue
        judged = judge_word(
            layer,
            driver,
            world_id,
            checked,
            example=str(proposal.get(EXAMPLE) or ""),
            run_id=run_id,
        )
        if not (
            judged.same_word and len(judged.same_word) >= 2 and set(judged.same_word) == {SAME}
        ):
            mints += 1
        ops.append({**fields, "op": "propose_word", "judgment_ids": list(judged.judgment_ids)})
    return WordPlan(tuple(ops), tuple(generic), tuple(held), tuple(reused))


def ride_on_generic(
    registry: Registry, ops: list[dict[str, Any]], phrases: tuple[str, ...] | list[str]
) -> list[dict[str, Any]]:
    """Rewrite each create_link whose meaning is one of these phrases to the generic connection
    meaning, with the phrase kept for the consolidation agent to re-type (line 1596). A world with
    no single generic meaning leaves the links as they are, and the writer refuses them."""
    wanted = {normalise_phrase(phrase) for phrase in phrases}
    generic = generic_meaning(registry)
    if generic is None or not wanted:
        return list(ops)
    rewritten = []
    for op in ops:
        meaning = op.get("meaning") if op.get("op") == "create_link" else None
        if isinstance(meaning, str) and normalise_phrase(meaning) in wanted:
            op = {**op, "meaning": generic["id"], "original_phrase": meaning}
        rewritten.append(op)
    return rewritten
