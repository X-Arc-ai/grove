"""The episode the runner cuts for a request holds the time its row is dated at (S51).

A remember carries no time per message, so the Learner dates its row at the request's `said_at`
(the runner's batch form does so), and the writer refuses a row dated outside its episode. The
window therefore runs from `said_at` to the source's read. Invented names only (R3).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from grove.ids import new_id
from grove.runner.brain import pass_input
from grove.runner.passes import _ensure_episode, caller_for, run_record, start_pass
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer import Caller, apply_batch, apply_request, enqueue_request, record_connector
from grove.writer.sources import write_screened_source
from tests.runner.helpers import FAST, HOST, new_auth

LEARNER = Caller(kind="update", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:l")
WORDS = "Remember this: Lina Okafor runs the Elm Street seed swap."


def queue_remember(
    driver, world_id: str, said_at: datetime | None, *, messages: list[Message] | None = None
) -> tuple[str, str]:
    connector = record_connector(driver, world_id, "ai_host")
    screened = Screener(ScreeningConfig()).screen_source(
        "",
        messages if messages is not None else [Message(key="m1", sender=None, text=WORDS, at=None)],
    )
    key = hashlib.sha256(screened.content().encode("utf-8")).hexdigest()
    written: dict[str, str] = {}

    def write_source(tx) -> str:
        source_id = new_id(tx.world_id)
        write_screened_source(
            tx, screened, source_id=source_id, connector_id=connector, parser_version="test-1"
        )
        written["id"] = source_id
        return source_id

    request_id = f"store-{secrets.token_hex(6)}"
    enqueue_request(
        driver,
        world_id,
        HOST,
        request_id,
        write_source=write_source,
        content_key=key,
        said_at=said_at,
    )
    return request_id, written["id"]


def test_a_remember_dated_at_its_said_at_lies_inside_its_episode(driver, seeded, frozen):
    said_at = frozen.now() - timedelta(minutes=3)
    _, source_id = queue_remember(driver, seeded, said_at)
    episode_id = _ensure_episode(driver, seeded, source_id, "run-e1", frozen.now())
    [episode] = WorldQuery(driver, seeded).read(
        "MATCH (e:Episode {id: $id}) RETURN e", id=episode_id
    )
    assert episode["e"]["start_time"] == said_at.astimezone(UTC)
    assert episode["e"]["end_time"] == frozen.now().astimezone(UTC)
    # The Learner dates its row at said_at, as the batch form says, and the writer takes it.
    batch = {
        "evidence": [
            {
                "ev_id": "e1",
                "episode_id": episode_id,
                "source_id": source_id,
                "locator": "whole",
                "message_key": "m1",
                "source_time": said_at.isoformat(),
                "statement_class": "report_state",
                "derivation": "observed",
                "speaker_kind": "person",
                "modality": "text",
            }
        ],
        "ops": [
            {
                "op": "create_item",
                "op_id": "o1",
                "kind": "person",
                "title": "Lina Okafor",
                "became_true": "unknown",
                "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
                "reason": "the remember names her",
            }
        ],
    }
    receipt = apply_batch(driver, seeded, LEARNER, batch, run_id="run-e1")
    assert receipt.state == "saved"
    assert datetime(2000, 1, 1, tzinfo=UTC) < said_at


@pytest.mark.parametrize(
    ("timestamps", "cited_index", "expected_start", "expected_end"),
    [
        (
            ["2026-11-02T03:00:00Z", "2026-11-02T09:00:00+05:00", "2026-11-02T05:00:00Z"],
            2,
            "2026-11-02T03:00:00Z",
            "2026-11-02T05:00:00Z",
        ),
        (
            ["2026-11-02T02:00:00Z", "2026-11-02T01:00:00-02:00", "2026-11-02T05:00:00Z"],
            0,
            "2026-11-02T02:00:00Z",
            "2026-11-02T05:00:00Z",
        ),
    ],
    ids=["latest-message", "earliest-message"],
)
def test_mixed_offsets_keep_every_message_inside_the_episode(
    driver, seeded, frozen, timestamps, cited_index, expected_start, expected_end
):
    messages = [
        Message(key=f"m{index + 1}", sender=None, text=WORDS, at=datetime.fromisoformat(timestamp))
        for index, timestamp in enumerate(timestamps)
    ]
    queue_remember(driver, seeded, None, messages=messages)
    grant = start_pass(driver, new_auth()[0], seeded, settings=FAST)
    form = pass_input(driver, seeded, run_record(driver, seeded, grant.run_id))["batch_form"]
    batch = form["batch"]
    batch["ops"] = [batch["ops"][0] | {"title": "Lina Okafor", "op_id": "o1"}]
    batch["evidence"][0].update(
        message_key=messages[cited_index].key,
        source_time=timestamps[cited_index],
    )
    receipt = apply_request(
        driver, seeded, caller_for("learner"), form["request_id"], batch, run_id=grant.run_id
    )
    assert receipt.api_state == "saved", receipt
    [episode] = WorldQuery(driver, seeded).read(
        "MATCH (e:Episode {id: $id}) RETURN e", id=batch["evidence"][0]["episode_id"]
    )
    assert episode["e"]["start_time"] == datetime.fromisoformat(expected_start)
    assert episode["e"]["end_time"] == datetime.fromisoformat(expected_end)
