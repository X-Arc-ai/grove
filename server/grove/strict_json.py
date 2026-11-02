"""Strict JSON for bytes that arrive from outside.

``json.loads`` is generous in ways that hurt later. Given bytes it guesses the
encoding, so UTF-16 and UTF-32 and a BOM pass. It accepts ``NaN`` and
``Infinity``. It accepts an escape such as ``\\ud800``, a lone surrogate no
UTF-8 encoder can write, and a number such as ``1e999``, which overflows to
infinity, which JSON cannot carry. Each of these then fails only when the
answer is built, long after the input looked fine.

``loads_strict`` refuses them all at the door, and every refusal is a
``ValueError``.
"""

from __future__ import annotations

import json


def _refuse_constant(name: str) -> object:
    raise ValueError(f"JSON constant {name} is not allowed")


def loads_strict(raw: bytes) -> object:
    """Parse ``raw`` as strict UTF-8 JSON, or raise ``ValueError``.

    Refused: bytes that are not valid UTF-8, a BOM, ``NaN`` and ``Infinity``,
    a lone surrogate from an escape, a number that overflows to infinity, and
    a nesting too deep for the parser. A parsed value always survives
    ``json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")``.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("body is not valid UTF-8") from error
    try:
        # json.loads raises JSONDecodeError (a ValueError) for a text with a
        # leading BOM, so no separate check is needed for it.
        parsed = json.loads(text, parse_constant=_refuse_constant)
    except RecursionError as error:
        raise ValueError("JSON nesting is too deep") from error
    try:
        json.dumps(parsed, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except RecursionError as error:
        raise ValueError("JSON nesting is too deep") from error
    except UnicodeEncodeError as error:
        raise ValueError("JSON string holds a lone surrogate") from error
    except ValueError as error:
        raise ValueError("JSON number overflows") from error
    return parsed
