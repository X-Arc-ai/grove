"""runner/: OpenCode's only config. The gateway is its only provider, bash runs `brain *` only,
every other tool is denied for all four agents, and the agents carry the data rules."""

import json
from pathlib import Path

import yaml

from grove.runner.loop import prepare_home

REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "runner"
AGENTS = ("grove", "learner", "curator", "connector")
BASH = {"*": "deny", "brain *": "allow"}


def config() -> dict:
    return json.loads((RUNNER / "opencode.json").read_text())


def agent(name: str) -> tuple[dict, str]:
    text = (RUNNER / "agents" / f"{name}.md").read_text()
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def test_the_gateway_is_the_only_provider():
    doc = config()
    assert set(doc["provider"]) == {"grove"}
    grove = doc["provider"]["grove"]
    assert grove["npm"] == "@ai-sdk/openai-compatible"
    assert grove["options"] == {
        "baseURL": "{env:GROVE_GATEWAY_URL}",
        "apiKey": "{env:GROVE_RUN_TOKEN}",
    }
    assert set(grove["models"]) == {"muse-spark-1.3"}
    assert doc["model"] == doc["small_model"] == "grove/muse-spark-1.3"
    assert doc["plugin"] == [] and doc["mcp"] == {} and doc["instructions"] == []
    assert doc["autoupdate"] is False and doc["share"] == "disabled"


def test_bash_runs_brain_only_and_every_other_tool_is_denied():
    permission = config()["permission"]
    assert list(permission)[0] == "*" and permission["*"] == "deny"
    assert permission["bash"] == BASH
    assert permission["edit"] == "deny" and permission["webfetch"] == "deny"
    assert set(permission) == {"*", "bash", "edit", "webfetch"}


def test_each_agent_pins_its_model_and_denies_every_tool_but_brain():
    for name in AGENTS:
        front, _ = agent(name)
        assert front["model"] == "grove/muse-spark-1.3", name
        rules = front["permission"]
        assert list(rules)[0] == "*" and rules["*"] == "deny", name
        assert rules["bash"] == BASH, name
        assert set(rules) <= {"*", "bash", "task"}, name


def test_grove_hands_a_pass_only_to_the_three_subagents():
    front, _ = agent("grove")
    assert front["mode"] == "primary"
    assert front["permission"]["task"] == {
        "*": "deny",
        "learner": "allow",
        "curator": "allow",
        "connector": "allow",
    }
    for name in ("learner", "curator", "connector"):
        sub, _ = agent(name)
        assert sub["mode"] == "subagent" and sub["permission"]["task"] == "deny", name


def test_every_agent_carries_the_data_rules():
    rules = (REPO / "server/grove/prompts/instructions/grove.data-rules.v1.md").read_text()
    paragraphs = [p for p in rules.split("\n\n")[1:] if not p.startswith("Your only output")]
    for name in AGENTS:
        _, body = agent(name)
        for paragraph in paragraphs:
            assert paragraph.strip() in body, (name, paragraph[:40])


def test_prepare_home_installs_exactly_the_repo_config(tmp_path):
    home = tmp_path / "home"
    stray = home / ".config" / "opencode" / "agent"
    stray.mkdir(parents=True)
    (stray / "planted.md").write_text("---\nmode: primary\n---\nplanted\n")
    old = home / ".config" / "opencode" / "agents"
    old.mkdir()
    (old / "gone.md").write_text("old")
    prepare_home(home, RUNNER)
    target = home / ".config" / "opencode"
    assert json.loads((target / "opencode.json").read_text()) == config()
    assert sorted(p.name for p in (target / "agents").iterdir()) == sorted(
        f"{a}.md" for a in AGENTS
    )
    assert not (target / "agent").exists()
