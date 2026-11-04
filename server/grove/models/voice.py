"""Voice API request validation and timestamp provenance, independent of transport.

Offsets are model detection boundaries, not exact acoustic alignment. Speaker labels belong
to one provider request; chunk_index and model_call_id keep separate requests distinct.
"""

from dataclasses import dataclass, field

from grove.models.errors import InvalidOutput, ModelRefused

MODES = ("PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION")
# Application bounds, not claims about the provider's documented limits.
MAX_HINTS = 100
MAX_HINT_CHARS = 200
MAX_TURNS = 10_000
MAX_TRANSCRIPT_CHARS = 100_000


@dataclass(frozen=True)
class VoiceTurn:
    turn_id: int
    start_ms: int
    end_ms: int
    chunk_index: int
    model_call_id: str
    text: str = field(repr=False)
    speaker: str | None = field(default=None, repr=False)


def request_options(
    mode: str,
    keywords: list[str] | tuple[str, ...] | None,
    language_bias: list[str] | tuple[str, ...] | None,
) -> dict:
    if type(mode) is not str or mode not in MODES:
        raise ModelRefused("the voice mode is not supported")
    result = {"mode": mode}
    for name, values in (("keywords", keywords), ("languageBias", language_bias)):
        if values is None:
            continue
        if type(values) not in (list, tuple) or len(values) > MAX_HINTS:
            raise ModelRefused("voice hints must be bounded lists of strings")
        if any(not _text(value, MAX_HINT_CHARS) or not value.strip() for value in values):
            raise ModelRefused("voice hints must be bounded nonempty strings")
        result[name] = list(values)
    return result


def _text(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and len(value) <= limit
        and not any(0xD800 <= ord(char) <= 0xDFFF or char == "\x00" for char in value)
    )


def parse_turns(doc: dict, *, mode: str, piece_ms: int) -> tuple[dict, ...]:
    """Validate every turn before the caller records a successful model call.

    An empty partial turn stays in the result. Offsets outside the actual audio are refused;
    the provider's billing duration cannot redefine a clip's provenance.
    """
    if not _text(doc.get("transcript"), MAX_TRANSCRIPT_CHARS):
        raise InvalidOutput("the transcript is missing or invalid")
    turns = doc.get("turns", [] if mode == "PUSH_TO_TALK" else None)
    if type(turns) is not list or len(turns) > MAX_TURNS:
        raise InvalidOutput("voice turns are missing or invalid")
    parsed = []
    previous_id = -1
    for turn in turns:
        if type(turn) is not dict:
            raise InvalidOutput("a voice turn is invalid")
        turn_id, start, end = (turn.get(name) for name in ("turnId", "startMs", "endMs"))
        if (
            type(turn_id) is not int
            or not previous_id < turn_id < 2**31
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= piece_ms
        ):
            raise InvalidOutput("voice turn identifiers or offsets are invalid")
        speaker = turn.get("speaker")
        if not _text(turn.get("transcript"), MAX_TRANSCRIPT_CHARS):
            raise InvalidOutput("a voice turn transcript is invalid")
        if speaker is not None and not _text(speaker, MAX_HINT_CHARS):
            raise InvalidOutput("a voice speaker label is invalid")
        parsed.append(
            {
                "turn_id": turn_id,
                "start_ms": start,
                "end_ms": end,
                "text": turn["transcript"],
                "speaker": speaker,
            }
        )
        previous_id = turn_id
    # A nonempty transcript with no detected turns cannot satisfy a requested timed mode.
    if mode != "PUSH_TO_TALK" and doc["transcript"].strip() and not parsed:
        raise InvalidOutput("the timed transcript has no voice turns")
    return tuple(parsed)
