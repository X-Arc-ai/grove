"""WAV audio for Voice Transcribe, which takes files of up to 10 minutes: longer audio is split."""

import io
import struct
import wave
from dataclasses import dataclass

from grove.models.errors import AudioTooLong, ModelRefused

API_LIMIT_SECONDS = 600
# A margin under the limit, so a header's rounding never puts a piece over it.
PIECE_SECONDS = 590
# About six hours. The header states the rate, so a small crafted file could ask for any number of
# calls, and transcription runs on at the ceiling.
MAX_PIECES = 36


@dataclass(frozen=True)
class Piece:
    data: bytes
    seconds: float


def wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            return reader.getnframes() / reader.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError, struct.error) as exc:
        raise ModelRefused("the audio is not a readable WAV file") from exc


def split_wav(data: bytes, piece_seconds: int = PIECE_SECONDS) -> list[Piece]:
    """The audio as pieces of at most `piece_seconds`, each a complete WAV file."""
    if not 0 < piece_seconds <= API_LIMIT_SECONDS:
        raise AudioTooLong(f"a piece must be between 1 and {API_LIMIT_SECONDS} seconds")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            params = reader.getparams()
            rate = reader.getframerate()
            if rate <= 0 or reader.getnframes() == 0:
                raise ModelRefused("the audio holds no frames")
            frames_per_piece = rate * piece_seconds
            pieces = []
            while True:
                frames = reader.readframes(frames_per_piece)
                if not frames:
                    break
                if len(pieces) == MAX_PIECES:
                    raise AudioTooLong(f"a file may take at most {MAX_PIECES} pieces")
                out = io.BytesIO()
                with wave.open(out, "wb") as writer:
                    writer.setparams(params)
                    writer.writeframes(frames)
                count = len(frames) // (params.sampwidth * params.nchannels)
                pieces.append(Piece(data=out.getvalue(), seconds=count / rate))
    except (wave.Error, EOFError, ZeroDivisionError, struct.error) as exc:
        raise ModelRefused("the audio is not a readable WAV file") from exc
    return pieces
