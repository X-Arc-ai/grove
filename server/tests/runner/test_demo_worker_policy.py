"""Synthetic unit controls for the persisted demo's autonomous-job boundary.

Database queries and writer calls are controlled here; the scheduler, designation
policy, agent inference and token codecs are real. No native store or provider runs.
"""

from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from grove import demo_policy
from grove.consolidation import packet
from grove.models.config import load_catalog
from grove.models.gateway_auth import verify as verify_gateway
from grove.runner import passes
from grove.runner.tokens import verify_pass_token
from tests.runner.helpers import new_auth

WORLD = "t_hosts-b2_11111111-1111-4111-8111-111111111111"
DESIGNATION = {
    "world_id": WORLD,
    "user_id": "synthetic-owner",
    "demo_version": "cedar-v1",
    "demo_world_id": WORLD,
    "designated_user_id": "synthetic-owner",
    "claimed_demo_version": "cedar-v1",
}


@pytest.fixture
def state(monkeypatch, frozen):
    state = SimpleNamespace(
        designation=deepcopy(DESIGNATION),
        jobs=[],
        runs=[],
        request=None,
        changed=[{"id": "synthetic-item"}],
        reads=[],
        writes=[],
        auth=new_auth()[0],
        driver=object(),
        now=frozen.now(),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("the refused autonomous path reached a writer, packet, or token function")

    def read(query, **params):
        state.reads.append(query)
        if query == passes._DUE_REQUEST:
            return [{"id": state.request.id}] if state.request is not None else []
        if query == passes._DUE_JOBS:
            return [{"agent": job["agent_or_job_type"]} for job in state.jobs]
        if query == passes._LIVE_RUNS:
            return deepcopy(state.runs)
        if query == passes._RUN_CHANGED:
            return deepcopy(state.changed)
        if query.startswith("MATCH (b:Budget"):
            return []
        if "j.state IN ['queued', 'claimed']" in query:
            return [
                {"id": job["id"]} for job in state.jobs if job["agent_or_job_type"] == "connector"
            ]
        if "state: 'failed'" in query:
            return []
        if "r.id STARTS WITH $prefix" in query:
            return []  # This rig holds no import Request.
        pytest.fail("unexpected world query")

    def designation_read(query, **params):
        assert query == demo_policy.DESIGNATION_QUERY
        assert params == {"world_id": WORLD}
        return [deepcopy(state.designation)]

    monkeypatch.delenv("GROVE_DEMO_EMAIL_HASH", raising=False)
    monkeypatch.setattr(passes, "_worlds", lambda driver: [WORLD])
    monkeypatch.setattr(passes, "WorldQuery", lambda *args: SimpleNamespace(read=read))
    monkeypatch.setattr(
        demo_policy, "PlatformQuery", lambda *args: SimpleNamespace(read=designation_read)
    )
    for name in (
        "enqueue_job",
        "start_run",
        "claim_job",
        "set_run_inputs",
        "finish_run",
        "end_job",
        "abandon_stale_runs",
    ):
        monkeypatch.setattr(passes.run_rows, name, forbidden)
    for name in (
        "claim_interactive_request",
        "claim_import_request",
        "_ensure_episode",
        "mint_pass_token",
        "mint_gateway_token",
    ):
        monkeypatch.setattr(passes, name, forbidden)
    monkeypatch.setattr(packet, "work", forbidden)
    return state


def queued_job(agent):
    return {
        "id": "synthetic-" + agent,
        "agent_or_job_type": agent,
        "state": "queued",
        "attempts": 2,
        "input_references": '["synthetic-item"]',
        "version": 3,
    }


def test_demo_due_work_does_not_enqueue_or_build_a_packet(state):
    assert passes.due_work(state.driver) == []


@pytest.mark.parametrize("agent", ["curator", "connector"])
@pytest.mark.parametrize("explicit", [False, True])
def test_existing_demo_jobs_stay_queued_without_run_claim_or_tokens(state, agent, explicit):
    state.jobs = [queued_job(agent)]
    before = deepcopy(state.jobs)
    assert passes.due_work(state.driver) == []
    assert (
        passes.start_pass(state.driver, state.auth, WORLD, agent=agent if explicit else None)
        is None
    )
    assert state.jobs == before
    assert state.writes == []


def test_demo_without_request_preserves_stale_runs_for_operator_continuation(state):
    state.jobs = [queued_job("connector")]
    state.runs = [
        {
            "id": "stale-run",
            "agent": "connector",
            "time_cap": 1,
            "started": state.now - timedelta(days=1),
        }
    ]
    before = deepcopy((state.jobs, state.runs))
    assert passes.due_work(state.driver) == []
    assert (state.jobs, state.runs) == before


@pytest.mark.parametrize(
    "field", ["demo_version", "demo_world_id", "claimed_demo_version", "designated_user_id"]
)
@pytest.mark.parametrize("value", ["", "corrupt"])
def test_any_partial_designation_refuses_all_autonomous_entry_points(state, field, value):
    state.designation = {field: value}
    state.jobs = [queued_job("curator"), queued_job("connector")]
    before = deepcopy(state.jobs)
    assert passes.due_work(state.driver) == []
    assert passes.connector_due(state.driver, WORLD) is False
    passes._ensure_connector_job(state.driver, WORLD)
    passes._schedule_after(state.driver, WORLD, "learner", "synthetic-run")
    for agent in (None, "curator", "connector"):
        assert passes.start_pass(state.driver, state.auth, WORLD, agent=agent) is None
    assert state.jobs == before


def test_demo_connector_due_refuses_before_packet_read(state):
    assert passes.connector_due(state.driver, WORLD) is False


def test_demo_ensure_connector_refuses_even_if_due_answer_is_stale(state, monkeypatch):
    monkeypatch.setattr(passes, "connector_due", lambda *args: True)
    passes._ensure_connector_job(state.driver, WORLD)


@pytest.mark.parametrize("agent", ["learner", "curator", "connector"])
def test_demo_pass_end_does_not_enqueue_curator_or_connector(state, agent):
    passes._schedule_after(state.driver, WORLD, agent, "synthetic-run")


def allow_start(state, monkeypatch):
    # Observe the real start_pass sequence with an in-memory writer boundary.
    def start_run(driver, world_id, **row):
        state.writes.append(("start_run", row["agent"]))
        return {"started": state.now}

    def claim_job(driver, world_id, *, agent, worker):
        state.writes.append(("claim_job", agent))
        return next(job for job in state.jobs if job["agent_or_job_type"] == agent)

    def claim_request(*args, **kwargs):
        state.writes.append(("claim_request", "learner"))
        result, state.request = state.request, None
        return result

    def ensure_episode(*args):
        state.writes.append(("episode", "learner"))

    monkeypatch.setattr(passes.run_rows, "start_run", start_run)
    monkeypatch.setattr(passes.run_rows, "claim_job", claim_job)
    monkeypatch.setattr(passes.run_rows, "set_run_inputs", lambda *args: None)
    monkeypatch.setattr(passes, "claim_interactive_request", claim_request)
    # This rig has no store, and the guard reads one before each learner pass.
    monkeypatch.setattr(passes, "material_request_guard", lambda *_args, **_kw: nullcontext())
    monkeypatch.setattr(passes, "_ensure_episode", ensure_episode)
    from grove.models.gateway_auth import mint
    from grove.runner.tokens import mint_pass_token

    monkeypatch.setattr(passes, "mint_pass_token", mint_pass_token)
    monkeypatch.setattr(passes, "mint_gateway_token", mint)


@pytest.mark.parametrize("explicit", [False, True])
def test_demo_cedar_request_keeps_learner_selection_and_real_token_grant(
    state, monkeypatch, explicit
):
    state.request = SimpleNamespace(
        id="cedar-request", source_id="cedar-source", text=demo_policy.DEMO_TEXT
    )
    assert passes.due_work(state.driver) == [{"world_id": WORLD, "agent": "learner"}]
    allow_start(state, monkeypatch)
    grant = passes.start_pass(
        state.driver,
        state.auth,
        WORLD,
        agent="learner" if explicit else None,
        settings=passes.PassSettings(learner_requests=1),
    )
    assert grant.agent == "learner" and grant.input_ids == ["cedar-request"]
    assert grant.job_id is None
    assert state.writes == [
        ("start_run", "learner"),
        ("claim_request", "learner"),
        ("episode", "learner"),
    ]
    claims = verify_pass_token(state.auth.gateway_secret, grant.pass_token)
    gateway = verify_gateway(state.auth.gateway_secret, load_catalog(), grant.gateway_token)
    assert (claims.world_id, claims.run_id, claims.agent) == (WORLD, grant.run_id, "learner")
    assert (gateway.world_id, gateway.run_id, gateway.job) == (WORLD, grant.run_id, "update_pass")
    passes._schedule_after(state.driver, WORLD, "learner", grant.run_id)


def test_demo_request_that_disappears_between_reads_does_not_select_jobs(state, monkeypatch):
    state.jobs = [queued_job("curator")]
    original = passes.WorldQuery(state.driver, WORLD).read
    calls = 0

    def read(query, **params):
        nonlocal calls
        if query == passes._DUE_REQUEST:
            calls += 1
            return [{"id": "cedar-request"}] if calls == 1 else []
        return original(query, **params)

    monkeypatch.setattr(passes, "WorldQuery", lambda *args: SimpleNamespace(read=read))
    assert passes.due_work(state.driver) == []
    assert calls == 2


def test_demo_learner_can_still_preempt_an_existing_autonomous_pass(state):
    state.request = SimpleNamespace(id="cedar-request")
    state.runs = [{"id": "live-run", "agent": "connector", "time_cap": 600, "started": state.now}]
    assert passes.due_work(state.driver) == [
        {"world_id": WORLD, "agent": "learner", "preempt": "live-run"}
    ]


@pytest.mark.parametrize("agent", ["curator", "connector"])
@pytest.mark.parametrize("explicit", [False, True])
def test_ordinary_world_keeps_autonomous_selection_claim_and_grant(
    state, monkeypatch, agent, explicit
):
    state.designation = {"demo_version": None, "demo_world_id": None}
    state.jobs = [queued_job(agent)]
    monkeypatch.setattr(packet, "work", lambda *args: {"triggers": []})
    assert passes.due_work(state.driver) == [{"world_id": WORLD, "agent": agent}]
    allow_start(state, monkeypatch)
    grant = passes.start_pass(state.driver, state.auth, WORLD, agent=agent if explicit else None)
    assert grant.agent == agent and grant.job_id == "synthetic-" + agent
    assert grant.input_ids == ["synthetic-item"]
    assert state.writes == [("start_run", agent), ("claim_job", agent)]
    assert verify_pass_token(state.auth.gateway_secret, grant.pass_token).agent == agent
    assert (
        verify_gateway(state.auth.gateway_secret, load_catalog(), grant.gateway_token).job
        == (passes.AGENTS[agent][1])
    )


def test_ordinary_world_still_enqueues_connector_and_curator(state, monkeypatch):
    state.designation = {}
    pending = {"triggers": ["revisions"], "changed": [{"id": "synthetic-item"}]}
    monkeypatch.setattr(packet, "work", lambda *args: pending)
    monkeypatch.setattr(packet, "job_trigger", lambda value: "synthetic-revisions")

    def enqueue(driver, world_id, **row):
        assert world_id == WORLD
        state.writes.append(row)
        state.jobs.append(queued_job(row["agent"]))

    monkeypatch.setattr(passes.run_rows, "enqueue_job", enqueue)
    assert passes.connector_due(state.driver, WORLD) is True
    assert passes.due_work(state.driver) == [{"world_id": WORLD, "agent": "connector"}]
    assert [row["agent"] for row in state.writes] == ["connector"]
    passes._schedule_after(state.driver, WORLD, "learner", "synthetic-run")
    assert [row["agent"] for row in state.writes] == ["connector", "curator"]
    assert state.writes[1]["input_references"] == ["synthetic-item"]
