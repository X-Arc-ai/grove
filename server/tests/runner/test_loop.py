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
from grove.runner.loop import OPENCODE_FLAGS, LoopSettings, Runner, prepare_home
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


def test_a_queued_store_starts_a_learner_pass_within_60_seconds(driver, rig):
    world_id, app, loop_settings, record, _, _ = rig
    with Served(app()) as served:
        runner = Runner(loop_settings(served.url, "propose", poll_seconds=5.0))
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
