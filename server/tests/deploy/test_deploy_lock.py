"""The operator's deploy lock: one deploy at a time, freed after its cap if the holder died."""

import os
import signal
import subprocess
import time
from pathlib import Path

from grove import clock
from tests.deploy.conftest import DEPLOY

SCRIPT = DEPLOY / "with-deploy-lock.sh"


def _env(locks: Path, cap: int = 3600) -> dict[str, str]:
    return {**os.environ, "GROVE_LOCK_DIR": str(locks), "GROVE_LOCK_CAP_SECONDS": str(cap)}


def _wait_for(condition, seconds: float) -> bool:
    for _ in range(int(seconds / 0.1)):
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def test_the_lock_is_held_while_the_command_runs_and_freed_after(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    done = subprocess.run(
        ["bash", str(SCRIPT), "bash", "-c", f"test -d {lock} && test -s {lock}/owner && exit 7"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 7, done.stderr
    assert not lock.exists()


def test_a_second_deploy_is_refused_while_the_first_holds_the_lock(tmp_path):
    first = subprocess.Popen(
        ["bash", str(SCRIPT), "sleep", "20"], env=_env(tmp_path), start_new_session=True
    )
    try:
        assert _wait_for(lambda: (tmp_path / "deploy.lock.d" / "owner").exists(), 10)
        second = subprocess.run(
            ["bash", str(SCRIPT), "true"],
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert second.returncode == 75
        assert "the deploy lock is held" in second.stderr
        token = (tmp_path / "deploy.lock.d" / "owner").read_text().split("\n")[0]
        assert token.startswith("token=")
        assert token.removeprefix("token=") not in second.stderr
    finally:
        os.killpg(first.pid, signal.SIGTERM)
        first.wait(timeout=10)
    assert _wait_for(lambda: not (tmp_path / "deploy.lock.d").exists(), 5)


def test_the_releaser_frees_the_lock_of_a_killed_holder_after_the_cap(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    holder = subprocess.Popen(["bash", str(SCRIPT), "sleep", "30"], env=_env(tmp_path, cap=2))
    assert _wait_for(lambda: (lock / "owner").exists(), 10)
    # SIGKILL runs no trap: only the detached releaser can free the lock now.
    holder.kill()
    holder.wait(timeout=10)
    assert lock.exists()
    assert _wait_for(lambda: not lock.exists(), 10)


def test_the_releaser_never_frees_a_lock_that_another_holder_took(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    holder = subprocess.Popen(["bash", str(SCRIPT), "sleep", "30"], env=_env(tmp_path, cap=2))
    assert _wait_for(lambda: (lock / "owner").exists(), 10)
    holder.kill()
    holder.wait(timeout=10)
    # A new holder took the lock in the window before the cap: its token is not the releaser's.
    (lock / "owner").write_text("token=someone-else\n")
    time.sleep(4)
    assert (lock / "owner").read_text() == "token=someone-else\n"


def _owner(lock: Path, since_epoch: int, token: str = "someone-else") -> None:
    lock.mkdir()
    (lock / "owner").write_text(f"token={token}\nholder=lane-x\nsince_epoch={since_epoch}\n")


def test_a_kill_of_the_holder_s_whole_group_still_frees_the_lock_after_the_cap(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    holder = subprocess.Popen(
        ["bash", str(SCRIPT), "sleep", "30"], env=_env(tmp_path, cap=2), start_new_session=True
    )
    assert _wait_for(lambda: (lock / "owner").exists(), 10)
    os.killpg(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    assert lock.exists()
    assert _wait_for(lambda: not lock.exists(), 10)


def test_a_lock_past_its_cap_with_no_releaser_is_taken_over(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    now = int(clock.now().timestamp())
    _owner(lock, now - 7200)
    done = subprocess.run(
        ["bash", str(SCRIPT), "bash", "-c", f"grep -q '^holder=lane-y$' {lock}/owner && exit 9"],
        env={**_env(tmp_path), "GROVE_LOCK_HOLDER": "lane-y"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 9, done.stderr
    assert "took over a deploy lock past its cap" in done.stderr
    assert "someone-else" not in done.stderr
    assert not lock.exists()


def test_a_lock_inside_its_cap_is_never_taken_over(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    _owner(lock, int(clock.now().timestamp()) - 60)
    done = subprocess.run(
        ["bash", str(SCRIPT), "true"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 75
    assert (lock / "owner").read_text().startswith("token=someone-else")


def test_a_normal_release_leaves_no_releaser_running(tmp_path):
    # A cap only this test run uses, so another run on the machine cannot match the process name.
    marker = str(40000 + os.getpid() % 20000)
    done = subprocess.run(
        ["bash", str(SCRIPT), "bash", "-c", f"pgrep -f -- 'sleep {marker}' >/dev/null && exit 8"],
        env=_env(tmp_path, cap=int(marker)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 8, done.stderr
    assert _wait_for(
        lambda: subprocess.run(["pgrep", "-f", "--", f"sleep {marker}"]).returncode == 1, 5
    )


def test_a_lock_folder_with_no_owner_file_and_past_its_cap_is_taken_over(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    lock.mkdir()
    two_hours_ago = clock.now().timestamp() - 7200
    os.utime(lock, (two_hours_ago, two_hours_ago))
    done = subprocess.run(
        ["bash", str(SCRIPT), "true"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert "took over" in done.stderr
    assert not lock.exists()


def test_a_fresh_lock_folder_with_no_owner_file_is_held_and_says_so(tmp_path):
    lock = tmp_path / "deploy.lock.d"
    lock.mkdir()
    done = subprocess.run(
        ["bash", str(SCRIPT), "true"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 75
    assert "no owner file yet" in done.stderr
    assert lock.is_dir()
