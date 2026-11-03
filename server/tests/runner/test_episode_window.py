"""The episode the runner cuts for a request holds the time its row is dated at (S46).

A remember carries no time per message, so the Learner dates its row at the request's `said_at`
(the runner's batch form does so), and the writer refuses a row dated outside its episode. The
window therefore runs from `said_at` to the source's read. Invented names only (R3).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from grove.ids import new_id
from grove.runner.passes import _ensure_episode
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer import Caller, apply_batch, enqueue_request, record_connector
from grove.writer.sources import write_screened_source
from tests.runner.helpers import HOST

LEARNER = Caller(kind="update", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:l")
WORDS = "Remember this: Lina Okafor runs the Elm Street seed swap."


def queue_remember(driver, world_id: str, said_at: datetime) -> tuple[str, str]:
    connector = record_connector(driver, world_id, "ai_host")
    screened = Screener(ScreeningConfig()).screen_source(
        "", [Message(key="m1", sender=None, text=WORDS, at=None)]
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
