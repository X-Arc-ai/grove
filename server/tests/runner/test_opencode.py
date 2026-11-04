"""R10 on the pinned OpenCode binary, against a scripted local model: no model is ever called.

Each case runs OpenCode the way the runner does: the repo's real runner/opencode.json and agent
files (copied by `prepare_home` into an isolated HOME under tmp_path), and the environment that
`opencode_env` builds, so SHELL is the real grove-brain-shell and GROVE_BRAIN_BIN is a fake brain
that logs its argv and stdin. The learner is a subagent, and OpenCode runs a subagent named by
`--agent` as the default agent instead ("is a subagent, not a primary agent. Falling back"), so
each case starts `--agent grove`, and for a learner case the scripted model first hands the pass
to the learner with the task tool, as the runner's own pass does.

`run --format json` prints only the top session's events, so the learner's calls are not on
stdout. Each case reads every call from OpenCode's own record of the run (its SQLite database in
the isolated HOME, read only, after the process ended), checks that the top session's calls there
are the ones on stdout, and checks that what the model got back for each call is what the record
holds. The record's layout is that of the pinned version, which the version check proves.

Two guards stand in the way, and each test names the one it proves:
- OpenCode's matcher (bash `*`: deny, `brain *`: allow) refuses a chain, a pipe, a substitution
  and a second line before any shell runs.
- grove-brain-shell, at SHELL, refuses what the matcher lets through: a redirect and a file option.

Every refusal is proved twice: by what the agent gets back, and by the side effect that did not
happen (no canary file in the work folder, no run of brain, no fetch from the model's server).
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from grove.runner.loop import LoopSettings, opencode_env, prepare_home
from tests.runner.scripted_model import ScriptedModel

pytestmark = pytest.mark.opencode

REPO = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO / "runner"
VERSION = "1.18.32"
DEFAULT_BIN = Path.home() / ".opencode" / "bin" / "opencode"
RUNNER_SHELL = Path(sys.executable).parent / "grove-brain-shell"
RUN_TIMEOUT = 90
INSTALL_HINT = (
    f"OpenCode {VERSION} is needed for the permission tests (R10). Set GROVE_OPENCODE_BIN, or put "
    f"the binary at ~/.opencode/bin/opencode: take opencode-<os>-<arch> of release v{VERSION} "
    "from https://github.com/anomalyco/opencode/releases (CI: the step 'Install OpenCode for "
    "the permission tests' in .github/workflows/validate.yml)."
)

MATCHER_REFUSAL = (
    "The user has specified a rule which prevents you from using this specific tool call"
)
SHELL_REFUSAL = "refused by the runner shell"
SHELL_REFUSED_EXIT = 126
UNAVAILABLE = "unavailable tool"
FAKE_LOG_VAR = "FAKE_BRAIN_LOG"
# The one line the fake brain prints. Piped into sh, it would leave the canary `piped`.
FAKE_LINE = "touch piped"
FAKE_BRAIN = """#!{python} -I
import json, os, sys
data = sys.stdin.read()
with open(os.environ["{var}"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": data}}) + "\\n")
print("{line}")
"""


def bash(command: str) -> dict[str, Any]:
    return {"tool": "bash", "args": {"command": command, "description": "probe"}}


def hand_to(agent: str) -> dict[str, Any]:
    """Grove's step: the whole pass to the subagent, as grove.md tells it to."""
    prompt = f"Run id run-test-1. Start with brain pass. This is the {agent} pass."
    return {
        "tool": "task",
        "args": {"description": f"{agent} pass", "prompt": prompt, "subagent_type": agent},
    }


@dataclass(frozen=True)
class ToolUse:
    agent: str
    tool: str
    call_id: str
    input: dict[str, Any]
    status: str
    output: str
    error: str
    exit: int | None


@dataclass(frozen=True)
class Run:
    code: int
    tools: list[ToolUse]
    brain_calls: list[dict[str, Any]]
    files: list[str]
    offers: list[tuple[str, list[str]]]
    tool_results: dict[str, str]
    gets: list[str]
    stderr: str


def _settings(home: Path, gateway_url: str, opencode_bin: str, brain: Path, shell: str):
    return LoopSettings(
        api_url="http://127.0.0.1:1",
        runner_token="dummy-runner-token",
        gateway_url=gateway_url,
        home=home,
        config_dir=CONFIG_DIR,
        opencode_bin=opencode_bin,
        brain_bin=str(brain),
        shell_bin=shell,
    )


def _grant(agent: str) -> dict[str, Any]:
    return {
        "pass_token": "p1.test",
        "gateway_token": "g1.test",
        "agent": agent,
        "run_id": "run-test-1",
        "world_id": "world-test",
        "time_cap_seconds": RUN_TIMEOUT,
    }


def _spawn(command: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
    """OpenCode in its own process group, under a hard timeout; nothing of it outlives the call."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate()
        pytest.fail(f"OpenCode ran over {RUN_TIMEOUT} s:\n{err[-3000:]}")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return process.returncode, out, err


def _stdout_tool_uses(stdout: str) -> list[tuple[str, str, str]]:
    """(tool, call id, status) of each tool_use event that `run --format json` printed."""
    uses = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "tool_use":
            continue
        part = event.get("part") or {}
        state = part.get("state") or {}
        uses.append((str(part.get("tool")), str(part.get("callID")), str(state.get("status"))))
    return uses


def _recorded_tool_uses(env: dict[str, str]) -> list[ToolUse]:
    """Every tool call of the run, the subagent's too, from OpenCode's own record, in order."""
    db = Path(env["XDG_DATA_HOME"]) / "opencode" / "opencode.db"
    assert db.is_file(), f"OpenCode kept no record at {db}"
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT part.data, message.data FROM part JOIN message ON message.id = part.message_id"
            " ORDER BY part.time_created, part.id"
        ).fetchall()
    uses = []
    for part_data, message_data in rows:
        part = json.loads(part_data)
        if part.get("type") != "tool":
            continue
        state = part.get("state") or {}
        metadata = state.get("metadata") or {}
        uses.append(
            ToolUse(
                agent=str(json.loads(message_data).get("agent")),
                tool=str(part.get("tool")),
                call_id=str(part.get("callID")),
                input=state.get("input") or {},
                status=str(state.get("status")),
                output=str(state.get("output") or ""),
                error=str(state.get("error") or ""),
                exit=metadata.get("exit"),
            )
        )
    return uses


@pytest.fixture(scope="module")
def opencode_bin(tmp_path_factory) -> str:
    explicit = os.environ.get("GROVE_OPENCODE_BIN")
    path = Path(explicit) if explicit else DEFAULT_BIN
    if not path.is_file() or not os.access(path, os.X_OK):
        pytest.fail(f"no OpenCode binary at {path}. {INSTALL_HINT}")
    if not RUNNER_SHELL.is_file():
        pytest.fail(f"no grove-brain-shell at {RUNNER_SHELL}: run 'uv sync' in server/.")
    home = tmp_path_factory.mktemp("version-home")
    prepare_home(home, CONFIG_DIR)
    settings = _settings(home, "http://127.0.0.1:1/v1", str(path), home / "brain", "/bin/sh")
    code, out, err = _spawn([str(path), "--version"], home, opencode_env(settings, _grant("grove")))
    if code != 0 or out.strip() != VERSION:
        pytest.fail(f"{path} --version printed {out.strip()!r} (exit {code}), not {VERSION}.")
    return str(path)


@pytest.fixture
def model():
    with ScriptedModel() as scripted:
        yield scripted


class Rig:
    """One isolated HOME, one empty work folder and one fake brain, for one OpenCode run."""

    def __init__(self, root: Path, model: ScriptedModel, opencode_bin: str) -> None:
        self.model = model
        self.opencode_bin = opencode_bin
        self.home = root / "home"
        self.cwd = root / "work"
        self.cwd.mkdir()
        self.log = root / "brain.log"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.brain = bin_dir / "brain"
        self.brain.write_text(
            FAKE_BRAIN.format(python=sys.executable, var=FAKE_LOG_VAR, line=FAKE_LINE),
            encoding="utf-8",
        )
        self.brain.chmod(0o755)
        self.ran = False

    def run(
        self,
        steps: list[dict[str, Any]],
        *,
        agent: str = "learner",
        shell: str | Path = RUNNER_SHELL,
    ) -> Run:
        assert not self.ran, "one OpenCode run per test"
        self.ran = True
        prepare_home(self.home, CONFIG_DIR)
        settings = _settings(
            self.home, self.model.gateway_url, self.opencode_bin, self.brain, str(shell)
        )
        env = opencode_env(settings, _grant(agent))
        env[FAKE_LOG_VAR] = str(self.log)
        script = steps if agent == "grove" else [hand_to(agent), *steps]
        agents = ["grove"] * len(script) if agent == "grove" else ["grove"] + [agent] * len(steps)
        self.model.script(script)
        command = [self.opencode_bin, "run", "--agent", "grove", "--format", "json", "probe"]
        code, out, err = _spawn(command, self.cwd, env)
        calls = []
        if self.log.exists():
            calls = [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]
        run = Run(
            code=code,
            tools=_recorded_tool_uses(env),
            brain_calls=calls,
            files=sorted(path.name for path in self.cwd.iterdir()),
            offers=list(self.model.offers),
            tool_results=dict(self.model.tool_results),
            gets=list(self.model.gets),
            stderr=err[-3000:],
        )
        # No vacuous pass: OpenCode ran, asked the model for every step, and each agent made
        # its calls in order.
        assert run.code == 0, run.stderr
        assert self.model.steps_taken == len(script), run
        wanted = list(zip(agents, [step["tool"] for step in script], strict=True))
        assert [(use.agent, use.tool) for use in run.tools] == wanted, run
        # The record agrees with stdout (the top session's events) and with what the model got.
        top = [(use.tool, use.call_id, use.status) for use in run.tools if use.agent == "grove"]
        assert _stdout_tool_uses(out) == top, out[-3000:]
        for use in run.tools:
            got = use.output if use.status == "completed" else use.error
            assert run.tool_results.get(use.call_id) == got, (use, run.tool_results)
        return run


@pytest.fixture
def rig(tmp_path, model, opencode_bin) -> Rig:
    return Rig(tmp_path, model, opencode_bin)


def _learner_calls(run: Run) -> list[ToolUse]:
    """The learner's calls (the first tool use is grove's task call, which completed)."""
    assert run.tools[0].tool == "task" and run.tools[0].status == "completed", run
    return run.tools[1:]


def _only(run: Run) -> ToolUse:
    calls = _learner_calls(run)
    assert len(calls) == 1, run
    return calls[0]


def test_a_plain_brain_call_runs_through_both_guards(rig):
    run = rig.run([bash("brain status")])
    use = _only(run)
    assert use.status == "completed" and use.exit == 0, use
    assert use.output == FAKE_LINE + "\n"
    assert run.brain_calls == [{"argv": ["status"], "stdin": ""}]
    assert run.files == []


def test_a_propose_heredoc_gives_brain_the_batch_on_stdin(rig):
    batch = json.dumps({"ops": [{"op": "create_item", "kind": "event", "title": "Garden fair"}]})
    run = rig.run([bash(f"brain propose --request r1 <<'EOF'\n{batch}\nEOF")])
    use = _only(run)
    assert use.status == "completed" and use.exit == 0, use
    assert run.brain_calls == [{"argv": ["propose", "--request", "r1"], "stdin": batch + "\n"}]
    assert run.files == []


@pytest.mark.parametrize(
    ("command", "canary"),
    [
        pytest.param("brain get x; touch chained", "chained", id="chained"),
        pytest.param("brain get x && touch anded", "anded", id="and-list"),
        pytest.param("brain get x | sh", "piped", id="piped"),
        pytest.param("brain get $(touch subst)", "subst", id="substituted"),
        pytest.param("brain get `touch backtick`", "backtick", id="backtick"),
        pytest.param("brain status\ntouch newline", "newline", id="second-line"),
    ],
)
def test_opencode_refuses_a_second_command_before_any_shell_runs(rig, command, canary):
    run = rig.run([bash(command)])
    use = _only(run)
    assert use.status == "error", use
    assert MATCHER_REFUSAL in use.error
    assert use.output == "" and use.exit is None, use
    assert not (rig.cwd / canary).exists()
    assert run.files == []
    assert run.brain_calls == []


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        pytest.param("brain get x > redirect", "the shell character '>'", id="redirect-out"),
        pytest.param("brain propose < /etc/hosts", "the shell character '<'", id="redirect-in"),
        pytest.param(
            "brain propose --file /etc/hosts", "not from a file", id="propose-from-a-file"
        ),
    ],
)
def test_the_runner_shell_refuses_what_the_opencode_matcher_lets_through(rig, command, reason):
    run = rig.run([bash(command)])
    use = _only(run)
    # OpenCode's matcher let it through (the call ran and completed); the shell refused it.
    assert use.status == "completed", use
    assert MATCHER_REFUSAL not in use.output
    assert SHELL_REFUSAL in use.output and reason in use.output, use
    assert use.exit == SHELL_REFUSED_EXIT, use
    assert run.brain_calls == []
    assert run.files == []


def test_with_sh_at_shell_the_redirect_writes_its_file_so_the_runner_shell_is_the_guard(rig):
    """Characterization: OpenCode's matcher alone lets `brain get x > redirect` write a file."""
    run = rig.run([bash("brain get x > redirect")], shell="/bin/sh")
    use = _only(run)
    assert use.status == "completed" and use.exit == 0, use
    assert SHELL_REFUSAL not in use.output
    assert run.files == ["redirect"]
    assert (rig.cwd / "redirect").read_text("utf-8") == FAKE_LINE + "\n"
    assert run.brain_calls == [{"argv": ["get", "x"], "stdin": ""}]


def _hosts_lines() -> list[str]:
    text = Path("/etc/hosts").read_text("utf-8", errors="replace")
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_write_edit_and_webfetch_are_unavailable_and_leave_nothing_behind(rig, model):
    canary_url = f"{model.base_url}/webfetch-canary"
    steps = [
        {"tool": "write", "args": {"filePath": str(rig.cwd / "write-canary"), "content": "x"}},
        {
            "tool": "edit",
            "args": {"filePath": str(rig.cwd / "edit-canary"), "oldString": "", "newString": "x"},
        },
        {"tool": "webfetch", "args": {"url": canary_url, "format": "text"}},
    ]
    run = rig.run(steps)
    calls = _learner_calls(run)
    assert [use.tool for use in calls] == ["write", "edit", "webfetch"]
    for use in calls:
        assert use.status == "error", use
        assert UNAVAILABLE in use.error
        assert use.output == "" and use.exit is None, use
    assert run.files == []
    assert "/webfetch-canary" not in run.gets
    got = "\n".join(run.tool_results.values())
    assert not [line for line in _hosts_lines() if line in got]
    assert run.brain_calls == []


def test_the_learner_reads_its_folder_and_nothing_outside_it(rig):
    """OpenCode follows a link, so grove.runner.loop.pass_folder refuses a folder that holds one."""
    (rig.cwd / "plan.txt").write_text("added file text", encoding="utf-8")
    other = rig.cwd.parent / "other-world"
    other.mkdir()
    (other / "secret.txt").write_text("another world's words", encoding="utf-8")
    run = rig.run(
        [
            {"tool": "read", "args": {"filePath": str(rig.cwd / "plan.txt")}},
            {"tool": "read", "args": {"filePath": str(other / "secret.txt")}},
            {"tool": "read", "args": {"filePath": "../other-world/secret.txt"}},
            {"tool": "read", "args": {"filePath": "/etc/hosts"}},
        ]
    )
    inside, *outside = _learner_calls(run)
    assert inside.status == "completed" and "added file text" in inside.output, inside
    for use in outside:
        assert use.status == "error" and use.output == "", use
    got = "\n".join(run.tool_results.values())
    assert "another world" not in got
    assert not [line for line in _hosts_lines() if line in got]


def test_the_learner_is_offered_only_bash_and_read(rig):
    run = rig.run([])
    learner = [names for agent, names in run.offers if agent == "learner"]
    grove = [names for agent, names in run.offers if agent == "grove"]
    assert learner and all(names == ["bash", "read"] for names in learner), run.offers
    assert grove and all(names == ["bash", "task"] for names in grove), run.offers
    assert {agent for agent, _ in run.offers} == {"grove", "learner"}, run.offers


def test_grove_is_offered_only_bash_and_task(rig):
    run = rig.run([], agent="grove")
    assert run.offers and all(offer == ("grove", ["bash", "task"]) for offer in run.offers)
