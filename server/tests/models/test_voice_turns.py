"""Voice provenance: provider turns survive chunking, and malformed output stays failed."""

import json

import pytest

from grove.models.errors import InvalidOutput, ModelRefused
from tests.models import test_layer as layer_helpers
from tests.models.test_layer import (
    FakeProvider,
    model_calls,
    multipart,
    reply,
    wav,
)

make_layer = layer_helpers.make_layer
test_clock = layer_helpers.test_clock


def turn(text="The orchard is ready.", start=120, end=900, speaker="A"):
    return {"turnId": 0, "startMs": start, "endMs": end, "transcript": text, "speaker": speaker}


def test_voice_turns_preserve_offsets_and_scope_speakers_to_each_chunk(driver, world, make_layer):
    provider = FakeProvider(
        reply({"transcript": "first", "audioDurationMs": 1, "turns": [turn("first")]}),
        reply({"transcript": "second", "turns": [turn("second", 250, 750)]}),
    )
    result = make_layer(provider).transcribe(
        world,
        audio_wav=wav(591, rate=1000, width=1)[0],
        mode="DIARIZATION",
        keywords=["Lio Varn"],
        language_bias=["English", "French"],
    )
    request = json.loads(multipart(provider.requests[0])["request"][1])
    assert request["mode"] == "DIARIZATION"
    assert request["keywords"] == ["Lio Varn"]
    assert request["languageBias"] == ["English", "French"]
    assert [(t.start_ms, t.end_ms, t.chunk_index, t.speaker) for t in result.turns] == [
        (120, 900, 0, "A"),
        (590250, 590750, 1, "A"),
    ]
    assert [t.text for t in result.turns] == ["first", "second"]
    assert [t.model_call_id for t in result.turns] == list(result.model_call_ids)
    assert len(model_calls(driver, world)) == 2


def test_voice_preserves_empty_partial_turn_without_inventing_text(world, make_layer):
    provider = FakeProvider(
        reply({"transcript": "first", "turns": [turn("first"), turn("", 950, 990)]})
    )
    # Each turn has its own identifier within the request.
    provider._replies[0][1]["turns"][1]["turnId"] = 1
    result = make_layer(provider).transcribe(
        world, audio_wav=wav(1, rate=1000, width=1)[0], mode="ENDPOINTING"
    )
    assert len(result.turns) == 2
    assert result.turns[1].text == ""
    assert result.turns[1].start_ms == 950


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [{"turnId": 0}],
        [turn(start=-1)],
        [turn(start=901, end=900)],
        [turn(start=True)],
        [turn(end=1001)],
        [turn(speaker=42)],
    ],
)
def test_invalid_voice_turns_are_metered_as_invalid_output(driver, world, make_layer, bad):
    provider = FakeProvider(reply({"transcript": "words", "turns": bad}))
    with pytest.raises(InvalidOutput):
        make_layer(provider).transcribe(
            world, audio_wav=wav(1, rate=1000, width=1)[0], mode="DIARIZATION"
        )
    assert [r["outcome"] for r in model_calls(driver, world)] == ["invalid_output"]
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "guess"},
        {"language_bias": "English"},
        {"keywords": ["bad\ud800"]},
        {"keywords": [1]},
    ],
)
def test_invalid_voice_options_make_no_provider_call(world, make_layer, args):
    provider = FakeProvider()
    with pytest.raises(ModelRefused):
        make_layer(provider).transcribe(world, audio_wav=wav(1, rate=1000, width=1)[0], **args)
    assert provider.requests == []


def test_push_to_talk_keeps_legacy_request_shape(world, make_layer):
    provider = FakeProvider(reply({"transcript": "words"}))
    result = make_layer(provider).transcribe(world, audio_wav=wav(1, rate=1000, width=1)[0])
    request = json.loads(multipart(provider.requests[0])["request"][1])
    assert request == {
        "model": "muse-voice-transcribe-1.0",
        "audioEncoding": "WAV",
        "mode": "PUSH_TO_TALK",
    }
    assert result.turns == ()
