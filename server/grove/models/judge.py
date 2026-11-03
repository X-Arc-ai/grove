"""The two judges of a proposed word (D8 step 6, D15), in the model-call layer.

Spark judges first (job `word_judge`). Glimmer on Modal judges second (job `second_opinion`): the
same question, and whether each declared field fits the worked example. When Glimmer cannot be
reached, a second Spark call with a different prompt stands in (job `second_opinion_fallback`),
recorded as correlated (line 1668), and the writer's gate still needs the two verdicts to agree.

Each call's constrained output becomes judgment rows through grove.writer.judgments, before the
batch reaches the writer: no network call happens inside a transaction (D2). The batch then cites
the judgment ids, and the writer reads the verdicts from the store, never from the caller.
"""

import json
from dataclasses import dataclass

from grove.models.errors import InvalidOutput, NotConfigured, ProviderError, RateLimited
from grove.models.layer import ModelLayer, Result
from grove.prompts import DataBlock, build_prompt
from grove.store.connection import Driver
from grove.store.query import WorldQuery
from grove.writer.judgments import JudgmentRow, record_judgments, store_subject_value
from grove.writer.ontology import (
    DIFFERENT,
    DOES_NOT_FIT,
    FIELD_FIT,
    FITS,
    SAME,
    SAME_WORD,
    CheckedProposal,
    field_fit_value,
    same_word_subject,
    same_word_value,
)

FIRST_JOB = "word_judge"
SECOND_JOB = "second_opinion"
FALLBACK_JOB = "second_opinion_fallback"
FIRST_PROMPT = "grove.word-judge.v1"
SECOND_PROMPT = "grove.word-second-opinion.v1"
FALLBACK_PROMPT = "grove.word-second-opinion-fallback.v1"
OUTPUT_TOKENS = 2048
# Glimmer could not be reached, or answered nothing usable: the correlated Spark judge stands in.
# A budget or run cap stops the pass instead, so its requests stay queued.
UNREACHABLE = (NotConfigured, ProviderError, RateLimited, InvalidOutput)

_VERDICT = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": [SAME, DIFFERENT]},
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "confidence"],
    "additionalProperties": False,
}
_FIT = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": [FITS, DOES_NOT_FIT]}},
    "required": ["verdict"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class WordJudgment:
    """The recorded verdicts of one proposal: cite `judgment_ids` in its propose_word."""

    judgment_ids: tuple[str, ...]
    same_word: tuple[str, ...]
    fields: dict[str, str]
    correlated: bool
    model_call_ids: tuple[str, ...]


def _first_schema() -> dict:
    return {
        "type": "object",
        "properties": {SAME_WORD: _VERDICT},
        "required": [SAME_WORD],
        "additionalProperties": False,
    }


def _second_schema(fields: tuple[str, ...], with_pair: bool) -> dict:
    properties: dict = {}
    required = []
    if with_pair:
        properties[SAME_WORD] = _VERDICT
        required.append(SAME_WORD)
    properties["fields"] = {
        "type": "object",
        "properties": {name: _FIT for name in fields},
        "required": list(fields),
        "additionalProperties": False,
    }
    required.append("fields")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _text_of(driver: Driver, world_id: str, value_id: str | None) -> str:
    if not value_id:
        return ""
    rows = WorldQuery(driver, world_id).read(
        "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value", id=value_id
    )
    return str(rows[0]["value"]) if rows else ""


def _blocks(
    driver: Driver, world_id: str, checked: CheckedProposal, example: str
) -> list[DataBlock]:
    proposed = {
        "type": checked.word_type,
        "name": checked.name,
        "description": checked.description,
        "declared fields": checked.field_values,
        "fields to judge": list(checked.disputable),
        "reads the existing word from the other end": checked.reversed,
    }
    if checked.neighbours:
        row = checked.neighbours[0].row
        nearest = {
            "name": row.get("name"),
            "description": _text_of(driver, world_id, row.get("description")),
        }
    else:
        nearest = {"name": None, "description": "The registry holds no word of this type yet."}
    return [
        DataBlock("question", "automated", None, None, json.dumps(proposed, ensure_ascii=False)),
        DataBlock("question", "automated", None, None, json.dumps(nearest, ensure_ascii=False)),
        DataBlock("source", "person", None, None, example),
    ]


def _confidence(answer: dict) -> float | None:
    value = answer.get("confidence")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return min(1.0, max(0.0, float(value)))


def _respond(
    layer: ModelLayer,
    world_id: str,
    job: str,
    prompt_name: str,
    blocks: list[DataBlock],
    schema: dict,
    run_id: str | None,
) -> Result:
    prompt = build_prompt(prompt_name, blocks)
    return layer.respond(
        world_id,
        job=job,
        instructions=prompt.instructions,
        input=prompt.data,
        schema=schema,
        schema_name="verdict",
        run_id=run_id,
        max_output_tokens=OUTPUT_TOKENS,
    )


def judge_word(
    layer: ModelLayer,
    driver: Driver,
    world_id: str,
    checked: CheckedProposal,
    *,
    example: str,
    run_id: str | None = None,
) -> WordJudgment:
    """Both judges on one proposal that passed `check_proposal`. `example` is the screened source
    text of the worked example. Raises BudgetReached or RunCapReached from the layer: the pass
    then leaves its requests queued."""
    blocks = _blocks(driver, world_id, checked, example)
    subject = None
    if checked.neighbours:
        name_ref = store_subject_value(
            driver,
            world_id,
            json.dumps(
                same_word_value(checked.word_type, checked.name, checked.reversed),
                sort_keys=True,
                ensure_ascii=False,
            ),
        )
        subject = same_word_subject(name_ref, checked.word_type, checked.neighbours[0].row["id"])
    field_refs = {
        name: store_subject_value(
            driver,
            world_id,
            json.dumps(
                field_fit_value(checked.word_type, checked.name, name, value, checked.kind_id),
                sort_keys=True,
                ensure_ascii=False,
            ),
        )
        for name, value in checked.field_values.items()
    }

    ids: list[str] = []
    verdicts: list[str] = []
    calls: list[str] = []
    if subject is not None:
        first = _respond(layer, world_id, FIRST_JOB, FIRST_PROMPT, blocks, _first_schema(), run_id)
        answer = first.data[SAME_WORD]
        ids += record_judgments(
            driver,
            world_id,
            first.model_call_id,
            [JudgmentRow(SAME_WORD, subject, answer["verdict"], _confidence(answer))],
        )
        verdicts.append(answer["verdict"])
        calls.append(first.model_call_id)
    if subject is None and not field_refs:
        return WordJudgment(tuple(ids), tuple(verdicts), {}, False, tuple(calls))

    schema = _second_schema(checked.disputable, subject is not None)
    correlated = False
    try:
        prompt = build_prompt(SECOND_PROMPT, blocks)
        second = layer.chat(
            world_id,
            job=SECOND_JOB,
            messages=prompt.messages(),
            schema=schema,
            schema_name="verdict",
            run_id=run_id,
            max_tokens=OUTPUT_TOKENS,
        )
    except UNREACHABLE:
        correlated = True
        second = _respond(layer, world_id, FALLBACK_JOB, FALLBACK_PROMPT, blocks, schema, run_id)
    rows = []
    if subject is not None:
        answer = second.data[SAME_WORD]
        rows.append(
            JudgmentRow(SAME_WORD, subject, answer["verdict"], _confidence(answer), correlated)
        )
        verdicts.append(answer["verdict"])
    fields = {name: second.data["fields"][name]["verdict"] for name in checked.disputable}
    rows += [
        JudgmentRow(FIELD_FIT, field_refs[name], verdict, None, correlated)
        for name, verdict in fields.items()
    ]
    ids += record_judgments(driver, world_id, second.model_call_id, rows)
    calls.append(second.model_call_id)
    return WordJudgment(tuple(ids), tuple(verdicts), fields, correlated, tuple(calls))
