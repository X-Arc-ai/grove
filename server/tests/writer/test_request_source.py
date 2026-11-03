"""A request and its screened source commit together (ruling SC1, spec lines 750, 2462, 2505).

The caller screens first, then one write transaction holds the :Source, its :ScreeningReceipt and
the :Request that names it. A request never names a source that is not there, and a second
enqueue under the same key stores the words no second time.
"""

import hashlib

import pytest

from grove.ids import new_id
from grove.screening import Message, Screener, ScreeningConfig
from grove.writer import (
    enqueue_request,
    record_connector,
    request_state,
    write_request_row,
    write_screened_source,
)
from grove.writer.tx import WorldTx, WorldWriter
from tests.writer.helpers import HOST, read

CARD = "4111 1111 1111 1111"
WORDS = f"Rowan paid the boathouse deposit with card {CARD}"


class Stop(Exception):
    """Raised after the writes of a transaction, so it rolls back."""


def screened(text: str = WORDS):
    return Screener(ScreeningConfig()).screen_source("remember.txt", [Message("m1", "Rowan", text)])


def content_key(source) -> str:
    return hashlib.sha256(source.content().encode("utf-8")).hexdigest()


def plug(source, connector_id: str, source_ids: list[str]):
    """The seam hosts-b uses: write_screened_source in the enqueue's own transaction."""

    def write_source(tx: WorldTx) -> str:
        source_id = new_id(tx.world_id)
        source_ids.append(source_id)
        write_screened_source(
            tx, source, source_id=source_id, connector_id=connector_id, parser_version="host-1"
        )
        return source_id

    return write_source


def counts(driver, world_id: str) -> dict[str, int]:
    return {
        label: read(driver, world_id, f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]
        for label in ("Source", "ScreeningReceipt", "Request")
    }


@pytest.fixture
def connector(driver, seeded):
    return record_connector(driver, seeded["world_id"], "ai_host")


def test_the_request_names_the_screened_source_it_committed_with(driver, seeded, connector):
    world_id = seeded["world_id"]
    source = screened()
    written: list[str] = []

    state = enqueue_request(
        driver,
        world_id,
        HOST,
        "host-k1",
        write_source=plug(source, connector, written),
        content_key=content_key(source),
    )

    assert state.status == "queued"
    assert state.api_state == "saving"
    assert state.source_id == written[0]
    rows = read(
        driver,
        world_id,
        "MATCH (s:Source {id: $id}) MATCH (r:ScreeningReceipt {source_id: $id}) "
        "RETURN s.content AS content, s.untrusted AS untrusted, r.counts_per_class AS counts",
        id=written[0],
    )
    assert len(rows) == 1
    assert CARD not in rows[0]["content"]
    assert rows[0]["untrusted"] is True
    assert "card" in rows[0]["counts"]
    assert counts(driver, world_id) == {"Source": 1, "ScreeningReceipt": 1, "Request": 1}


def test_a_rollback_after_the_writes_leaves_no_source_no_receipt_and_no_request(
    driver, seeded, connector
):
    world_id = seeded["world_id"]
    source = screened()
    before = counts(driver, world_id)

    def work(tx: WorldTx) -> None:
        row, fresh = write_request_row(
            tx,
            HOST,
            "host-k2",
            write_source=plug(source, connector, []),
            content_key=content_key(source),
        )
        assert fresh and row["source_id"]
        raise Stop

    with pytest.raises(Stop):
        WorldWriter(driver, world_id).transaction(work)

    assert counts(driver, world_id) == before
    assert request_state(driver, world_id, "host-k2") is None


def test_a_source_that_fails_to_write_leaves_no_request(driver, seeded):
    world_id = seeded["world_id"]
    source = screened()
    before = counts(driver, world_id)

    def broken(tx: WorldTx) -> str:
        # A connector id that is not an id: write_screened_source refuses it inside the enqueue.
        return write_screened_source(
            tx, source, source_id=new_id(tx.world_id), connector_id="nope", parser_version="h-1"
        )["source_id"]

    with pytest.raises(ValueError):
        enqueue_request(
            driver, world_id, HOST, "host-k3", write_source=broken, content_key=content_key(source)
        )

    assert counts(driver, world_id) == before
    assert request_state(driver, world_id, "host-k3") is None


def test_a_second_enqueue_of_the_same_words_stores_them_no_second_time(driver, seeded, connector):
    world_id = seeded["world_id"]
    source = screened()
    written: list[str] = []
    for _ in range(2):
        state = enqueue_request(
            driver,
            world_id,
            HOST,
            "host-k4",
            write_source=plug(source, connector, written),
            content_key=content_key(source),
        )

    assert len(written) == 1
    assert state.source_id == written[0]
    assert counts(driver, world_id) == {"Source": 1, "ScreeningReceipt": 1, "Request": 1}
