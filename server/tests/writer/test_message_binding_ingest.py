"""The real legacy ChatGPT connector supplies exact writer evidence, without planted sources."""

import json
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.clock import TestClock
from grove.ingest.take_in import take_in
from grove.store.query import WorldQuery
from grove.writer.errors import BatchDeclined
from tests.ingest.conftest import make_screener, upload_of
from tests.ingest.test_chatgpt import Conversation, message
from tests.writer.helpers import (
    LEARNER,
    apply,
    assert_only_decline_log,
    count_rows,
    create_item,
    observation,
)


def test_chatgpt_message_date_substitution_rolls_back_and_exact_date_succeeds(driver, seeded):
    world_id = seeded["world_id"]
    conversation = Conversation("cedar-exact-message-time")
    conversation.add(message("cedar-person", "user", ["Cedar leads the seed exchange."], seconds=0))
    conversation.add(message("cedar-host", "assistant", ["The exchange is open."], seconds=3600))
    timer = TestClock()
    timer.set(datetime(2026, 11, 2, tzinfo=UTC))
    with clock.using(timer):
        result = take_in(
            driver,
            world_id,
            "chatgpt",
            upload_of(json.dumps([conversation.export()]).encode()),
            screener=make_screener(),
        )
        query = WorldQuery(driver, world_id)
        [source_row] = query.read("MATCH (s:Source {id: $id}) RETURN s", id=result.source_ids[0])
        [episode_row] = query.read("MATCH (e:Episode) RETURN e")
        messages = json.loads(source_row["s"]["content"])
        first, second = messages
        src = {"source_id": source_row["s"]["id"], "episode_id": episode_row["e"]["id"]}
        row = observation(
            src,
            "e1",
            1,
            at=datetime.fromisoformat(second["at"]),
            message_key=first["key"],
            locator="key:" + first["key"],
            speaker_kind=first["speaker_kind"],
        )
        assert (
            episode_row["e"]["start_time"]
            <= datetime.fromisoformat(second["at"])
            <= episode_row["e"]["end_time"]
        )
        before = count_rows(driver, world_id)
        with pytest.raises(BatchDeclined) as caught:
            apply(driver, world_id, [create_item("person", "Cedar", "e1")], [row], caller=LEARNER)
        assert any(refusal.rule == "message_source_time" for refusal in caught.value.refusals)
        assert_only_decline_log(driver, world_id, before, caught.value.refusals)
        row["source_time"] = first["at"]
        receipt = apply(
            driver,
            world_id,
            [create_item("person", "Cedar", "e1")],
            [row],
            caller=LEARNER,
        )
        assert receipt.state == "saved"
        [evidence] = query.read("MATCH (e:Evidence) RETURN e")
        assert evidence["e"]["message_key"] == first["key"]
        assert evidence["e"]["source_time"] == datetime.fromisoformat(first["at"])


def test_material_title_cannot_supply_evidence():
    import pytest

    from grove.writer.errors import Refused
    from grove.writer.message_binding import retained_messages

    with pytest.raises(Refused, match="retained source"):
        retained_messages(
            {"parser_version": "material.title.v1", "content": "[]"}, {"type": "person_in_views"}
        )
