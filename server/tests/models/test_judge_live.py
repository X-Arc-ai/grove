"""The verification judges with real model calls on synthetic text. Never in CI.

Run in a lane, from server/:

    GROVE_SLICE_ID=truth-clock GROVE_LIVE_EVIDENCE_DIR=<dir> \
        uv run pytest tests/models/test_judge_live.py -m live

One first-judge call on Spark and one second judge: Glimmer when it can be reached, else the
correlated Spark stand-in. The evidence file holds the model call ids, the verdicts and the token
counts, never a prompt, an excerpt or a key. Every name below is invented.
"""

import json
import os
from pathlib import Path

import pytest

from grove.models.judge import VERDICTS, Piece, judge, second_judge
from grove.models.layer import ModelLayer
from grove.store.query import WorldQuery
from grove.writer.testing import plant_node
from tests.models.test_live import live_settings
from tests.writer.helpers import make_source, seeded  # noqa: F401

pytestmark = pytest.mark.live

TIMETABLE = "Kestrel ferries, winter timetable for Velmora: the first ferry leaves pier 3 at 07:40."
UNRELATED = "Odalys Pennick repainted the Brannock shed green on Saturday."


def _judged(judged) -> dict:
    return {
        "model_call_id": judged.model_call_id,
        "model": judged.model,
        "job": judged.job,
        "effort": judged.effort,
        "correlated": judged.correlated,
        "verdicts": [
            {"block": v.block, "verdict": v.verdict, "confidence": v.confidence}
            for v in judged.verdicts
        ],
        "tokens": {
            "input": judged.usage.input_tokens,
            "cached": judged.usage.cached_tokens,
            "output": judged.usage.output_tokens,
            "reasoning": judged.usage.reasoning_tokens,
            "estimated": judged.usage.estimated,
        },
    }


def test_live_first_and_second_judge_on_synthetic_text(driver, seeded, request):  # noqa: F811
    w = seeded["world_id"]
    kinds = {
        row["name"]: row["id"]
        for row in WorldQuery(driver, w).read("MATCH (k:Kind) RETURN k.id AS id, k.name AS name")
    }
    claim = plant_node(
        driver,
        w,
        "Item",
        kind=kinds["task"],
        title="Catch the first Kestrel ferry to Velmora at 07:40",
        status="open",
        status_role="open",
        properties="{}",
        became_true_precision="unknown",
        version=1,
    )["id"]
    page = make_source(driver, w, "web_pages", [TIMETABLE])
    notes = make_source(driver, w, "notes_files_releases", [UNRELATED])
    pieces = [
        Piece(
            text=TIMETABLE,
            speaker_kind="automated",
            source_type="confirmed",
            source_id=page["source_id"],
            locator=f"char:0-{len(TIMETABLE)}",
        ),
        Piece(
            text=UNRELATED,
            speaker_kind="person",
            source_type="confirmed",
            source_id=notes["source_id"],
            locator="line:1",
        ),
    ]

    with ModelLayer(driver, settings=live_settings()) as layer:
        first = judge(layer, driver, w, claim_id=claim, pieces=pieces, run_id="judge-live-1")
        second = second_judge(layer, driver, w, claim_id=claim, pieces=pieces)

    folder = os.environ.get("GROVE_LIVE_EVIDENCE_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        doc = {"test": request.node.name, "first": _judged(first), "second": _judged(second)}
        target = Path(folder) / f"{request.node.name}.json"
        target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    rows = WorldQuery(driver, w).read(
        "MATCH (j:Judgment) RETURN j.model_call_id AS call, j.role AS role, "
        "j.correlated AS correlated, j.verdict AS verdict"
    )
    assert len(rows) == 4
    assert {row["verdict"] for row in rows} <= set(VERDICTS)
    assert first.model == "muse-spark-1.3" and first.job == "verification"
    assert [v.verdict for v in first.verdicts][1] == "not_about_claim"
    assert second.correlated == (second.model == "muse-spark-1.3")
    assert {
        (row["role"], bool(row["correlated"]))
        for row in rows
        if row["call"] == second.model_call_id
    } == {("second_judge", second.correlated)}
