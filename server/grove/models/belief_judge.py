"""Code-controlled D9 pairwise judgments. Caller text never becomes a verdict.

The source and belief are read within one world. Every model response is metered by
ModelLayer and its constrained verdict is recorded before a writer operation can cite it.
"""

import json
import math
from typing import Any

from grove.models.errors import InvalidOutput
from grove.models.layer import ModelLayer
from grove.models.verify_judge import UNREACHABLE, JudgeInputError, cut
from grove.prompts import DataBlock, build_prompt
from grove.store.primitives import KIND_BELIEF
from grove.store.query import WorldQuery
from grove.writer.judgments import (
    MAX_SUBJECT_VALUE,
    JudgmentRow,
    record_judgments,
    store_subject_value,
)

MAX_EXCERPT = 20000
_MODES = frozenset({"evidence", "same_belief", "field_fit"})


def strict_excerpt(content: str, locator: str) -> str:
    """A retained message key is an exact JSON key, never a substring search."""
    if locator.startswith("key:"):
        try:
            messages = json.loads(content)
        except (TypeError, ValueError):
            raise JudgeInputError("a key locator requires retained messages") from None
        if not isinstance(messages, list):
            raise JudgeInputError("a key locator requires retained messages")
        found = [m for m in messages if isinstance(m, dict) and m.get("key") == locator[4:]]
        if len(found) != 1 or not isinstance(found[0].get("text"), str):
            raise JudgeInputError("the key must identify exactly one retained message")
        text = found[0]["text"]
    elif locator == "whole":
        text = content
    else:
        # The D4 locator grammar remains authoritative. Refuse a long source rather than
        # let cut silently discard a qualifying clause of the selected excerpt.
        text = cut(content, locator)
        if len(text) >= 4000:
            raise JudgeInputError("use a smaller exact locator for this source")
    if not text or len(text) > MAX_EXCERPT:
        raise JudgeInputError("the selected excerpt must contain 1 to 20000 characters")
    return text


def _belief(query: WorldQuery, item_id: Any) -> dict:
    if not isinstance(item_id, str):
        raise JudgeInputError("belief_id must be an item id")
    rows = query.read("MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    if not rows or rows[0]["i"].get("kind") != KIND_BELIEF:
        raise JudgeInputError("belief_id names no belief in this world")
    return rows[0]["i"]


def _belief_block(item: dict) -> DataBlock:
    return DataBlock(
        "belief",
        "automated",
        None,
        item["id"],
        json.dumps({"title": item.get("title"), "body": item.get("body")}, ensure_ascii=False),
    )


def _source(query: WorldQuery, source_id: Any, locator: Any) -> str:
    if not isinstance(source_id, str) or not isinstance(locator, str):
        raise JudgeInputError("a source reference requires source_id and locator")
    rows = query.read("MATCH (s:Source {id: $id}) RETURN s", id=source_id)
    if not rows or rows[0]["s"].get("forget_state") == "forgotten":
        raise JudgeInputError("source_id names no retained source in this world")
    content = rows[0]["s"].get("content")
    if not isinstance(content, str):
        raise JudgeInputError("the source has no retained text")
    return strict_excerpt(content, locator)


def _prepared(query: WorldQuery, body: dict) -> tuple[str, list[DataBlock], dict, list[str]]:
    mode = body.get("mode")
    if not isinstance(mode, str) or mode not in _MODES:
        raise JudgeInputError("mode must be evidence, same_belief or field_fit")
    if mode == "evidence":
        if set(body) - {"mode", "belief_id", "evidence_id", "source_id", "locator"}:
            raise JudgeInputError("unknown evidence judgment field")
        item = _belief(query, body.get("belief_id"))
        evidence_id = body.get("evidence_id")
        if evidence_id is not None:
            if not isinstance(evidence_id, str):
                raise JudgeInputError("evidence_id must be an evidence id")
            if "source_id" in body or "locator" in body:
                raise JudgeInputError("cite an evidence row or a source, not both")
            rows = query.read("MATCH (e:Evidence {id: $id}) RETURN e", id=evidence_id)
            if not rows or rows[0]["e"].get("active") is not True:
                raise JudgeInputError("evidence_id names no active evidence in this world")
            row = rows[0]["e"]
            text = _source(query, row.get("source_id"), row.get("locator"))
            binding = {"evidence_id": evidence_id}
            block = DataBlock("source", row["speaker_kind"], row["source_type"], evidence_id, text)
        else:
            text = _source(query, body.get("source_id"), body.get("locator"))
            binding = {"source_id": body["source_id"], "locator": body["locator"]}
            block = DataBlock("source", "third_party", None, body["source_id"], text)
        return (
            f"item:{item['id']}",
            [_belief_block(item), block],
            binding,
            ["supports", "contradicts", "not_about_claim"],
        )
    if mode == "same_belief":
        if set(body) != {"mode", "survivor", "absorbed"}:
            raise JudgeInputError("same_belief requires survivor and absorbed")
        first, second = _belief(query, body["survivor"]), _belief(query, body["absorbed"])
        if first["id"] == second["id"]:
            raise JudgeInputError("same_belief requires two different beliefs")
        subject = "|".join(sorted([f"belief:{first['id']}", f"belief:{second['id']}"]))
        return subject, [_belief_block(first), _belief_block(second)], {}, ["same", "different"]
    if set(body) != {"mode", "title", "subject_refs", "externally_testable", "retest_how"}:
        raise JudgeInputError("field_fit requires the proposed belief and its retest fields")
    subjects = body["subject_refs"]
    if (
        not isinstance(body["title"], str)
        or not body["title"].strip()
        or len(body["title"]) > 1000
        or body["externally_testable"] is not True
        or not isinstance(subjects, list)
        or not 1 <= len(subjects) <= 50
        or any(not isinstance(ref, str) for ref in subjects)
        or len(set(subjects)) != len(subjects)
        or not isinstance(body["retest_how"], dict)
    ):
        raise JudgeInputError("invalid proposed belief fields")
    found = query.read("MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id", ids=subjects)
    if {row["id"] for row in found} != set(subjects):
        raise JudgeInputError("a subject is not in this world")
    value = {key: body[key] for key in ("title", "externally_testable", "retest_how")}
    value["subject_refs"] = sorted(subjects)
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(text) > MAX_SUBJECT_VALUE:
        raise JudgeInputError("the proposed fields are too large")
    return (
        text,
        [DataBlock("question", "automated", None, None, text)],
        {},
        ["fits", "does_not_fit"],
    )


def judge_belief(layer: ModelLayer, driver: Any, world_id: str, body: dict, *, run_id: str) -> dict:
    """Record two fixed judgments with their exact row or source binding."""
    if not isinstance(body, dict):
        raise JudgeInputError("the judgment input is an object")
    subject, blocks, binding, verdicts = _prepared(WorldQuery(driver, world_id), body)
    mode = body["mode"]
    if mode == "field_fit":
        subject = store_subject_value(driver, world_id, subject)
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": verdicts},
            "confidence": {"type": "number"},
        },
        "required": ["verdict", "confidence"],
        "additionalProperties": False,
    }
    # A fixed mode label is data, and no caller string can alter the instruction part.
    blocks = [DataBlock("question", "automated", None, None, mode), *blocks]
    first_prompt = build_prompt("grove.belief-judge.v1", blocks)
    first = layer.respond(
        world_id,
        job="word_judge",
        effort="high",
        instructions=first_prompt.instructions,
        input=first_prompt.data,
        schema=schema,
        schema_name="belief_verdict",
        run_id=run_id,
    )
    second_prompt = build_prompt("grove.belief-second.v1", blocks)
    correlated = False
    try:
        second = layer.chat(
            world_id,
            job="second_opinion",
            messages=second_prompt.messages(),
            schema=schema,
            schema_name="belief_verdict",
            run_id=run_id,
        )
    except UNREACHABLE:
        correlated = True
        second = layer.respond(
            world_id,
            job="second_opinion_fallback",
            instructions=second_prompt.instructions,
            input=second_prompt.data,
            schema=schema,
            schema_name="belief_verdict",
            run_id=run_id,
        )
    role = {
        "evidence": "contradicts_or_complements",
        "same_belief": "same_belief",
        "field_fit": "field_fit",
    }[mode]
    ids, calls, results = [], [], []
    for index, (result, correlation) in enumerate(((first, False), (second, correlated))):
        verdict, confidence = result.data.get("verdict"), result.data.get("confidence")
        if (
            verdict not in verdicts
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise InvalidOutput("the belief judge returned an invalid verdict")
        ids.extend(
            record_judgments(
                driver,
                world_id,
                result.model_call_id,
                [
                    JudgmentRow(
                        role=("first_judge" if index == 0 else "second_judge")
                        if mode == "evidence"
                        else role,
                        subject=subject,
                        verdict=verdict,
                        stated_confidence=float(confidence),
                        correlated=correlation,
                        **binding,
                    )
                ],
            )
        )
        calls.append(result.model_call_id)
        results.append(verdict)
    answer = {
        "judgment_ids": ids,
        "model_call_ids": calls,
        "verdicts": results,
        "correlated": correlated,
    }
    if mode == "field_fit":
        answer["field_subject"] = subject
    return answer
