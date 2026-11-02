"""The one writer of :MapPosition: where the map drew an item first.

Insert-only. A position, once placed, is never moved: the map keeps every placed item where the
person first saw it, after a reload and after new items arrive. A second placement of the same
item, in a later call or in a call that races the first, is counted as kept and changes nothing.

The (world_id, item_id) uniqueness constraint backs the race: MERGE on that key makes a second
transaction wait for the first and then match its row. If the store still refuses a create with a
constraint error, the call runs again, and the row that won counts as kept.
"""

import math
from collections.abc import Mapping, Sequence

from neo4j import Driver
from neo4j.exceptions import ConstraintError

from grove import clock
from grove.ids import new_id
from grove.writer.tx import WorldTx, WorldWriter

MAX_POSITIONS = 5000
MAX_COORDINATE = 1e7
MAX_ID_LENGTH = 128
_ATTEMPTS = 3
_FIELDS = frozenset({"id", "x", "y"})

# The MATCH keeps only items of this world (the scoper stamps every pattern with the world). The
# MERGE on the key of the uniqueness constraint creates the row or finds the one that is there; a
# found row is never changed, because only ON CREATE sets a value.
_PLACE = (
    "UNWIND $rows AS row "
    "MATCH (i:Item {id: row.item_id}) "
    "MERGE (p:MapPosition {item_id: row.item_id}) "
    "ON CREATE SET p.id = row.position_id, p.x = row.x, p.y = row.y, p.placed_at = $at "
    "RETURN row.item_id AS item_id, p.id = row.position_id AS placed"
)


class InvalidPositions(ValueError):
    """Input that is not 1 to 5000 positions. The message names the field, never a value."""

    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


def _coordinate(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidPositions(f"{field} must be a number", field)
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_COORDINATE:
        raise InvalidPositions(f"{field} must be finite and at most 1e7 either way", field)
    return number


def check_positions(positions: object) -> list[tuple[str, float, float]]:
    """(item id, x, y) for each entry, in the given order; raise InvalidPositions on bad input."""
    if isinstance(positions, str | bytes) or not isinstance(positions, Sequence):
        raise InvalidPositions("positions must be a list", "positions")
    if not 1 <= len(positions) <= MAX_POSITIONS:
        raise InvalidPositions(f"positions must hold 1 to {MAX_POSITIONS} entries", "positions")
    checked = []
    for index, entry in enumerate(positions):
        where = f"positions[{index}]"
        if not isinstance(entry, Mapping):
            raise InvalidPositions(f"{where} must be an object", where)
        if set(entry) != _FIELDS:
            raise InvalidPositions(f"{where} must have exactly id, x and y", where)
        item_id = entry["id"]
        if not isinstance(item_id, str) or not 1 <= len(item_id) <= MAX_ID_LENGTH:
            raise InvalidPositions(
                f"{where}.id must be a string of 1 to {MAX_ID_LENGTH} characters", f"{where}.id"
            )
        x = _coordinate(entry["x"], f"{where}.x")
        y = _coordinate(entry["y"], f"{where}.y")
        checked.append((item_id, x, y))
    return checked


def _place(tx: WorldTx, world_id: str, checked: list[tuple[str, float, float]]) -> list[dict]:
    rows = [
        {"item_id": item_id, "position_id": new_id(world_id), "x": x, "y": y}
        for item_id, x, y in checked
    ]
    return tx.run(_PLACE, rows=rows, at=clock.now(world_id))


def place_items(driver: Driver, world_id: str, positions: object) -> dict[str, int]:
    """Place each item of this world that has no position yet.

    Returns {"placed", "kept", "unknown"}, which add up to the number of entries: placed is a new
    row, kept is an item that already had one (also a repeat inside this call), and unknown is an
    id that is not an item of this world, which writes nothing.
    """
    checked = check_positions(positions)
    writer = WorldWriter(driver, world_id)
    for attempt in range(_ATTEMPTS):
        try:
            rows = writer.transaction(lambda tx: _place(tx, world_id, checked))
            break
        except ConstraintError:
            # A create that lost a race on the key. The next run finds the winner's row.
            if attempt == _ATTEMPTS - 1:
                raise
    placed = sum(1 for row in rows if row["placed"])
    found = len(rows)
    return {"placed": placed, "kept": found - placed, "unknown": len(checked) - found}
