"""Adapter probes with invented media only. They do not prove end-to-end ingestion."""

import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from grove import clock
from grove.ids import new_id
from grove.ingest.connector import ReadBudget
from grove.ingest.images import prepare_image, read_image
from grove.ingest.voice import transcribe_wav
from grove.models.layer import ModelLayer
from tests.models import test_live as live_helpers

evidence = live_helpers.evidence
live_settings = live_helpers.live_settings

pytestmark = pytest.mark.live


def test_live_voice_turns_and_hints_are_metered(driver, world, tmp_path, evidence):
    say = shutil.which("say")
    assert say is not None, "the synthetic probe requires macOS say"
    target = tmp_path / "synthetic.wav"
    subprocess.run(
        [
            say,
            "-o",
            str(target),
            "--data-format=LEI16@16000",
            "The Kestrel orchard has twelve pear trees. [[slnc 1200]] "
            "The ladders are beside row three.",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with ModelLayer(driver, settings=live_settings()) as layer:
        try:
            result = transcribe_wav(
                layer,
                world,
                target.read_bytes(),
                budget=ReadBudget(),
                keywords=["Kestrel"],
                language_bias=["English"],
            )
        finally:
            rows = layer.meter.calls(world, clock.now(world).date())
            evidence(rows)
    assert result.turns and any("orchard" in t.provider.text.lower() for t in result.turns)
    assert all(t.at is None for t in result.turns)
    assert all(0 <= t.provider.start_ms <= t.provider.end_ms for t in result.turns)
    assert len(rows) == 1 and rows[0]["outcome"] == "ok"
    assert rows[0]["model"] == "muse-voice-transcribe-1.0"
    evidence(
        rows,
        turn_count=len(result.turns),
        offsets=[[t.provider.start_ms, t.provider.end_ms] for t in result.turns],
    )


def test_live_glimmer_reads_a_synthetic_image_and_meters_the_call(driver, world, evidence):
    image = Image.new("RGB", (800, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (30, 30),
        "KESTREL ORCHARD\nRECEIPT: 12 PEARS",
        fill="black",
        font=ImageFont.load_default(size=42),
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    prepared = prepare_image(out.getvalue(), budget=ReadBudget())
    with ModelLayer(driver, settings=live_settings()) as layer:
        try:
            result = read_image(layer, world, prepared, source_id=new_id(world))
        finally:
            rows = layer.meter.calls(world, clock.now(world).date())
            evidence(rows)
    assert "KESTREL" in result.text.upper() and "12" in result.text
    assert len(rows) == 1 and rows[0]["model"] == "muse-glimmer-30b"
    assert rows[0]["route"] == "modal" and rows[0]["outcome"] == "ok"
    evidence(rows, image_sha256=prepared.sha256, text=result.text)
