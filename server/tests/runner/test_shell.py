"""grove-brain-shell, the second guard of R10. It is the program OpenCode runs as `$SHELL -c`.

A fake `brain` records its argv and stdin, so each test sees what would have run. Every refused
form also proves its side effect never happened: the canary file is absent.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from grove.runner.shell import REFUSED_EXIT, Refused, parse

SHELL = str(Path(sys.executable).parent / "grove-brain-shell")


@pytest.fixture
def rig(tmp_path):
    log = tmp_path / "brain.log"
    fake = tmp_path / "brain"
    fake.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FAKE_LOG"\n'
        'cat >> "$FAKE_LOG.stdin"\n'
        "echo fake-brain-ran\n"
    )
    fake.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "GROVE_BRAIN_BIN": str(fake), "FAKE_LOG": str(log)}

    def run(command: str):
        done = subprocess.run(
            [SHELL, "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True
        )
        argv = log.read_text().splitlines() if log.exists() else []
        stdin = Path(f"{log}.stdin").read_text() if Path(f"{log}.stdin").exists() else ""
        return done, argv, stdin

    return run, tmp_path


def test_a_plain_brain_call_runs_brain_with_its_words(rig):
    run, _ = rig
    done, argv, _ = run("brain status")
    assert done.returncode == 0 and "fake-brain-ran" in done.stdout
    assert argv == ["status"]


def test_quoted_words_pass_as_they_are_with_no_expansion(rig):
    run, _ = rig
    done, argv, _ = run("brain recall 'garden fair; Elm $HOME `x`'")
    assert done.returncode == 0
    assert argv == ["recall garden fair; Elm $HOME `x`"]


def test_a_heredoc_gives_brain_propose_its_batch_on_stdin(rig):
    run, _ = rig
    batch = {"ops": [{"op": "create_item", "title": "Lina Okafor $not_expanded"}]}
    done, argv, stdin = run("brain propose --request r1 <<'EOF'\n" + json.dumps(batch) + "\nEOF")
    assert done.returncode == 0
    assert argv == ["propose --request r1"]
    assert json.loads(stdin) == batch


REFUSED = [
    pytest.param("brain get x; touch canary", id="chained"),
    pytest.param("brain get x && touch canary", id="and-list"),
    pytest.param("brain get x || touch canary", id="or-list"),
    pytest.param("brain get x | sh", id="piped"),
    pytest.param("brain get $(touch canary)", id="substituted"),
    pytest.param("brain get `touch canary`", id="backtick"),
    pytest.param('brain get "$(touch canary)"', id="substituted-in-double-quotes"),
    pytest.param('brain get "`touch canary`"', id="backtick-in-double-quotes"),
    pytest.param("brain get x > canary", id="redirect-out"),
    pytest.param("brain get x >> canary", id="redirect-append"),
    pytest.param("brain propose < /etc/hosts", id="redirect-in"),
    pytest.param("brain status\ntouch canary", id="second-line"),
    pytest.param("brain get x &", id="background"),
    pytest.param("brain get $HOME", id="variable"),
    pytest.param("brain get ~", id="tilde"),
    pytest.param("brain get *", id="glob"),
    pytest.param("brain get x # comment", id="comment"),
    pytest.param("touch canary", id="another-command"),
    pytest.param("/bin/sh -c 'touch canary'", id="a-shell"),
    pytest.param("brain propose --file /etc/hosts", id="propose-from-a-file"),
    pytest.param("brain propose --file=/etc/hosts", id="propose-from-a-file-equals"),
    pytest.param("brain propose <<'EOF'\n{}\nEOF\ntouch canary", id="text-after-heredoc"),
    pytest.param("brain get x <<'EOF'\n{}\nEOF", id="heredoc-on-another-verb"),
    pytest.param("brain propose <<'EOF'\n{}\n", id="unclosed-heredoc"),
    pytest.param("brain launch x", id="unknown-verb"),
    pytest.param("brain get 'x", id="unclosed-quote"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("command", REFUSED)
def test_every_other_form_is_refused_and_nothing_runs(rig, command):
    run, folder = rig
    done, argv, stdin = run(command)
    assert done.returncode == REFUSED_EXIT
    assert "refused by the runner shell" in done.stderr
    assert argv == [] and stdin == ""
    assert not (folder / "canary").exists()


def test_the_shell_takes_only_dash_c_and_one_command(tmp_path):
    for argv in ([], ["-c"], ["-l", "-c", "brain status"], ["-c", "brain status", "extra"]):
        done = subprocess.run([SHELL, *argv], capture_output=True, text=True, cwd=tmp_path)
        assert done.returncode == REFUSED_EXIT


def test_brain_starts_only_by_a_full_path(tmp_path):
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "GROVE_BRAIN_BIN": "brain"}
    done = subprocess.run([SHELL, "-c", "brain status"], env=env, capture_output=True, text=True)
    assert done.returncode == REFUSED_EXIT and "full path" in done.stderr


def test_parse_keeps_the_words_and_drops_no_quote_rule():
    assert parse("brain get 01a19fe3-44f1-74af-8efc-e3029c5a17b6") == (
        ["brain", "get", "01a19fe3-44f1-74af-8efc-e3029c5a17b6"],
        None,
    )
    with pytest.raises(Refused):
        parse('brain get "a\\b"')
    assert os.path.isabs(SHELL)
