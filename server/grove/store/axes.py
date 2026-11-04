"""The result of an as-of read on either time axis (D3, "Two time axes, kept apart").

The world axis answers "what was true at T", with everything Grove knows now. The record axis
answers "what Grove believed at T": the head as it stood at record time T, read at world time T.
Both reads return this one shape, so a caller never mixes the axes by accident.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from grove.store.current import Standing

AXES: tuple[str, ...] = ("world", "record")


@dataclass(frozen=True)
class AsOfRow:
    """One item or link as the read found it, and its standing at the time read."""

    row: dict[str, Any] = field(hash=False)
    standing: Standing


@dataclass(frozen=True)
class AsOf:
    """`items` and `links` are keyed by id. A row that did not stand at `at` is left out, except an
    item that was overtaken: it is kept, with its successors on its standing."""

    axis: str
    at: datetime
    items: dict[str, AsOfRow] = field(hash=False)
    links: dict[str, AsOfRow] = field(hash=False)

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"axis must be one of {list(AXES)}")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("an as-of time needs its offset")
