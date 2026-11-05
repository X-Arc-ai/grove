"""The runner service: `python -m grove.runner` (section 4, "The loop").

It polls the API for due work every few seconds, so a queued `store` starts a Learner pass well
inside 60 s. For each grant it runs OpenCode once, as `opencode run --agent grove`, in its own
process group, under a watchdog of the pass's time cap. Then it ends the pass through the API,
which hands back any request the pass did not settle.

Its one credential is the runner token. OpenCode gets the pass's two tokens and nothing else:
not the runner token, not the caller's environment. A stop signal (the nightly backup stops the
service) ends the running passes as `stopped`, so their requests go back to the queue. A kill
with no time to end them leaves the requests to their leases.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from grove import clock
from grove.instruction_names import is_instruction_name

# The isolation flags (models lane, opencode-capture): with no PROJECT_CONFIG flag, a run outside
# git walks up to a real ~/.opencode; with no CLAUDE_CODE flags it reads Claude Code's files.
OPENCODE_FLAGS = (
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_AUTOUPDATE",
    "OPENCODE_DISABLE_MODELS_FETCH",
    "OPENCODE_DISABLE_SHARE",
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_DISABLE_LSP_DOWNLOAD",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS",
)
STOP_GRACE_SECONDS = 10.0
MAX_SUMMARY = 2_000
_API_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def log(event: str, **fields: Any) -> None:
    """One line per event: ids, codes and counts only, never text."""
    parts = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"grove-runner {event} {parts}".rstrip(), flush=True)


class NotConfigured(Exception):
    """A setting the loop needs is unset. It names the variables, never a value."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = list(names)


@dataclass(frozen=True)
class LoopSettings:
    api_url: str
    runner_token: str = field(repr=False)
    gateway_url: str
    home: Path
    config_dir: Path
    opencode_bin: str
    brain_bin: str
    shell_bin: str
    poll_seconds: float = 5.0
    max_passes: int = 2
    # A pass's folder holds OpenCode's events, and so the source text of its tool outputs. It is
    # removed when the pass ends, unless a lane keeps it for evidence on synthetic data.
    keep_pass_logs: bool = False

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "LoopSettings":
        env = os.environ if env is None else env
        bin_dir = Path(sys.executable).parent
        missing = [
            name
            for name in ("GROVE_API_URL", "GROVE_RUNNER_TOKEN", "GROVE_GATEWAY_URL")
            if not env.get(name)
        ]
        if missing:
            raise NotConfigured(missing)
        return cls(
            api_url=env["GROVE_API_URL"].rstrip("/"),
            runner_token=env["GROVE_RUNNER_TOKEN"],
            gateway_url=env["GROVE_GATEWAY_URL"].rstrip("/"),
            home=Path(env.get("GROVE_RUNNER_HOME") or env.get("HOME") or "/var/lib/grove-runner"),
            config_dir=Path(env.get("GROVE_RUNNER_CONFIG") or "/opt/grove/runner"),
            opencode_bin=env.get("GROVE_OPENCODE_BIN") or "/usr/local/bin/opencode",
            brain_bin=env.get("GROVE_BRAIN_BIN") or str(bin_dir / "brain"),
            shell_bin=env.get("GROVE_BRAIN_SHELL") or str(bin_dir / "grove-brain-shell"),
            poll_seconds=float(env.get("GROVE_RUNNER_POLL_SECONDS") or 5.0),
            max_passes=int(env.get("GROVE_RUNNER_MAX_PASSES") or 2),
            keep_pass_logs=env.get("GROVE_RUNNER_KEEP_PASS_LOGS") == "1",
        )


class RunnerApi:
    """The runner's calls to the API, with the runner token."""

    def __init__(self, settings: LoopSettings, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=f"{settings.api_url}/v1/internal/runner",
            headers={"Authorization": f"Bearer {settings.runner_token}"},
            timeout=_API_TIMEOUT,
            trust_env=False,
        )

    def due(self) -> list[dict[str, str]]:
        response = self._client.get("/due")
        response.raise_for_status()
        return list(response.json()["due"])

    def start(self, world_id: str, agent: str | None = None) -> dict[str, Any] | None:
        body: dict[str, Any] = {"world_id": world_id}
        if agent:
            body["agent"] = agent
        response = self._client.post("/passes", json=body)
        response.raise_for_status()
        return response.json().get("grant")

    def end(self, world_id: str, run_id: str, outcome: str, summary: str | None) -> None:
        body = {"world_id": world_id, "outcome": outcome, "summary": summary}
        response = self._client.post(f"/passes/{run_id}/end", json=body)
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()


def config_home(home: Path) -> Path:
    return home / ".config" / "opencode"


def prepare_home(home: Path, config_dir: Path) -> None:
    """The repo's runner/ config as OpenCode's only config: opencode.json and the four agents.

    OpenCode's config folder is rebuilt from nothing, because OpenCode also reads other files
    and folders there (a second config, plugins, tools, commands), and the folder outlives every
    image on the box. Every pass folder left by a runner that was killed is removed: each holds
    OpenCode's session store, and so the person's words, outside the store and its forget."""
    target = config_home(home)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for folder in ("tmp", ".local/share", ".local/state", ".cache"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    passes = home / "passes"
    if passes.exists():
        shutil.rmtree(passes)
    passes.mkdir()
    shutil.copyfile(config_dir / "opencode.json", target / "opencode.json")
    agents = target / "agents"
    agents.mkdir()
    for path in sorted((config_dir / "agents").glob("*.md")):
        shutil.copyfile(path, agents / path.name)


def opencode_env(
    settings: LoopSettings, grant: dict[str, Any], work: Path | None = None
) -> dict[str, str]:
    """The whole environment of the OpenCode process: nothing of the runner's own leaks in.

    With `work` (the pass's own folder), OpenCode's data, state and temporary folders live in it:
    its session store holds every tool output, so the person's words, and the folder goes when
    the pass ends. No copy of a pass outlives it in the runner's HOME, where no forget reaches."""
    home = settings.home
    data = work / "data" if work is not None else home / ".local" / "share"
    state = work / "state" if work is not None else home / ".local" / "state"
    tmp = work / "tmp" if work is not None else home / "tmp"
    env = {
        "PATH": os.pathsep.join(
            [str(Path(settings.brain_bin).parent), "/usr/local/bin", "/usr/bin", "/bin"]
        ),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SHELL": settings.shell_bin,
        "PYTHONUTF8": "1",
        "GROVE_API_URL": settings.api_url,
        "GROVE_PASS_TOKEN": grant["pass_token"],
        "GROVE_GATEWAY_URL": settings.gateway_url,
        "GROVE_RUN_TOKEN": grant["gateway_token"],
        "GROVE_BRAIN_BIN": settings.brain_bin,
    }
    env.update({flag: "1" for flag in OPENCODE_FLAGS})
    return env


READER = "learner"


def pass_folder(settings: LoopSettings, grant: dict[str, Any], work: Path) -> Path:
    """OpenCode's working folder: for the Learner, the world's added files, once it has any.

    Only the Learner has the read tool (runner/agents/learner.md). Every other pass has no use for
    a person's files, so it runs in `work`, which holds none.

    The API writes that folder (grove.api.agent_files) and it outlives the pass. OpenCode's own
    session store and logs stay in `work`, so the pass's folder still goes when the pass ends.

    OpenCode's read tool follows a link out of its working folder, and the API never makes one,
    so a folder that holds a link is not used: it would reach another world's files. A folder that
    holds an instruction name at any depth is not used either, also when an earlier build let the
    name in: its text would reach the model as instructions (grove.instruction_names)."""
    folder = settings.home / "worlds" / grant["world_id"]
    if (
        grant["agent"] != READER
        or not folder.is_dir()
        or any(path.is_symlink() or is_instruction_name(path.name) for path in folder.rglob("*"))
    ):
        return work
    return folder


def pass_message(grant: dict[str, Any]) -> str:
    """Code writes it: the pass's agent and run id, no stored text."""
    return (
        f"Pass for the agent {grant['agent']}. Run id {grant['run_id']}. "
        f"Give this pass to the {grant['agent']} subagent with the task tool."
    )


def summarise(events_path: Path) -> tuple[str, int, bool]:
    """The last text part, the count of tool calls, and whether the gateway refused at the
    ceiling. Read from OpenCode's JSON events; the outcome never depends on prose."""
    last_text, tools, budget = "", 0, False
    try:
        lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", 0, False
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "tool_use":
            tools += 1
        elif event.get("type") == "text" and isinstance(part.get("text"), str):
            last_text = part["text"]
        # Only OpenCode's own error event carries the gateway's refusal. A tool output can hold
        # any stored text, so it never sets the outcome.
        if event.get("type") == "error" and "budget_reached" in json.dumps(event.get("error")):
            budget = True
    return last_text[:MAX_SUMMARY], tools, budget


@dataclass
class PassRun:
    grant: dict[str, Any]
    process: subprocess.Popen | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    # Set with `stop` when the API asks for the world's interactive lane: a Learner waits.
    preempted: bool = False


def _kill_group(process: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, STOP_GRACE_SECONDS), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_pass(settings: LoopSettings, run: PassRun) -> tuple[str, str]:
    """Runs OpenCode for one grant. Returns (outcome, summary)."""
    grant = run.grant
    work = settings.home / "passes" / grant["run_id"]
    for folder in ("data", "state", "tmp"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    events = work / "events.jsonl"
    try:
        command = [
            settings.opencode_bin,
            "run",
            "--agent",
            "grove",
            "--format",
            "json",
            "--title",
            f"{grant['agent']} pass",
            pass_message(grant),
        ]
        with open(events, "wb") as out, open(work / "opencode.log", "wb") as err:
            run.process = subprocess.Popen(
                command,
                cwd=pass_folder(settings, grant, work),
                env=opencode_env(settings, grant, work),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            started = clock.monotonic()
            outcome = None
            while outcome is None:
                try:
                    code = run.process.wait(timeout=1.0)
                    outcome = "ok" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    if run.stop.is_set():
                        _kill_group(run.process)
                        outcome = "preempted" if run.preempted else "stopped"
                    elif clock.monotonic() - started >= grant["time_cap_seconds"]:
                        _kill_group(run.process)
                        outcome = "timeout"
        text, tools, budget = summarise(events)
        if budget and outcome != "ok":
            outcome = "budget"
        return outcome, f"tool_calls={tools} exit={run.process.returncode}\n{text}".strip()
    finally:
        # Also when the pass raises: the folder holds the person's words.
        if not settings.keep_pass_logs:
            shutil.rmtree(work, ignore_errors=True)


class Runner:
    def __init__(self, settings: LoopSettings, api: RunnerApi | None = None) -> None:
        self.settings = settings
        self.api = api or RunnerApi(settings)
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._runs: dict[str, tuple[PassRun, threading.Thread]] = {}

    def _finish(self, run: PassRun) -> None:
        grant = run.grant
        outcome, summary = "failed", None
        try:
            outcome, summary = run_pass(self.settings, run)
        except Exception as exc:  # the pass must still end, or its run stays live until stale
            log("pass_error", run=grant["run_id"], error=type(exc).__name__)
        try:
            self.api.end(grant["world_id"], grant["run_id"], outcome, summary)
            log("pass_ended", world=grant["world_id"], run=grant["run_id"], outcome=outcome)
        except httpx.HTTPError as exc:
            log("end_failed", run=grant["run_id"], error=type(exc).__name__)
        finally:
            with self._lock:
                self._runs.pop(grant["world_id"], None)

    def running(self) -> int:
        with self._lock:
            return len(self._runs)

    def tick(self) -> int:
        """One poll: start a pass for each due world that has none here. Returns passes started."""
        started = 0
        try:
            due = self.api.due()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log("due_failed", error=type(exc).__name__)
            return 0
        for row in due:
            world_id = row.get("world_id")
            with self._lock:
                held = self._runs.get(world_id)
                if row.get("preempt"):
                    # A request waits behind a maintenance pass of this world: stop that pass.
                    # The Learner starts on a later poll, once the pass has ended.
                    if (
                        held
                        and held[0].grant["run_id"] == row["preempt"]
                        and not held[0].stop.is_set()
                    ):
                        held[0].preempted = True
                        held[0].stop.set()
                        log("pass_preempted", world=world_id, run=row["preempt"])
                    continue
                if held or len(self._runs) >= self.settings.max_passes:
                    continue
            if self.stopping.is_set():
                break
            try:
                grant = self.api.start(world_id, row.get("agent"))
            except httpx.HTTPError as exc:
                log("start_failed", world=world_id, error=type(exc).__name__)
                continue
            if not grant:
                continue
            run = PassRun(grant)
            thread = threading.Thread(target=self._finish, args=(run,), daemon=True)
            with self._lock:
                self._runs[world_id] = (run, thread)
            log("pass_started", world=world_id, run=grant["run_id"], agent=grant["agent"])
            thread.start()
            started += 1
        return started

    def stop(self) -> None:
        self.stopping.set()
        with self._lock:
            runs = list(self._runs.values())
        for run, _ in runs:
            run.stop.set()
        for _, thread in runs:
            thread.join(timeout=STOP_GRACE_SECONDS + 20)

    def serve(self) -> None:
        prepare_home(self.settings.home, self.settings.config_dir)
        log("ready", max_passes=self.settings.max_passes, poll=self.settings.poll_seconds)
        while not self.stopping.is_set():
            self.tick()
            self.stopping.wait(self.settings.poll_seconds)
        self.stop()


def main() -> None:
    """The service. With no runner token yet (the first deploy, before deploy/runner-secrets.sh
    runs), it logs once and waits for its stop signal instead of exiting: a crash loop would fail
    the deploy's `compose up --wait`, and the next deploy brings the token."""
    stopping = threading.Event()
    holder: dict[str, Runner] = {}

    def on_signal(signum: int, _: Any) -> None:
        log("stopping", signal=signum)
        stopping.set()
        if "runner" in holder:
            holder["runner"].stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        settings = LoopSettings.from_environment()
    except NotConfigured as missing:
        log("not_configured", missing=",".join(missing.names))
        stopping.wait()
        return
    runner = Runner(settings)
    holder["runner"] = runner
    if stopping.is_set():
        return
    runner.serve()


if __name__ == "__main__":
    main()
