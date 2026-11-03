"""The two word judges live, on synthetic text, through to the writer. Never in CI (R9).

Run in a lane, from server/:
    GROVE_SLICE_ID=ontology GROVE_LIVE_EVIDENCE_DIR=<dir> \\
        uv run pytest tests/ontology/test_live_judge.py -m live
The verdicts are the models' own, so the test asserts what the build guarantees for any verdict:
two same-word judgments from two model calls (Glimmer, or a correlated Spark), recorded before the
batch, and a writer outcome that follows them (an alias only when both say same). It writes the
rows it made, never a key or a prompt, to GROVE_LIVE_EVIDENCE_DIR.
"""

import json
import os
from pathlib import Path

import pytest

from grove.models.layer import ModelLayer
from grove.ontology import plan_words
from grove.writer import apply_batch, render_receipt
from tests.models.test_live import live_settings
from tests.writer.helpers import LEARNER, create_item, create_link, new_op_id, observation, read

pytestmark = pytest.mark.live

CASES = {
    "is godparent of": "Ilse stood as godmother to Teodor at the christening in May.",
    "is associated with": "Ilse is associated with the Quill rowing club through her brother.",
}


def proposal(name: str) -> dict:
    return {
        "op_id": new_op_id(),
        "word_type": "meaning",
        "name": name,
        "description": f"The first person {name} the second.",
        "reason": f"No meaning in the registry says that one person {name} another.",
        "worked_example": {"ev_id": "e1"},
        "neighbours": [
            {"word": "related to", "distinction": "related to records a connection and no role"},
            {"word": "member of", "distinction": "member of joins a person to an organisation"},
        ],
        "declared": {
            "from_kinds": {"kinds": ["person"]},
            "to_kinds": {"kinds": ["person"]},
            "direction": "from_to",
            "cardinality": "many",
            "carries_consequences": False,
            "temporal_class": "permanent",
        },
        "example": CASES[name],
    }


def _plain(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


@pytest.mark.parametrize("name", sorted(CASES))
def test_live_word_judges_record_two_verdicts_and_the_writer_follows_them(
    driver, seeded, chat, name, request
):
    world_id = seeded["world_id"]
    with ModelLayer(driver, settings=live_settings()) as layer:
        plan = plan_words(layer, driver, world_id, [proposal(name)])
    assert len(plan.ops) == 1, plan

    judgments = read(
        driver,
        world_id,
        "MATCH (j:Judgment) OPTIONAL MATCH (c:ModelCall {id: j.model_call_id}) "
        "RETURN j.role AS role, j.verdict AS verdict, j.correlated AS correlated, "
        "j.stated_confidence AS confidence, c.model AS model, c.job AS job",
    )
    calls = read(
        driver,
        world_id,
        "MATCH (c:ModelCall) RETURN c.job AS job, c.model AS model, c.outcome AS outcome, "
        "c.input_tokens AS input_tokens, c.output_tokens AS output_tokens, c.cost AS cost",
    )
    same_word = [row for row in judgments if row["role"] == "same_word"]
    assert len(same_word) == 2
    assert len({row["model"] for row in same_word}) == 2 or any(r["correlated"] for r in same_word)

    phrase_link = create_link("$ilse", name, "$teo", "e1", recheck_by="2027-06-01T00:00:00+00:00")
    batch = {
        "evidence": [observation(chat, "e1", 1)],
        "ops": [
            create_item("person", "Ilse Marrow", "e1", temp_id="$ilse"),
            create_item("person", "Teodor Quill", "e1", temp_id="$teo"),
            *plan.ops,
            phrase_link,
        ],
    }
    receipt = apply_batch(driver, world_id, LEARNER, batch)
    minted = read(driver, world_id, "MATCH (m:Meaning {name: $n}) RETURN m.status AS s", n=name)
    aliased = read(driver, world_id, "MATCH (a:Alias {phrase: $n}) RETURN a.status AS s", n=name)
    both_same = all(row["verdict"] == "same" for row in same_word)
    assert (aliased != []) == both_same and (minted != []) == (not both_same)

    folder = os.environ.get("GROVE_LIVE_EVIDENCE_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        doc = {
            "test": request.node.name,
            "proposal": name,
            "judgments": [{k: _plain(v) for k, v in row.items()} for row in judgments],
            "model_calls": [{k: _plain(v) for k, v in row.items()} for row in calls],
            "outcome": "alias" if both_same else "mint",
            "word": minted or aliased,
            "receipt": render_receipt(driver, world_id, receipt),
        }
        target = Path(folder) / f"{request.node.name}.json"
        target.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
