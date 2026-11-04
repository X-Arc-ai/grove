"""Synthetic audio conversion: complete artifacts, bounded decoding and frame provenance."""

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import wave

import pytest

from grove.ingest import audio
from grove.ingest.connector import NotAnExport, ReadBudget
from grove.models.audio import split_wav

DECODER_PRESENT = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_decoder = pytest.mark.skipif(not DECODER_PRESENT, reason="FFmpeg runtime is not installed")


def wav_bytes(*, rate=16_000, frames=1600, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * frames * channels * width)
    return output.getvalue()


def encoded(tmp_path, kind, *, frames=1600, rate=16_000):
    source = tmp_path / "synthetic.wav"
    target = tmp_path / ("synthetic.ogg" if kind == "ogg" else "synthetic.m4a")
    source.write_bytes(wav_bytes(rate=rate, frames=frames))
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-i",
            str(source),
            "-threads",
            "1",
            "-c:a",
            "libopus" if kind == "ogg" else "aac",
            "-y",
            str(target),
        ],
        check=True,
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        timeout=20,
        env={"PATH": os.defpath},
    )
    return target.read_bytes()


@needs_decoder
@pytest.mark.parametrize("rate", [16_000, 24_000])
def test_wav_normalization_preserves_original_and_exact_frames(rate):
    source = wav_bytes(rate=rate, frames=rate + 1)
    result = audio.normalize_audio(source, budget=ReadBudget(), sample_rate=rate)
    assert result.original == source
    assert result.original_sha256 == hashlib.sha256(source).hexdigest()
    assert result.normalized_sha256 == hashlib.sha256(result.wav).hexdigest()
    assert result.media_type == "audio/wav"
    assert result.frames == rate + 1
    assert result.seconds == (rate + 1) / rate
    with wave.open(io.BytesIO(result.wav), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, rate)
        assert len(reader.readframes(result.frames + 1)) == result.frames * 2


@needs_decoder
def test_stereo_resampling_keeps_sample_duration():
    source = wav_bytes(rate=48_000, frames=48_000, channels=2)
    result = audio.normalize_audio(source, budget=ReadBudget())
    assert result.frames == 16_000
    assert result.seconds == 1


@needs_decoder
@pytest.mark.parametrize("kind,media_type", [("ogg", "audio/ogg"), ("m4a", "audio/mp4")])
def test_compressed_audio_retains_artifact_and_normalizes(tmp_path, kind, media_type):
    source = encoded(tmp_path, kind, frames=16_000)
    result = audio.normalize_audio(source, budget=ReadBudget())
    assert result.original == source
    assert result.original_sha256 == hashlib.sha256(source).hexdigest()
    assert result.media_type == media_type
    assert result.sample_rate == 16_000
    assert 16_000 <= result.frames < 18_000
    assert result.normalized_sha256 == hashlib.sha256(result.wav).hexdigest()
    assert "original=" not in repr(result) and "wav=" not in repr(result)


@needs_decoder
def test_more_than_ten_minutes_remains_complete_across_model_chunks():
    frames = 16_000 * 601 + 1
    result = audio.normalize_audio(wav_bytes(frames=frames), budget=ReadBudget())
    pieces = split_wav(result.wav)
    assert len(pieces) == 2
    assert [part.seconds for part in pieces] == [590, 11 + 1 / 16_000]
    restored = bytearray()
    for piece in pieces:
        with wave.open(io.BytesIO(piece.data), "rb") as reader:
            assert reader.getnframes() / reader.getframerate() <= 600
            restored.extend(reader.readframes(reader.getnframes()))
    with wave.open(io.BytesIO(result.wav), "rb") as reader:
        assert bytes(restored) == reader.readframes(frames)
    assert len(restored) // 2 == frames == result.frames


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"not audio",
        "source.wav",
        b"http://audio.example/x",
        b"#EXTM3U\nfile:///private/source\n",
    ],
)
def test_no_paths_urls_playlists_or_empty_input(value, monkeypatch):
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(value, budget=ReadBudget())


def test_input_bound_precedes_decoder_and_hash(monkeypatch):
    monkeypatch.setattr(audio, "MAX_INPUT_BYTES", 4)
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    monkeypatch.setattr(audio.hashlib, "sha256", lambda _: pytest.fail("hash must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(wav_bytes(), budget=ReadBudget())


def test_shared_budget_refuses_before_decoder(monkeypatch):
    source = wav_bytes()
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(source, budget=ReadBudget(max_bytes=len(source) - 1))


@pytest.mark.parametrize("rate,channels,width", [(192_000, 1, 2), (1000, 1, 2), (16_000, 3, 2)])
def test_source_rate_and_channels_are_bounded(rate, channels, width, monkeypatch):
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(
            wav_bytes(rate=rate, channels=channels, width=width), budget=ReadBudget()
        )


@pytest.mark.parametrize("cut", [1, 17, 42])
def test_truncated_wav_never_reaches_decoder(cut, monkeypatch):
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(wav_bytes()[:-cut], budget=ReadBudget())


def test_wav_with_bad_block_alignment_is_refused(monkeypatch):
    data = bytearray(wav_bytes())
    struct.pack_into("<H", data, 32, 1)
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(bytes(data), budget=ReadBudget())


def test_wav_duration_limit_refuses_without_decode(monkeypatch):
    monkeypatch.setattr(audio, "MAX_AUDIO_SECONDS", 1)
    monkeypatch.setattr(audio, "_program", lambda _: pytest.fail("decoder must not start"))
    with pytest.raises(NotAnExport):
        audio.normalize_audio(wav_bytes(frames=16_001), budget=ReadBudget())


@needs_decoder
def test_truncated_ogg_and_missing_final_page_are_refused(tmp_path):
    source = encoded(tmp_path, "ogg", frames=32_000)
    last_page = source.rfind(b"OggS")
    for truncated in (source[:-1], source[:last_page]):
        with pytest.raises(NotAnExport):
            audio.normalize_audio(truncated, budget=ReadBudget())


@needs_decoder
def test_truncated_mp4_and_incomplete_box_are_refused(tmp_path):
    source = encoded(tmp_path, "m4a")
    for truncated in (source[:-1], source[:12], source + b"\x00"):
        with pytest.raises(NotAnExport):
            audio.normalize_audio(truncated, budget=ReadBudget())


@needs_decoder
def test_chained_ogg_streams_are_refused(tmp_path):
    source = encoded(tmp_path, "ogg")
    with pytest.raises(NotAnExport):
        audio.normalize_audio(source + source, budget=ReadBudget())


@needs_decoder
def test_decoded_output_cannot_exceed_remaining_budget(tmp_path):
    source = encoded(tmp_path, "ogg", frames=32_000)
    with pytest.raises(NotAnExport):
        audio.normalize_audio(source, budget=ReadBudget(max_bytes=len(source) + 2000))


def test_missing_decoder_is_unavailable_not_invalid_upload(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda _: None)
    with pytest.raises(audio.AudioDecoderUnavailable, match="not configured"):
        audio.normalize_audio(wav_bytes(), budget=ReadBudget())


def test_process_limits_are_applied_before_exec(tmp_path):
    code = (
        "import json,resource; print(json.dumps({"
        "'cpu':resource.getrlimit(resource.RLIMIT_CPU),"
        "'file':resource.getrlimit(resource.RLIMIT_FSIZE),"
        "'fds':resource.getrlimit(resource.RLIMIT_NOFILE),"
        "'core':resource.getrlimit(resource.RLIMIT_CORE)}))"
    )
    observed = json.loads(
        audio._run_program(sys.executable, ["-c", code], directory=tmp_path, max_output=4096)
    )
    assert observed == {"cpu": [20, 20], "file": [4097, 4097], "fds": [32, 32], "core": [0, 0]}


def test_process_output_overflow_is_refused_and_diagnostics_are_not_exposed(tmp_path):
    code = "import os; os.write(2,b'private synthetic text'); os.write(1,b'x'*10000)"
    with pytest.raises(NotAnExport, match="conversion limits") as caught:
        audio._run_program(sys.executable, ["-c", code], directory=tmp_path, max_output=100)
    assert "private" not in str(caught.value)


def test_stalled_decoder_is_killed_and_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "PROCESS_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(NotAnExport, match="conversion limits"):
        audio._run_program(
            sys.executable,
            ["-c", "import time; time.sleep(30)"],
            directory=tmp_path,
            max_output=100,
        )


def test_child_has_no_parent_secret_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PRIVATE_VALUE", "private synthetic text")
    result = audio._run_program(
        sys.executable,
        ["-c", "import os; print(os.getenv('SYNTHETIC_PRIVATE_VALUE'))"],
        directory=tmp_path,
        max_output=100,
    )
    assert result == b"None\n"


def test_input_options_cannot_enable_remote_or_external_tracks(tmp_path):
    options = audio._input_options("mov", tmp_path / "source.bin")
    assert options[options.index("-protocol_whitelist") + 1] == "file"
    assert options[options.index("-format_whitelist") + 1] == "mov"
    assert options[options.index("-enable_drefs") + 1] == "0"
    assert options[options.index("-use_absolute_path") + 1] == "0"


@needs_decoder
def test_no_filename_metadata_or_source_time_is_in_result(tmp_path):
    source = wav_bytes()
    misleading = tmp_path / "2020-01-01_private-speaker.wav"
    misleading.write_bytes(source)
    result = audio.normalize_audio(misleading.read_bytes(), budget=ReadBudget())
    assert not hasattr(result, "source_at")
    assert not hasattr(result, "sender")
    assert not hasattr(result, "filename")
    assert "private-speaker" not in repr(result)
    assert result.original_sha256 == hashlib.sha256(source).hexdigest()


@needs_decoder
def test_private_scratch_is_removed_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.tempfile, "tempdir", str(tmp_path))
    audio.normalize_audio(wav_bytes(), budget=ReadBudget())
    assert list(tmp_path.iterdir()) == []
