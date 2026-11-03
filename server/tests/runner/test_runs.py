"""The job runner's bookkeeping rows (grove.writer.runs): one live run per world, abandoned runs,
and jobs that merge and claim once."""

import json
import threading

from grove.store.query import WorldQuery
from grove.writer import runs

CAPS = {"model": "muse-spark-1.3", "effort": "low", "token_cap": 1000, "time_cap": 60}


def start(driver, world, run_id, **over):
    fields = {
        "run_id": run_id,
        "agent": "learner",
        "trigger": "queue",
        **CAPS,
        "stale_margin_seconds": 10,
    } | over
    return runs.start_run(driver, world, **fields)


def record(driver, world, run_id):
    return WorldQuery(driver, world).read("MATCH (r:RunRecord {id: $id}) RETURN r", id=run_id)[0][
        "r"
    ]


def test_one_live_run_per_world(driver, seeded, frozen):
    assert start(driver, seeded, "run-a")["outcome"] == "running"
    assert start(driver, seeded, "run-b") is None
    frozen.advance(seconds=69)
    assert start(driver, seeded, "run-b") is None


def test_a_run_past_its_cap_and_margin_is_abandoned_when_the_next_starts(driver, seeded, frozen):
    start(driver, seeded, "run-a")
    frozen.advance(seconds=71)
    assert start(driver, seeded, "run-b")["id"] == "run-b"
    first = record(driver, seeded, "run-a")
    assert first["outcome"] == "abandoned" and first["finished"] is not None
    assert first["version"] == 2


def test_a_run_ends_once(driver, seeded, frozen):
    start(driver, seeded, "run-a")
    ended = runs.finish_run(
        driver,
        seeded,
        "run-a",
        outcome="ok",
        tokens_in=5,
        tokens_cached=1,
        tokens_out=2,
        summary="two lines",
    )
    assert ended["outcome"] == "ok" and ended["tokens_in"] == 5
    again = runs.finish_run(
        driver, seeded, "run-a", outcome="killed", tokens_in=9, tokens_cached=9, tokens_out=9
    )
    assert again["outcome"] == "ok" and again["tokens_in"] == 5
    assert json.loads(again["proposed_operations"])["summary"] == "two lines"


def test_a_second_trigger_adds_its_inputs_to_the_queued_job(driver, seeded, frozen):
    first = runs.enqueue_job(
        driver,
        seeded,
        agent="curator",
        trigger="after_update",
        input_references=["a", "b"],
        priority=20,
    )
    second = runs.enqueue_job(
        driver,
        seeded,
        agent="curator",
        trigger="after_update",
        input_references=["b", "c"],
        priority=20,
    )
    assert first == second
    rows = WorldQuery(driver, seeded).read("MATCH (j:Job) RETURN j.input_references AS refs")
    assert len(rows) == 1 and json.loads(rows[0]["refs"]) == ["a", "b", "c"]


def test_two_claims_of_one_job_give_one_winner(driver, seeded, frozen):
    runs.enqueue_job(
        driver, seeded, agent="curator", trigger="after_update", input_references=["a"], priority=20
    )
    barrier = threading.Barrier(2)
    won: list = []

    def claim(worker: str) -> None:
        barrier.wait()
        won.append(runs.claim_job(driver, seeded, agent="curator", worker=worker))

    threads = [threading.Thread(target=claim, args=(f"worker-{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [job for job in won if job is not None]
    assert len(winners) == 1 and winners[0]["state"] == "claimed"


def test_a_job_backs_off_then_fails_after_its_attempts(driver, seeded, frozen):
    job_id = runs.enqueue_job(
        driver, seeded, agent="connector", trigger="revisions", input_references=[], priority=10
    )
    for attempt in range(1, runs.JOB_MAX_ATTEMPTS + 1):
        claimed = runs.claim_job(driver, seeded, agent="connector", worker=f"w{attempt}")
        assert claimed is not None and claimed["attempts"] == attempt
        state = runs.end_job(driver, seeded, job_id, worker=f"w{attempt}", done=False)
        if attempt < runs.JOB_MAX_ATTEMPTS:
            assert state == "queued"
            assert runs.claim_job(driver, seeded, agent="connector", worker="early") is None
            frozen.advance(seconds=runs.JOB_BACKOFF_SECONDS[attempt - 1])
        else:
            assert state == "failed"
    assert runs.claim_job(driver, seeded, agent="connector", worker="late") is None


def test_only_the_holder_ends_a_job(driver, seeded, frozen):
    job_id = runs.enqueue_job(
        driver, seeded, agent="curator", trigger="after_update", input_references=["a"], priority=20
    )
    runs.claim_job(driver, seeded, agent="curator", worker="holder")
    assert runs.end_job(driver, seeded, job_id, worker="intruder", done=True) == "claimed"
    assert runs.end_job(driver, seeded, job_id, worker="holder", done=True) == "done"
