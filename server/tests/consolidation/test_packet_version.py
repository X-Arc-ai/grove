"""A belief operation uses the belief version, independent of its item version."""

import pytest

from grove.runner.passes import end_pass, start_pass
from grove.store.query import WorldQuery
from grove.writer.runs import enqueue_job
from tests.beliefs.test_state_machine import attach, data, establish, op, state
from tests.consolidation.test_connector import bearer
from tests.writer.helpers import supports


@pytest.mark.parametrize("change_after_read", [False, True])
def test_packet_version_accepts_current_belief_and_refuses_stale_belief(
    driver, seeded, rig, change_after_read
):
    belief_id, _, _, _ = establish(driver, seeded)
    saved = state(driver, seeded, belief_id)
    query = WorldQuery(driver, seeded)
    item = query.read("MATCH (i:Item {id: $id}) RETURN i", id=belief_id)[0]["i"]
    assert saved["state"] == "established"
    assert saved["version"] > item["version"]
    enqueue_job(
        driver,
        seeded,
        agent="connector",
        trigger="belief-version-check",
        input_references=[belief_id],
        priority=10,
    )
    client, auth, _, settings = rig
    live = start_pass(driver, auth, seeded, agent="connector", settings=settings)
    assert live is not None

    def read_packet():
        response = client.get("/v1/internal/brain/pass", headers=bearer(live))
        assert response.status_code == 200, response.text
        return response.json()

    def propose(document, evidence):
        visible = next(row for row in document["beliefs"] if row["belief_id"] == belief_id)
        envelope = document["batch_form"]["batch"]
        envelope["evidence"] = [evidence]
        envelope["belief_ops"] = [
            op(
                "retire_belief",
                target=belief_id,
                version=visible["version"],
                evidence_refs=supports(evidence["ev_id"]),
                end_class="world_changed",
            )
        ]
        return client.post(
            "/v1/internal/brain/propose", json={"batch": envelope}, headers=bearer(live)
        )

    document = read_packet()
    if change_after_read:
        _, fourth = data(driver, seeded, 4)
        attach(driver, seeded, belief_id, fourth)
        before = state(driver, seeded, belief_id)
        _, fresh = data(driver, seeded, 5)
        response = propose(document, fresh)
        assert response.status_code == 422, response.text
        assert response.json()["refusals"][0]["rule"] == "version_conflict"
        assert state(driver, seeded, belief_id) == before
        assert (
            query.read(
                "MATCH (e:Evidence {target_id: $id, source_id: $source}) RETURN e",
                id=belief_id,
                source=fresh["source_id"],
            )
            == []
        )
        document = read_packet()
    else:
        _, fresh = data(driver, seeded, 4)

    response = propose(document, fresh)
    assert response.status_code == 200, response.text
    assert state(driver, seeded, belief_id)["state"] == "superseded"
    assert end_pass(driver, seeded, live.run_id, outcome="ok")["outcome"] == "ok"
