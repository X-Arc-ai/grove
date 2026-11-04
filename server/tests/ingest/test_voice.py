"""Synthetic audio preserves the source date, actual turns, and unresolved labels."""

import hashlib
import io
import wave
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from grove.ingest.connector import NotAnExport, ReadBudget
from grove.ingest.voice import transcribe_wav
from grove.models.errors import InvalidOutput
from grove.models.voice import VoiceTurn


def wav_bytes(rate=16_000, width=2, channels=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\0" * (rate * width * channels))
    return out.getvalue()


class Layer:
    def __init__(self, turns=None):
        self.calls = []
        self.turns = (
            turns
            if turns is not None
            else (
                VoiceTurn(0, 120, 800, 0, "call-one", "The orchard is ready.", "A"),
                VoiceTurn(1, 900, 950, 0, "call-one", "", "B"),
            )
        )

    def transcribe(self, world, **kwargs):
        self.calls.append((world, kwargs))
        return SimpleNamespace(turns=self.turns, model_call_ids=("call-one",))


@pytest.mark.parametrize("source_at", [None, datetime(2026, 10, 1, tzinfo=UTC)])
def test_voice_uses_source_time_and_retains_empty_partial_turn(source_at):
    data = wav_bytes()
    layer = Layer()
    result = transcribe_wav(
        layer,
        "test-world",
        data,
        budget=ReadBudget(),
        source_at=source_at,
        keywords=["Orla"],
        language_bias=["English"],
    )
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.original_wav == data
    assert len(result.turns) == 2 and result.turns[1].provider.text == ""
    expected = None if source_at is None else source_at + timedelta(milliseconds=120)
    assert result.turns[0].at == expected
    assert result.turns[0].provider.speaker == "A"  # A label, not a resolved person.
    assert layer.calls[0][1]["mode"] == "DIARIZATION"
    assert layer.calls[0][1]["keywords"] == ["Orla"]
    assert layer.calls[0][1]["language_bias"] == ["English"]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not audio",
        b"RIFF" + (12).to_bytes(4, "little") + b"WAVEJUNK" + (100).to_bytes(4, "little"),
        wav_bytes(rate=8000),
        wav_bytes(width=1),
        wav_bytes(channels=2),
        wav_bytes()[:-2],
    ],
)
def test_bad_or_unnormalized_wav_never_calls_a_model(data):
    layer = Layer()
    with pytest.raises(NotAnExport):
        transcribe_wav(layer, "test-world", data, budget=ReadBudget())
    assert layer.calls == []


@pytest.mark.parametrize(
    ("zone", "parts", "fold", "expected"),
    [
        ("America/New_York", (2026, 11, 1, 1, 30), 1, "2026-11-01T06:30:00.120000+00:00"),
        ("Europe/Paris", (2026, 10, 25, 2, 30), 1, "2026-10-25T01:30:00.120000+00:00"),
        (
            "America/New_York",
            (2026, 3, 8, 1, 59, 59, 900000),
            0,
            "2026-03-08T07:00:00.020000+00:00",
        ),
        (
            "America/New_York",
            (2026, 11, 1, 1, 59, 59, 900000),
            0,
            "2026-11-01T06:00:00.020000+00:00",
        ),
    ],
)
def test_voice_offsets_preserve_the_instant_around_dst_transitions(zone, parts, fold, expected):
    source_at = datetime(*parts, tzinfo=ZoneInfo(zone), fold=fold)
    result = transcribe_wav(
        Layer(), "test-world", wav_bytes(), budget=ReadBudget(), source_at=source_at
    )
    assert result.turns[0].at.isoformat() == expected


def test_naive_voice_timestamp_never_calls_a_model():
    layer = Layer()
    with pytest.raises(NotAnExport):
        transcribe_wav(
            layer, "test-world", wav_bytes(), budget=ReadBudget(), source_at=datetime(2026, 10, 1)
        )
    assert layer.calls == []


def test_audio_budget_precedes_a_model_call():
    layer = Layer()
    with pytest.raises(NotAnExport):
        transcribe_wav(layer, "test-world", wav_bytes(), budget=ReadBudget(max_bytes=10))
    assert layer.calls == []


def test_empty_transcription_stays_an_explicit_failure():
    with pytest.raises(InvalidOutput):
        transcribe_wav(Layer([]), "test-world", wav_bytes(), budget=ReadBudget())
