"""The one writer of :Source and :ScreeningReceipt (D2, D14).

A source row takes only a ScreenedSource, which only the Screener makes, so unscreened text cannot
reach the sources table by type. Its receipt holds counts per class, never a value.

Screening is code only and never waits on a model, so it finishes before the writer opens a
transaction. The caller mints the source id with new_id(world_id), screens, and then commits the
source, its receipt and the request row that names the source in one write transaction (ruling SC1
of docs/store-map.md). A request never names a source that is not there, and raw text is never
stored first.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from grove import clock
from grove.ids import new_id
from grove.screening import ScreenedSource
from grove.store.enums import ENUMS
from grove.writer.tx import WorldTx, WorldWriter

FIRST_VERSION = 1
KEPT = "kept"
SCREENED_WHERE = "server"

_CLASSES = frozenset(ENUMS["screening_class"])
# The text form of grove.ids.new_id: lowercase UUID, version 7, the RFC variant (S4).
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# A code is machine text. The form keeps free text, and so a value, out of parser_version.
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")

_ADD_SOURCE = "CREATE (s:Source) SET s += $row"
_ADD_RECEIPT = "CREATE (r:ScreeningReceipt) SET r += $row"


def _world_of(tx: WorldTx) -> str:
    # WorldTx has no public accessor, and the world id is bound by the layer: never a parameter.
    return tx._world_id


def _check_id(name: str, value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} is not an id from new_id")
    return value


def _checked_counts(counts: object) -> dict[str, int]:
    """The receipt's counts. No message names a key or a value: a bad key may be a value."""
    if not isinstance(counts, Mapping):
        raise TypeError("the counts of a screened source must be a mapping")
    checked: dict[str, int] = {}
    for key, value in counts.items():
        if type(key) is not str or key not in _CLASSES:
            raise ValueError("the counts hold a key outside the screening_class codes")
        # type() and not isinstance(): True is an int to isinstance.
        if type(value) is not int:
            raise TypeError("a count of a screened source is not an int")
        # The screener keeps only the classes that occurred.
        if value < 1:
            raise ValueError("a count of a screened source is below 1")
        checked[key] = value
    return checked


def write_screened_source(
    tx: WorldTx,
    screened: ScreenedSource,
    *,
    source_id: str,
    connector_id: str,
    parser_version: str,
) -> dict[str, Any]:
    """Write one :Source and its :ScreeningReceipt in the caller's transaction.

    It returns the source id, the counts per class and the number of kept messages: no text.
    """
    if type(tx) is not WorldTx:
        raise TypeError("write_screened_source takes a WorldTx")
    # type() and not isinstance(): a subclass could carry text that never went through screening.
    if type(screened) is not ScreenedSource:
        raise TypeError("write_screened_source takes a ScreenedSource from the Screener")
    _check_id("source_id", source_id)
    _check_id("connector_id", connector_id)
    if type(parser_version) is not str or _CODE.fullmatch(parser_version) is None:
        raise ValueError("parser_version is not a code")
    counts = _checked_counts(screened.counts)

    at = clock.now(_world_of(tx))
    tx.run(
        _ADD_SOURCE,
        row={
            "id": source_id,
            "connector_id": connector_id,
            "external_locator": screened.locator,
            "received_at": at,
            "content": screened.content(),
            "parser_version": parser_version,
            "untrusted": True,
            "forget_state": KEPT,
            "version": FIRST_VERSION,
            "touched_at": at,
        },
    )
    tx.run(
        _ADD_RECEIPT,
        row={
            "source_id": source_id,
            "counts_per_class": json.dumps(counts, sort_keys=True),
            "screened_at": at,
            "screened_where": SCREENED_WHERE,
        },
    )
    return {"source_id": source_id, "counts": counts, "kept_messages": len(screened.messages)}


def add_screened_source(
    writer: WorldWriter,
    screened: ScreenedSource,
    *,
    connector_id: str,
    parser_version: str,
    source_id: str | None = None,
) -> dict[str, Any]:
    """write_screened_source in a transaction of its own. With no source_id it mints one."""

    def work(tx: WorldTx) -> dict[str, Any]:
        # Minted inside the work: a retried transaction mints again, and only the id that
        # committed is returned.
        chosen = new_id(_world_of(tx)) if source_id is None else source_id
        return write_screened_source(
            tx,
            screened,
            source_id=chosen,
            connector_id=connector_id,
            parser_version=parser_version,
        )

    return writer.transaction(work)
