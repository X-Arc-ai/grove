"""The runner loop with a stand-in for OpenCode: the 60 s start, the watchdog, a stop, a killed
runner process, and the environment OpenCode gets. No model is called.

The stand-in is a small program that does what an agent would do through the same shell:
`$SHELL -c "brain pass"`, then a `brain propose` heredoc.
"""

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.runner.loop import (
    OPENCODE_FLAGS,
    LoopSettings,
    PassRun,
    Runner,
    prepare_home,
    run_pass,
    summarise,
)
from grove.runner.passes import PassSettings
from grove.store.query import WorldQuery
from grove.writer import request_state
from tests.runner.helpers import Served, new_auth, queue_store, seeded_world

REPO = Path(__file__).resolve().parents[3]
BIN = Path(sys.executable).parent
FAKE = """#!{python}
import json, os, subprocess, sys, threading
here = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(here, "mode")).read().strip()
with open(os.path.join(here, "env.json"), "w") as out:
    json.dump(dict(os.environ), out)
with open(os.path.join(here, "fake.pid"), "w") as out:
    out.write(str(os.getpid()))
shell = os.environ["SHELL"]
if mode == "propose":
    read = subprocess.run([shell, "-c", "brain pass"], capture_output=True, text=True)
    packet = json.loads(read.stdout)
    form = packet["batch_form"]
    form["batch"]["ops"] = [form["batch"]["ops"][0] | {{"title": "Lina Okafor"}}]
    command = "brain propose <<'EOF'\\n" + json.dumps(form) + "\\nEOF"
    answer = subprocess.run([shell, "-c", command], capture_output=True, text=True).stdout
    print(json.dumps({{"type": "tool_use", "part": {{"tool": "bash"}}}}))
    print(json.dumps({{"type": "text", "part": {{"text": json.loads(answer)["state"]}}}}))
    sys.exit(0)
if mode == "hang":
    child = subprocess.Popen(["/bin/sleep", "600"])
    with open(os.path.join(here, "child.pid"), "w") as out:
        out.write(str(child.pid))
    threading.Event().wait(600)
sys.exit(3)
"""


def wait_for(check, seconds: float):
    stop = threading.Event()
    for _ in range(int(seconds * 10)):
        value = check()
        if value:
            return value
        stop.wait(0.1)
    return check()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def rig(driver, make_world, tmp_path):
    world_id = seeded_world(driver, make_world())
    auth, token = new_auth()
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    fake = fake_dir / "opencode"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)

    def app(**caps):
        settings = PassSettings(only_worlds=frozenset({world_id}), **caps)
        return create_app(
            ApiSettings(public_base_url="http://127.0.0.1:8000"),
            driver=driver,
            runner_auth=auth,
            pass_settings=settings,
        )

    def loop_settings(url: str, mode: str, **over) -> LoopSettings:
        (fake_dir / "mode").write_text(mode)
        settings = LoopSettings(
            api_url=url,
            runner_token=token,
            gateway_url="http://127.0.0.1:1/v1",
            home=tmp_path / "home",
            config_dir=REPO / "runner",
            opencode_bin=str(fake),
            brain_bin=str(BIN / "brain"),
            shell_bin=str(BIN / "grove-brain-shell"),
        )
        return replace(settings, **over)

    def record():
        rows = WorldQuery(driver, world_id).read("MATCH (r:RunRecord) RETURN r ORDER BY r.started")
        return [row["r"] for row in rows]

    return world_id, app, loop_settings, record, fake_dir, token


def test_the_service_polls_often_enough_for_the_60_second_start():
    """Regression (R4 verifier): a slow default poll stayed green while each test set its own."""
    env = {
        "GROVE_API_URL": "http://api:8000",
        "GROVE_RUNNER_TOKEN": "x" * 40,
        "GROVE_GATEWAY_URL": "http://gateway:18300/v1",
    }
    assert LoopSettings.from_environment(env).poll_seconds <= 10.0


def test_a_queued_store_starts_a_learner_pass_within_60_seconds(driver, rig, tmp_path):
    world_id, app, loop_settings, record, _, token = rig
    with Served(app()) as served:
        # The poll of the service as the box runs it: from its environment, with no override.
        settings = loop_settings(served.url, "propose")
        env = {
            "GROVE_API_URL": served.url,
            "GROVE_RUNNER_TOKEN": token,
            "GROVE_GATEWAY_URL": settings.gateway_url,
            "GROVE_RUNNER_HOME": str(settings.home),
            "GROVE_RUNNER_CONFIG": str(settings.config_dir),
            "GROVE_OPENCODE_BIN": settings.opencode_bin,
        }
        runner = Runner(LoopSettings.from_environment(env))
        thread = threading.Thread(target=runner.serve, daemon=True)
        thread.start()
        threading.Event().wait(1.0)
        queued_at = clock.now()
        request_id = queue_store(driver, world_id)
        saved = wait_for(
            lambda: request_state(driver, world_id, request_id).api_state == "saved", 70
        )
        runner.stopping.set()
        thread.join(timeout=30)
    assert saved
    started = record()[0]["started"]
    assert (started - queued_at).total_seconds() <= 60
    titles = WorldQuery(driver, world_id).read(
        "MATCH (i:Item {title: 'Lina Okafor'}) RETURN i.id AS id"
    )
    assert len(titles) == 1


def test_the_watchdog_kills_the_pass_and_its_children_at_the_time_cap(driver, rig):
    world_id, app, loop_settings, record, fake_dir, _ = rig
    request_id = queue_store(driver, world_id)
    with Served(app(time_cap_seconds=3, lease_margin_seconds=30)) as served:
        runner = Runner(loop_settings(served.url, "hang"))
        prepare_home(runner.settings.home, runner.settings.config_dir)
        assert runner.tick() == 1
        assert wait_for(lambda: runner.running() == 0, 40)
    assert record()[0]["outcome"] == "timeout"
    state = request_state(driver, world_id, request_id)
    assert state.status == "failed" and state.last_error_code == "pass_timeout"
    child = int((fake_dir / "child.pid").read_text())
    assert wait_for(lambda: not alive(child), 10)


def test_a_stop_ends_the_running_pass_and_hands_its_request_back(driver, rig):
    world_id, app, loop_settings, record, fake_dir, _ = rig
    request_id = queue_store(driver, world_id)
    with Served(app(time_cap_seconds=120)) as served:
        runner = Runner(loop_settings(served.url, "hang"))
        prepare_home(runner.settings.home, runner.settings.config_dir)
        runner.tick()
        assert wait_for(lambda: (fake_dir / "child.pid").exists(), 20)
        runner.stop()
    assert record()[0]["outcome"] == "stopped"
    assert request_state(driver, world_id, request_id).last_error_code == "pass_stopped"


def test_a_failed_opencode_run_hands_its_request_back(driver, rig):
    world_id, app, loop_settings, record, _, _ = rig
    request_id = queue_store(driver, world_id)
    with Served(app()) as served:
        runner = Runner(loop_settings(served.url, "fail"))
        prepare_home(runner.settings.home, runner.settings.config_dir)
        runner.tick()
        assert wait_for(lambda: runner.running() == 0, 30)
    assert record()[0]["outcome"] == "failed"
    assert request_state(driver, world_id, request_id).retry_at is not None


def test_opencode_gets_the_pass_credentials_and_nothing_of_the_runner(driver, rig, monkeypatch):
    world_id, app, loop_settings, _, fake_dir, token = rig
    monkeypatch.setenv("GROVE_PLANTED_SECRET", "planted")
    queue_store(driver, world_id)
    with Served(app()) as served:
        runner = Runner(loop_settings(served.url, "propose"))
        prepare_home(runner.settings.home, runner.settings.config_dir)
        runner.tick()
        assert wait_for(lambda: runner.running() == 0, 30)
    env = json.loads((fake_dir / "env.json").read_text())
    assert "GROVE_PLANTED_SECRET" not in env and "GROVE_RUNNER_TOKEN" not in env
    assert token not in json.dumps(env)
    assert env["GROVE_PASS_TOKEN"].startswith("p1.") and env["GROVE_RUN_TOKEN"].startswith("g1.")
    assert env["SHELL"] == str(BIN / "grove-brain-shell")
    assert env["HOME"] == str(runner.settings.home)
    assert env["XDG_CONFIG_HOME"] == str(runner.settings.home / ".config")
    # OpenCode's session store (every tool output) lives in the pass's folder, which is gone.
    passes = runner.settings.home / "passes"
    assert env["XDG_DATA_HOME"].startswith(str(passes)) and env["TMPDIR"].startswith(str(passes))
    assert env["XDG_STATE_HOME"].startswith(str(passes))
    assert not Path(env["XDG_DATA_HOME"]).exists()
    for flag in OPENCODE_FLAGS:
        assert env[flag] == "1", flag
    assert {"OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_PROJECT_CONFIG"} <= set(env)


def test_a_killed_runner_leaves_its_request_queued_and_the_next_pass_applies_it_once(
    driver, rig, tmp_path
):
    world_id, app, loop_settings, record, fake_dir, token = rig
    request_id = queue_store(driver, world_id)
    with Served(app(time_cap_seconds=4, lease_margin_seconds=2)) as served:
        settings = loop_settings(served.url, "hang")
        env = {
            "PATH": "/usr/bin:/bin",
            "GROVE_API_URL": served.url,
            "GROVE_RUNNER_TOKEN": token,
            "GROVE_GATEWAY_URL": settings.gateway_url,
            "GROVE_RUNNER_HOME": str(settings.home),
            "GROVE_RUNNER_CONFIG": str(settings.config_dir),
            "GROVE_OPENCODE_BIN": settings.opencode_bin,
            "GROVE_RUNNER_POLL_SECONDS": "1",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "grove.runner"],
            env=env,
            cwd=Path(__file__).parents[2],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert wait_for(lambda: (fake_dir / "child.pid").exists(), 30)
            assert request_state(driver, world_id, request_id).status == "applying"
        finally:
            process.kill()
            process.wait(timeout=10)
        # The container dies with its main process: so do OpenCode and its children here.
        os.killpg(int((fake_dir / "fake.pid").read_text()), signal.SIGKILL)
        assert request_state(driver, world_id, request_id).status == "applying"
        assert record()[0]["outcome"] == "running"
        (fake_dir / "child.pid").unlink()
        runner = Runner(loop_settings(served.url, "propose", poll_seconds=1.0))
        thread = threading.Thread(target=runner.serve, daemon=True)
        thread.start()
        saved = wait_for(
            lambda: request_state(driver, world_id, request_id).api_state == "saved", 40
        )
        runner.stopping.set()
        thread.join(timeout=30)
    assert saved
    runs = record()
    assert [run["outcome"] for run in runs] == ["abandoned", "ok"]
    items = WorldQuery(driver, world_id).read(
        "MATCH (i:Item {title: 'Lina Okafor'}) RETURN i.id AS id"
    )
    assert len(items) == 1


def test_a_store_behind_a_running_curator_pass_starts_a_learner_within_60_seconds(driver, rig):
    world_id, app, loop_settings, record, fake_dir, _ = rig
    from grove.writer import runs

    # Force the CI ordering: the request is saved while its process still runs. Keep this
    # gate local to the preemption test; the other loop tests use the unmodified stand-in.
    fake = fake_dir / "opencode"
    program = fake.read_text()
    assert program.count("    sys.exit(0)") == 1
    program = program.replace(
        "    sys.exit(0)",
        """    open(os.path.join(here, "preempt_proposed"), "w").close()
    for _ in range(1200):
        if os.path.exists(os.path.join(here, "preempt_release")):
            break
        threading.Event().wait(0.05)
    else:
        sys.exit(4)
    sys.exit(0)""",
    )
    fake.write_text(program)

    def finish_learner(learner_id):
        (fake_dir / "preempt_release").touch()

        def completed():
            seen = record()
            if any(r["id"] == learner_id and r["outcome"] != "running" for r in seen):
                return seen
            return None

        return wait_for(completed, 30)

    runs.enqueue_job(
        driver,
        world_id,
        agent="curator",
        trigger="after_update",
        input_references=["x"],
        priority=20,
    )
    with Served(app(time_cap_seconds=300)) as served:
        runner = Runner(loop_settings(served.url, "hang"))
        thread = threading.Thread(target=runner.serve, daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: (fake_dir / "child.pid").exists(), 30)
            (fake_dir / "mode").write_text("propose")
            queued_at = clock.now()
            request_id = queue_store(driver, world_id)
            saved = wait_for(
                lambda: request_state(driver, world_id, request_id).api_state == "saved", 70
            )
            assert saved
            assert wait_for(lambda: (fake_dir / "preempt_proposed").exists(), 10)
            learners = [r for r in record() if r["agent"] == "learner"]
            assert len(learners) == 1 and learners[0]["outcome"] == "running"
            # A saved request is not a finished pass. Release the controlled process and wait
            # for this learner's terminal record before asking the service to stop.
            runs_seen = finish_learner(learners[0]["id"])
        finally:
            runner.stopping.set()
            thread.join(timeout=30)
    assert saved and runs_seen
    assert [(r["agent"], r["outcome"]) for r in runs_seen[:2]] == [
        ("curator", "preempted"),
        ("learner", "ok"),
    ]
    assert (runs_seen[1]["started"] - queued_at).total_seconds() <= 60


def test_with_no_runner_token_the_service_waits_and_stops_cleanly(tmp_path):
    """Regression: on a first deploy runner.env holds no token yet. A loop that exits makes the
    container restart in a loop, and `compose up --wait` fails the deploy."""
    env = {
        "PATH": "/usr/bin:/bin",
        "GROVE_API_URL": "http://127.0.0.1:1",
        "GROVE_GATEWAY_URL": "http://127.0.0.1:1/v1",
        "GROVE_RUNNER_HOME": str(tmp_path),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "grove.runner"],
        env=env,
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        threading.Event().wait(2.0)
        assert process.poll() is None
    finally:
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=20)
    assert process.returncode == 0
    assert "grove-runner not_configured missing=GROVE_RUNNER_TOKEN" in output
    assert "Traceback" not in output


def test_a_restart_removes_every_pass_folder_a_killed_runner_left(tmp_path):
    """Regression (R4 security review): a kill skips the removal, and a pass folder holds
    OpenCode's session store, so the person's words."""
    home = tmp_path / "home"
    stale = home / "passes" / "01a19fe3-690c-7976-a621-4e49cd31497e"
    (stale / "data" / "opencode").mkdir(parents=True)
    (stale / "events.jsonl").write_text('{"type": "tool_use", "part": {"tool": "bash"}}')
    config = home / ".config" / "opencode"
    (config / "plugin").mkdir(parents=True)
    (config / "plugin" / "planted.ts").write_text("export default {}")
    (config / "opencode.jsonc").write_text("{}")
    prepare_home(home, REPO / "runner")
    assert list((home / "passes").iterdir()) == []
    assert sorted(p.name for p in config.iterdir()) == ["agents", "opencode.json"]


def test_a_pass_that_fails_to_start_still_removes_its_folder(tmp_path):
    settings = LoopSettings(
        api_url="http://127.0.0.1:1",
        runner_token="x" * 40,
        gateway_url="http://127.0.0.1:1/v1",
        home=tmp_path / "home",
        config_dir=REPO / "runner",
        opencode_bin=str(tmp_path / "no-such-opencode"),
        brain_bin=str(BIN / "brain"),
        shell_bin=str(BIN / "grove-brain-shell"),
    )
    prepare_home(settings.home, settings.config_dir)
    grant = {
        "run_id": "01a19fe3-690c-7976-a621-4e49cd31497e",
        "agent": "learner",
        "pass_token": "p1.x",
        "gateway_token": "g1.x",
        "time_cap_seconds": 5,
    }
    with pytest.raises(OSError):
        run_pass(settings, PassRun(grant))
    assert list((settings.home / "passes").iterdir()) == []


def test_only_opencodes_own_error_event_sets_the_budget_outcome(tmp_path):
    """Regression (R4 security review): stored text in a tool output must not set an outcome."""
    events = tmp_path / "events.jsonl"
    tool = {"type": "tool_use", "part": {"tool": "bash", "state": {"output": "budget_reached"}}}
    events.write_text(json.dumps(tool) + "\n")
    assert summarise(events)[2] is False
    error = {
        "type": "error",
        "error": {"name": "APIError", "data": {"responseBody": '{"code": "budget_reached"}'}},
    }
    events.write_text(json.dumps(tool) + "\n" + json.dumps(error) + "\n")
    assert summarise(events)[2] is True
