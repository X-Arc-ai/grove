"""A synthetic D9 judgment through the actual metered providers. Never in CI."""

import json
import os
from pathlib import Path

import pytest

from grove.models.belief_judge import judge_belief
from grove.models.layer import ModelLayer
from grove.store.primitives import KIND_BELIEF
from grove.writer.testing import plant_node
from tests.models.test_live import live_settings
from tests.writer.helpers import make_source, seeded  # noqa: F401

pytestmark = pytest.mark.live


def test_live_two_judges_recognize_synthetic_counterexample(driver, seeded, request):  # noqa: F811
    w = seeded["world_id"]
    target = plant_node(
        driver, w, "Item", kind=KIND_BELIEF, title="Kestrel ferries never run on Sunday", version=1
    )["id"]
    source = make_source(
        driver,
        w,
        "web_pages",
        ["Kestrel ferry log: on Sunday, the ferry departed pier 3 at noon with passengers."],
    )
    with ModelLayer(driver, settings=live_settings()) as layer:
        answer = judge_belief(
            layer,
            driver,
            w,
            {
                "mode": "evidence",
                "belief_id": target,
                "source_id": source["source_id"],
                "locator": "whole",
            },
            run_id="belief-live-judge",
        )
    folder = os.environ.get("GROVE_LIVE_EVIDENCE_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / f"{request.node.name}.json").write_text(
            json.dumps(answer, indent=2) + "\n", encoding="utf-8"
        )
    assert len(set(answer["model_call_ids"])) == 2
    assert answer["verdicts"] == ["contradicts", "contradicts"]
