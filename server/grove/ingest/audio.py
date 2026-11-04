"""Bounded, byte-only audio normalization before the Voice adapter.

This module makes no model or store call. The caller retains the original artifact and commits
its provenance before transcription. Decoded frame duration is not a source timestamp.
"""

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from grove.ingest.connector import NotAnExport, ReadBudget

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_AUDIO_SECONDS = 3600
MAX_INPUT_RATE = 96_000
MAX_INPUT_CHANNELS = 2
MAX_PROBE_BYTES = 4096
MAX_CONTAINER_PARTS = 100_000
PROCESS_TIMEOUT_SECONDS = 45
PROCESS_CPU_SECONDS = 20
PROCESS_MEMORY_BYTES = 768 * 1024 * 1024
MAX_ALLOCATION_BYTES = 32 * 1024 * 1024
_INVALID = "the audio cannot be read within the conversion limits"

# A separate Python process sets the limits before exec. preexec_fn is unsafe in a threaded
# API process. No source bytes or values are interpolated into this program or its arguments.
_LIMITED_EXEC = """
import os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[1]), int(sys.argv[1])))
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[2]), int(sys.argv[2])))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
if sys.platform.startswith('linux'):
    resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[3]), int(sys.argv[3])))
os.execv(sys.argv[4], sys.argv[4:])
"""


class AudioDecoderUnavailable(RuntimeError):
    """The trusted runtime lacks the required decoder; this is not an invalid upload."""


@dataclass(frozen=True)
class NormalizedAudio:
    original_sha256: str
    normalized_sha256: str
    media_type: str
    sample_rate: int
    frames: int
    original: bytes = field(repr=False)
    wav: bytes = field(repr=False)

    @property
    def seconds(self) -> float:
        """Duration of decoded samples, with no claim about the source's wall time."""
        return self.frames / self.sample_rate


def _wav(data: bytes) -> None:
    if len(data) < 12 or int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise NotAnExport(_INVALID)
    position, parts = 12, 0
    fmt = None
    payload_size = None
    while position < len(data):
        parts += 1
        if parts > MAX_CONTAINER_PARTS or position + 8 > len(data):
            raise NotAnExport(_INVALID)
        kind = data[position : position + 4]
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        start, end = position + 8, position + 8 + size
        if end + (size % 2) > len(data):
            raise NotAnExport(_INVALID)
        if kind == b"fmt ":
            if fmt is not None or size < 16:
                raise NotAnExport(_INVALID)
            fmt = struct.unpack_from("<HHIIHH", data, start)
        elif kind == b"data":
            if payload_size is not None:
                raise NotAnExport(_INVALID)
            payload_size = size
        position = end + (size % 2)
    if fmt is None or payload_size is None:
        raise NotAnExport(_INVALID)
    encoding, channels, rate, byte_rate, align, bits = fmt
    if (
        encoding not in (1, 3)
        or not 1 <= channels <= MAX_INPUT_CHANNELS
        or not 8000 <= rate <= MAX_INPUT_RATE
        or bits not in (8, 16, 24, 32)
        or encoding == 3
        and bits != 32
        or align != channels * (bits // 8)
        or byte_rate != rate * align
        or payload_size == 0
        or payload_size % align
        or payload_size // align > rate * MAX_AUDIO_SECONDS
    ):
        raise NotAnExport(_INVALID)


def _ogg(data: bytes) -> None:
    position, parts, serial = 0, 0, None
    ended = False
    while position < len(data):
        if ended or parts >= MAX_CONTAINER_PARTS or position + 27 > len(data):
            raise NotAnExport(_INVALID)
        header = data[position : position + 27]
        segments = header[26]
        payload = position + 27 + segments
        if header[:5] != b"OggS\x00" or payload > len(data):
            raise NotAnExport(_INVALID)
        end = payload + sum(data[position + 27 : payload])
        if end > len(data) or header[5] & ~7:
            raise NotAnExport(_INVALID)
        page_serial = header[14:18]
        sequence = int.from_bytes(header[18:22], "little")
        if parts == 0:
            serial = page_serial
            if header[5] != 2 or data[payload : payload + 8] != b"OpusHead":
                raise NotAnExport(_INVALID)
            if end - payload < 19 or not 1 <= data[payload + 9] <= MAX_INPUT_CHANNELS:
                raise NotAnExport(_INVALID)
        elif header[5] & 2:
            raise NotAnExport(_INVALID)
        if page_serial != serial or sequence != parts:
            raise NotAnExport(_INVALID)
        ended = bool(header[5] & 4)
        parts += 1
        position = end
    # An export cut between complete pages is still incomplete if its final stream never ends.
    if not ended or parts < 2:
        raise NotAnExport(_INVALID)


def _mp4(data: bytes) -> None:
    position, parts = 0, 0
    seen = set()
    while position < len(data):
        parts += 1
        if parts > MAX_CONTAINER_PARTS or position + 8 > len(data):
            raise NotAnExport(_INVALID)
        size = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        header_size = 8
        if size == 1:
            if position + 16 > len(data):
                raise NotAnExport(_INVALID)
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - position
        if size < header_size or position + size > len(data):
            raise NotAnExport(_INVALID)
        seen.add(kind)
        position += size
    if not {b"ftyp", b"moov", b"mdat"} <= seen:
        raise NotAnExport(_INVALID)


def _container(data: bytes) -> tuple[str, str]:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        _wav(data)
        return "wav", "audio/wav"
    if data[:4] == b"OggS":
        _ogg(data)
        return "ogg", "audio/ogg"
    if data[4:8] == b"ftyp":
        _mp4(data)
        return "mov", "audio/mp4"
    raise NotAnExport("the audio container is not supported")


def _program(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise AudioDecoderUnavailable("the audio decoder is not configured")
    return str(Path(found).resolve())


def _run_program(program: str, arguments: list[str], *, directory: Path, max_output: int) -> bytes:
    """Bound stdout on disk before a read into Python; discard every decoder diagnostic."""
    with tempfile.TemporaryFile(dir=directory) as output:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    _LIMITED_EXEC,
                    str(PROCESS_CPU_SECONDS),
                    str(max_output + 1),
                    str(PROCESS_MEMORY_BYTES),
                    program,
                    *arguments,
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                cwd=directory,
                env={"LC_ALL": "C", "PATH": os.defpath},
                timeout=PROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise NotAnExport(_INVALID) from None
        if result.returncode != 0 or output.tell() > max_output:
            raise NotAnExport(_INVALID)
        output.seek(0)
        return output.read(max_output + 1)


def _input_options(container: str, source: Path) -> list[str]:
    options = [
        "-v",
        "error",
        "-max_alloc",
        str(MAX_ALLOCATION_BYTES),
        "-cpucount",
        "1",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        container,
        "-f",
        container,
    ]
    if container == "mov":
        options += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    return [*options, "-i", str(source)]


def _probe(program: str, options: list[str], directory: Path) -> None:
    data = _run_program(
        program,
        [
            *options,
            "-show_entries",
            "stream=codec_type,channels,sample_rate:format=duration",
            "-of",
            "json",
        ],
        directory=directory,
        max_output=MAX_PROBE_BYTES,
    )
    try:
        result = json.loads(data)
        streams = result["streams"]
        if not isinstance(streams, list) or len(streams) != 1:
            raise ValueError
        stream = streams[0]
        if (
            stream["codec_type"] != "audio"
            or type(stream["channels"]) is not int
            or not 1 <= stream["channels"] <= MAX_INPUT_CHANNELS
            or type(stream["sample_rate"]) is not str
            or not stream["sample_rate"].isdigit()
            or not 8000 <= int(stream["sample_rate"]) <= MAX_INPUT_RATE
        ):
            raise ValueError
        duration = result.get("format", {}).get("duration")
        if duration is not None:
            seconds = Decimal(duration)
            if not seconds.is_finite() or not 0 < seconds <= MAX_AUDIO_SECONDS:
                raise ValueError
    except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError):
        raise NotAnExport(_INVALID) from None


def normalize_audio(
    data: bytes, *, budget: ReadBudget, sample_rate: int = 16_000
) -> NormalizedAudio:
    """Normalize one complete WAV, Ogg Opus or M4A artifact to mono PCM16.

    The input is bytes only: neither a caller filename nor its filesystem metadata is accepted.
    The source may exceed ten minutes; the model layer later splits this complete WAV by frames.
    Runtime limits are deliberately tighter than the general upload limit. Nothing is truncated.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_INPUT_BYTES:
        raise NotAnExport("the audio exceeds the input limit or is empty")
    if type(sample_rate) is not int or sample_rate not in (16_000, 24_000):
        raise ValueError("the target audio rate must be 16000 or 24000")
    budget.take_bytes(len(data))
    container, media_type = _container(data)
    probe, decoder = _program("ffprobe"), _program("ffmpeg")
    # The child cannot produce a buffer larger than the remaining shared upload budget.
    # Include the deterministic WAV header before any decoder process starts.
    if budget.bytes_left < 46:
        budget.take_bytes(46)
    max_pcm = min(MAX_AUDIO_SECONDS * sample_rate * 2, budget.bytes_left - 44)
    with tempfile.TemporaryDirectory(prefix="grove-audio-") as scratch:
        directory = Path(scratch)
        source = directory / "source.bin"
        with source.open("xb") as stream:
            os.chmod(source, 0o600)
            stream.write(data)
        options = _input_options(container, source)
        _probe(probe, options, directory)
        pcm = _run_program(
            decoder,
            [
                "-nostdin",
                "-xerror",
                "-err_detect",
                "crccheck+explode",
                *options,
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-threads",
                "1",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            directory=directory,
            max_output=max_pcm,
        )
    if not pcm or len(pcm) % 2 or len(pcm) > max_pcm:
        raise NotAnExport(_INVALID)
    budget.take_bytes(len(pcm) + 44)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    normalized = output.getvalue()
    return NormalizedAudio(
        hashlib.sha256(data).hexdigest(),
        hashlib.sha256(normalized).hexdigest(),
        media_type,
        sample_rate,
        len(pcm) // 2,
        data,
        normalized,
    )
