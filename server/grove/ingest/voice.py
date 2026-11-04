"""Internal voice adapter. Upload registration and source persistence are separate stages.

The caller supplies decoded mono PCM and commits the source/episode first. This adapter keeps
provider detection offsets and source timestamps distinct. It never resolves diarization labels
to people or creates a source type from a model's words.
"""

import hashlib
import io
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from grove.ingest.connector import NotAnExport, ReadBudget
from grove.models.errors import InvalidOutput
from grove.models.layer import ModelLayer
from grove.models.voice import VoiceTurn


@dataclass(frozen=True)
class TimedTurn:
    provider: VoiceTurn
    at: datetime | None


@dataclass(frozen=True)
class VoiceText:
    sha256: str
    turns: tuple[TimedTurn, ...]
    model_call_ids: tuple[str, ...]
    original_wav: bytes = field(repr=False)


def transcribe_wav(
    layer: ModelLayer,
    world_id: str,
    data: bytes,
    *,
    budget: ReadBudget,
    source_at: datetime | None = None,
    keywords: list[str] | None = None,
    language_bias: list[str] | None = None,
) -> VoiceText:
    """Transcribe normalized audio with actual turns, never inferred timing from joined text.

    Only the source's own aware start time can date its turns. A missing time stays missing.
    The WAV is the retained input at this stage; an outer decoder must retain the original
    compressed file and its hash when there is one.
    """
    if source_at is not None and (
        not isinstance(source_at, datetime) or source_at.utcoffset() is None
    ):
        raise NotAnExport("a voice source time must include its offset")
    if type(data) is not bytes:
        raise NotAnExport("the voice input must be WAV bytes")
    budget.take_bytes(len(data))
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.getnframes()
            if (
                reader.getnchannels() != 1
                or reader.getsampwidth() != 2
                or reader.getframerate() not in (16_000, 24_000)
                or reader.getcomptype() != "NONE"
                or frames < 1
            ):
                raise NotAnExport("the voice input must be normalized mono 16-bit PCM")
            if frames * 2 > len(data) or len(reader.readframes(frames)) != frames * 2:
                raise NotAnExport("the WAV frame data is incomplete")
    except (wave.Error, EOFError):
        raise NotAnExport("the voice input is not a readable WAV") from None
    transcript = layer.transcribe(
        world_id,
        audio_wav=data,
        mode="DIARIZATION",
        keywords=keywords,
        language_bias=language_bias,
    )
    if not any(turn.text.strip() for turn in transcript.turns):
        raise InvalidOutput(
            "the audio yielded no transcribed turns",
            details={"model_call_ids": list(transcript.model_call_ids)},
        )
    turns = tuple(
        TimedTurn(
            turn, None if source_at is None else source_at + timedelta(milliseconds=turn.start_ms)
        )
        for turn in transcript.turns
    )
    return VoiceText(hashlib.sha256(data).hexdigest(), turns, transcript.model_call_ids, data)
