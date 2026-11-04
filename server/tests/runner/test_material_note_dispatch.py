"""A material note follows real pass input, a scripted model and the native writer."""

import json

import httpx

from grove.runner.brain import pass_input
from grove.runner.passes import caller_for, end_pass, run_record, start_pass
from grove.store.query import WorldQuery
from grove.writer import apply_request, request_state
from tests.runner.helpers import FAST, new_auth
from tests.runner.scripted_model import ScriptedModel
from tests.writer import test_materials

material_world = test_materials.material_world
write_note = test_materials.write_note


def test_note_uses_real_learner_input_and_saves_graph_effect(driver, material_world):
    session, cookie, ledger = material_world
    write_note(driver, material_world, body="Mira joins the Cedar launch.")
    grant = start_pass(driver, new_auth()[0], session.world_id, agent="learner", settings=FAST)
    assert grant and grant.agent == "learner"
    packet = pass_input(
        driver, session.world_id, run_record(driver, session.world_id, grant.run_id)
    )
    entry = packet["requests"][0]
    assert entry["input_complete"] and entry["held_messages"] == 0
    assert entry["messages"][0]["speaker_kind"] == "person"
    assert entry["messages"][0]["at"]
    form = packet["batch_form"]
    batch = form["batch"]
    batch["ops"] = [batch["ops"][0] | {"title": "Mira", "op_id": "material-person"}]
    # The existing local SSE model returns a complete proposal. It never reaches a provider.
    with ScriptedModel() as model:
        model.script([{"tool": "propose", "args": {"batch": batch}}])
        response = httpx.post(
            model.gateway_url + "/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": "# Learner"},
                    {"role": "user", "content": packet["data"]},
                ],
                "tools": [{"type": "function", "function": {"name": "propose"}}],
                "stream": True,
            },
        )
        arguments = ""
        for line in response.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                for choice in chunk.get("choices", []):
                    for call in choice.get("delta", {}).get("tool_calls", []):
                        arguments += call.get("function", {}).get("arguments", "")
        proposed = json.loads(arguments)["batch"]
        assert model.steps_taken == 1
    result = apply_request(
        driver,
        session.world_id,
        caller_for("learner"),
        form["request_id"],
        proposed,
        run_id=grant.run_id,
    )
    assert result.api_state == "saved"
    end_pass(driver, session.world_id, grant.run_id, outcome="ok")
    assert request_state(driver, session.world_id, form["request_id"]).status == "saved"
    assert WorldQuery(driver, session.world_id).read(
        "MATCH (i:Item {title: 'Mira'}) RETURN i.id AS id"
    )
