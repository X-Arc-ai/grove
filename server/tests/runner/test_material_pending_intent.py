"""Pending canonical erasure must stop material Requests before disclosure or consumption."""

import importlib

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.runner.brain import pass_input
from grove.runner.passes import caller_for, end_pass, run_record, start_pass
from grove.store.query import WorldQuery
from grove.views.session import token_hash
from grove.writer import apply_request, claim_next_request, finish_request
from grove.writer.forget_ledger import LedgerUnavailable, read_ledger
from grove.writer.import_queue import claim_interactive_request
from grove.writer.requests import pending_requests
from grove.writer.restore import replay_ledger
from tests.runner.helpers import FAST, new_auth, queue_store
from tests.writer import test_materials
from tests.writer.test_material_forget import approve_material

material_world = test_materials.material_world


def pending_delete(driver, context, monkeypatch, item):
    session, cookie, ledger = context
    target, approval = approve_material(driver, context, item)
    module = importlib.import_module("grove.writer.forget")

    def fail(*args):
        raise RuntimeError("injected transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "write_inventory", fail)
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            module.forget(
                driver,
                session.world_id,
                target,
                approval_token=approval,
                actor=session.user_id,
                material_session=session,
                session_hash=token_hash(cookie),
                ledger_dir=ledger,
            )
    assert len(read_ledger(ledger, session.world_id)) == 1
    assert (
        WorldQuery(driver, session.world_id).read("MATCH (f:ForgetLedger) RETURN count(f) AS n")[0][
            "n"
        ]
        == 0
    )


def request_rows(driver, world):
    return WorldQuery(driver, world).read("MATCH (r:Request) RETURN r ORDER BY r.id")


def test_pending_material_intent_refuses_new_pass_and_claim(driver, material_world, monkeypatch):
    world = material_world[0].world_id
    item = test_materials.write_note(driver, material_world)["id"]
    pending_delete(driver, material_world, monkeypatch, item)
    before = request_rows(driver, world)
    auth, token = new_auth()
    app = create_app(driver=driver)
    app.state.runner_auth = auth
    app.state.pass_settings = FAST
    client = TestClient(app)
    response = client.post(
        "/v1/internal/runner/passes",
        headers={"Authorization": "Bearer " + token},
        json={"world_id": world, "agent": "learner"},
    )
    assert response.status_code == 503
    assert "Mira" not in response.text and "grant" not in response.json()
    assert request_rows(driver, world) == before
    assert WorldQuery(driver, world).read("MATCH (r:RunRecord) RETURN count(r) AS n")[0]["n"] == 0
    with pytest.raises(LedgerUnavailable):
        claim_next_request(driver, world, caller_for("learner"), worker="pending-control")
    assert request_rows(driver, world) == before


@pytest.mark.parametrize("endpoint", ["pass", "finish", "propose"])
def test_pending_material_intent_refuses_claimed_http_work(
    driver, material_world, monkeypatch, endpoint
):
    world = material_world[0].world_id
    item = test_materials.write_note(driver, material_world)["id"]
    auth, _ = new_auth()
    grant = start_pass(driver, auth, world, agent="learner", settings=FAST)
    record = run_record(driver, world, grant.run_id)
    packet = pass_input(driver, world, record)
    pending_delete(driver, material_world, monkeypatch, item)
    before = request_rows(driver, world)
    app = create_app(driver=driver)
    app.state.runner_auth = auth
    client = TestClient(app, headers={"Authorization": "Bearer " + grant.pass_token})
    body = {"request_id": grant.input_ids[0]}
    if endpoint == "pass":
        response = client.get("/v1/internal/brain/pass")
    else:
        if endpoint == "propose":
            body["batch"] = packet["batch_form"]["batch"]
        response = client.post("/v1/internal/brain/" + endpoint, json=body)
    assert response.status_code == 503
    assert "Mira" not in response.text and "Cedar" not in response.text
    assert request_rows(driver, world) == before


@pytest.mark.parametrize("operation", ["input", "finish", "apply"])
def test_pending_material_intent_guards_direct_work(driver, material_world, monkeypatch, operation):
    world = material_world[0].world_id
    item = test_materials.write_note(driver, material_world)["id"]
    grant = start_pass(driver, new_auth()[0], world, agent="learner", settings=FAST)
    record = run_record(driver, world, grant.run_id)
    packet = pass_input(driver, world, record)
    pending_delete(driver, material_world, monkeypatch, item)
    before = request_rows(driver, world)
    with pytest.raises(LedgerUnavailable):
        if operation == "input":
            pass_input(driver, world, record)
        elif operation == "finish":
            finish_request(
                driver, world, caller_for("learner"), grant.input_ids[0], worker=grant.run_id
            )
        else:
            apply_request(
                driver,
                world,
                caller_for("learner"),
                grant.input_ids[0],
                packet["batch_form"]["batch"],
                run_id=grant.run_id,
            )
    assert request_rows(driver, world) == before


def test_material_dispatch_resumes_after_canonical_recovery(driver, material_world, monkeypatch):
    world = material_world[0].world_id
    erased = test_materials.write_note(driver, material_world, title="First", body="Erase this.")
    test_materials.write_note(driver, material_world, title="Second", body="Retain these words.")
    pending_delete(driver, material_world, monkeypatch, erased["id"])
    from grove.writer.restore import _keep_ledger

    ledger = read_ledger(material_world[2], world)
    replay_ledger(driver, world, ledger)
    _keep_ledger(driver, world, ledger)
    grant = start_pass(driver, new_auth()[0], world, agent="learner", settings=FAST)
    packet = pass_input(driver, world, run_record(driver, world, grant.run_id))
    assert "Retain these words." in packet["data"]
    assert "Erase this." not in packet["data"]
    assert len(packet["requests"]) == 1


def test_nonmaterial_dispatch_needs_no_material_ledger(driver, material_world, monkeypatch):
    world = material_world[0].world_id
    monkeypatch.delenv("GROVE_FORGET_LEDGER_DIR")
    queue_store(driver, world)
    grant = start_pass(driver, new_auth()[0], world, agent="learner", settings=FAST)
    packet = pass_input(driver, world, run_record(driver, world, grant.run_id))
    assert packet["requests"][0]["input_complete"]


@pytest.mark.parametrize("material_state", ["saved", "retry", "leased"])
@pytest.mark.parametrize("ledger_state", ["pending", "unconfigured"])
def test_not_due_material_preserves_nonmaterial_dispatch(
    driver, material_world, monkeypatch, material_state, ledger_state
):
    world = material_world[0].world_id
    test_clock = clock.TestClock()
    test_clock.set(clock.now())
    with clock.using(test_clock):
        item = test_materials.write_note(driver, material_world)["id"]
        caller = caller_for("learner")
        auth, token = new_auth()
        if material_state == "leased":
            claimed = claim_next_request(driver, world, caller, worker="other-worker")
            assert claimed is not None
        else:
            first = start_pass(driver, auth, world, agent="learner", settings=FAST)
            if material_state == "saved":
                finish_request(driver, world, caller, first.input_ids[0], worker=first.run_id)
            end_pass(driver, world, first.run_id, outcome="ok")
        before = request_rows(driver, world)
        assert len(before) == 1
        assert (
            before[0]["r"]["status"]
            == {"saved": "saved", "retry": "failed", "leased": "applying"}[material_state]
        )
        assert pending_requests(driver, world) == []
        pending_delete(driver, material_world, monkeypatch, item)
        ledger_before = read_ledger(material_world[2], world)
        if ledger_state == "unconfigured":
            monkeypatch.delenv("GROVE_FORGET_LEDGER_DIR")
        ordinary = queue_store(driver, world)
        assert [row.request_id for row in pending_requests(driver, world)] == [ordinary]
        app = create_app(driver=driver)
        app.state.runner_auth = auth
        app.state.pass_settings = FAST
        response = TestClient(app).post(
            "/v1/internal/runner/passes",
            headers={"Authorization": "Bearer " + token},
            json={"world_id": world, "agent": "learner"},
        )
        assert response.status_code == 201
        grant = response.json()["grant"]
        assert grant["input_ids"] == [ordinary]
        packet = pass_input(driver, world, run_record(driver, world, grant["run_id"]))
        assert len(packet["requests"]) == 1 and packet["requests"][0]["input_complete"]
        assert "Elm Street" in packet["data"]
        assert "Mira" not in packet["data"] and "Cedar" not in packet["data"]
        finish_request(driver, world, caller, ordinary, worker=grant["run_id"])
        end_pass(driver, world, grant["run_id"], outcome="ok")
        assert [row for row in request_rows(driver, world) if row["r"]["id"] != ordinary] == before
        assert read_ledger(material_world[2], world) == ledger_before
        if material_state != "saved":
            test_clock.advance(seconds=600)
            assert [row.request_id for row in pending_requests(driver, world)] == [
                before[0]["r"]["id"]
            ]
            requests_before = request_rows(driver, world)
            runs_before = WorldQuery(driver, world).read(
                "MATCH (r:RunRecord) RETURN r ORDER BY r.id"
            )
            with pytest.raises(LedgerUnavailable):
                start_pass(driver, auth, world, agent="learner", settings=FAST)
            assert request_rows(driver, world) == requests_before
            assert (
                WorldQuery(driver, world).read("MATCH (r:RunRecord) RETURN r ORDER BY r.id")
                == runs_before
            )


def test_pending_material_intent_between_preflight_and_claim_refuses(
    driver, material_world, monkeypatch
):
    from grove.runner import passes

    world = material_world[0].world_id
    item = test_materials.write_note(driver, material_world)["id"]
    before = request_rows(driver, world)
    start = passes.run_rows.start_run

    def start_then_delete(*args, **kwargs):
        row = start(*args, **kwargs)
        pending_delete(driver, material_world, monkeypatch, item)
        return row

    monkeypatch.setattr(passes.run_rows, "start_run", start_then_delete)
    with pytest.raises(LedgerUnavailable):
        start_pass(driver, new_auth()[0], world, agent="learner", settings=FAST)
    assert request_rows(driver, world) == before
    runs = WorldQuery(driver, world).read("MATCH (r:RunRecord) RETURN r.outcome AS outcome")
    assert runs == [{"outcome": "failed"}]


def test_material_input_holds_ledger_lock_through_source_read(driver, material_world, monkeypatch):
    import fcntl

    from grove.runner import brain

    world = material_world[0].world_id
    test_materials.write_note(driver, material_world)
    grant = start_pass(driver, new_auth()[0], world, agent="learner", settings=FAST)
    read = brain._request_input
    observed = []

    def read_under_lock(*args, **kwargs):
        with (material_world[2] / ".lock").open("rb") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append(True)
        return read(*args, **kwargs)

    monkeypatch.setattr(brain, "_request_input", read_under_lock)
    packet = pass_input(driver, world, run_record(driver, world, grant.run_id))
    assert observed == [True] and packet["requests"][0]["input_complete"]


def test_pending_material_intent_refuses_the_interactive_import_claim(
    driver, material_world, monkeypatch
):
    """A pass claims through the import queue now, so that path must hold the same guard."""
    world = material_world[0].world_id
    item = test_materials.write_note(driver, material_world)["id"]
    pending_delete(driver, material_world, monkeypatch, item)
    before = request_rows(driver, world)
    with pytest.raises(LedgerUnavailable):
        claim_interactive_request(
            driver, world, caller_for("learner"), worker="pending-control", lease_seconds=60
        )
    assert request_rows(driver, world) == before
