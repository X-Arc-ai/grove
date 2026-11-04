"""Bounded opaque scalars; validation establishes neither existence nor approval."""

import re

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]+", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
MAX_COUNT = 2**63 - 1


class ScalarRefused(ValueError):
    """A fixed field and reason, with no supplied value in the diagnostic."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}:{reason}")


def validate_identifier(value: object) -> str:
    if type(value) is not str:
        raise ScalarRefused("identifier", "type")
    if not 1 <= len(value) <= 128:
        raise ScalarRefused("identifier", "length")
    if _IDENTIFIER.fullmatch(value) is None:
        raise ScalarRefused("identifier", "format")
    return value


def validate_digest(value: object) -> str:
    if type(value) is not str:
        raise ScalarRefused("digest", "type")
    if len(value) != 64:
        raise ScalarRefused("digest", "length")
    if _DIGEST.fullmatch(value) is None:
        raise ScalarRefused("digest", "format")
    return value


def validate_count(value: object) -> int:
    if type(value) is not int:
        raise ScalarRefused("count", "type")
    if not 0 <= value <= MAX_COUNT:
        raise ScalarRefused("count", "range")
    return value
