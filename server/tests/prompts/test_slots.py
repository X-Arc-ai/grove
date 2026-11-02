import json
import os
import shutil
from pathlib import Path

import pytest

from grove.prompts.slots import (
    AGENTS,
    FORMS,
    INSTRUCTIONS,
    OPENCODE,
    SlotFinding,
    check,
    find_slots,
    walk,
)
from tests.prompts.conftest import RULES_BYTES

# One row per slot form of DESIGN.md section 2, plus the other forms OpenCode and a shell fill.
FORM_ROWS = {
    "format-named": ("Summarise {source_text} now.", "brace"),
    "format-positional": ("Summarise {} now.", "brace"),
    "format-indexed": ("Summarise {0} now.", "brace"),
    "format-multi-line": ('Answer with\n{\n  "ops": []\n}\n', "brace"),
    "jinja-statement": ("{% if done %}", "brace"),
    "double-brace": ("Summarise {{source_text}} now.", "double-brace"),
    "dollar-name": ("Summarise $source_text now.", "dollar-name"),
    "dollar-brace": ("Summarise ${source_text} now.", "dollar-brace"),
    "dollar-paren": ("Summarise $(cat notes) now.", "dollar-paren"),
    "printf-s": ("Summarise %s now.", "percent"),
    "printf-d": ("Count %d items.", "percent"),
    "printf-width": ("Pad %-10s here.", "percent"),
    "printf-named": ("Summarise %(source_text)s now.", "percent-name"),
    "tag-open": ('<untrusted_data n="1">', "untrusted-data-tag"),
    "tag-close": ("</untrusted_data>", "untrusted-data-tag"),
    "tag-upper-spaced": ("< UNTRUSTED_DATA>", "untrusted-data-tag"),
    "opencode-file": ("Read {file:./notes.md} first.", "opencode-file"),
    "opencode-env": ("The key is {env:GROVE_GATEWAY_TOKEN}.", "opencode-env"),
    "opencode-arguments": ("Run the pass on $ARGUMENTS.", "opencode-arguments"),
    "opencode-positional": ("Run the pass on $1.", "dollar-digit"),
    "opencode-shell": ("The state is !`git status` now.", "opencode-shell"),
    "opencode-path": ("Review @runner/agents/grove.md now.", "opencode-path"),
    "opencode-path-dot": ("Review @README.md now.", "opencode-path"),
}


def test_every_form_has_a_row():
    assert {form for _, form in FORM_ROWS.values()} == {form for form, _ in FORMS}


@pytest.mark.parametrize(("text", "form"), FORM_ROWS.values(), ids=FORM_ROWS)
def test_the_check_catches_the_form(text, form):
    assert form in {slot.form for slot in find_slots(text)}


CLEAN_ROWS = {
    "the-rules": RULES_BYTES.decode("utf-8"),
    "currency-in-words": "A fee of 5 USD is a said claim, not an order.",
    "percent-in-prose": "Half of them, 50% of the list, are open. 50%.",
    "email": "Orla Vance wrote from orla@brightwell.example about it.",
    "agent-mention": "Hand the pass to @learner.",
    "comparison": "If a < b and c > d, keep both.",
    "escaped-markup": "The block shows &lt; and &amp; as written.",
    "tag-in-words": "Each block starts with an untrusted_data tag.",
    "backticks": "Run `brain status` first.",
}


@pytest.mark.parametrize("text", CLEAN_ROWS.values(), ids=CLEAN_ROWS)
def test_plain_text_has_no_slot(text):
    assert find_slots(text) == []


def test_a_slot_reports_its_line():
    assert [(s.form, s.line) for s in find_slots("one\ntwo\nthree {x}\n")] == [("brace", 3)]


REPO = Path(__file__).resolve().parents[3]
RULES_PATH = f"{INSTRUCTIONS}/grove.data-rules.v1.md"


def test_the_walk_passes_on_the_real_tree():
    assert check(REPO) == []
    walked = {where for where, _ in walk(REPO).texts}
    on_disk = {
        Path(top, name).relative_to(REPO).as_posix()
        for top, _, names in os.walk(REPO / INSTRUCTIONS)
        for name in names
    }
    assert RULES_PATH in on_disk
    assert on_disk <= walked
    agents = REPO / AGENTS
    if agents.is_dir():
        assert {p.relative_to(REPO).as_posix() for p in agents.rglob("*.md")} <= walked


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the instruction folder, which the check must pass before anything is planted."""
    shutil.copytree(REPO / INSTRUCTIONS, tmp_path / INSTRUCTIONS)
    assert check(tmp_path) == []
    return tmp_path


@pytest.mark.parametrize(("text", "form"), FORM_ROWS.values(), ids=FORM_ROWS)
def test_neuter_a_slot_in_a_new_instruction_file_turns_the_walk_red(tree, text, form):
    (tree / INSTRUCTIONS / "grove.fresh.v1.md").write_text(f"Plain first line.\n{text}\n")
    findings = check(tree)
    assert (f"{INSTRUCTIONS}/grove.fresh.v1.md", f"slots/{form}") in {
        (finding.path, finding.rule) for finding in findings
    }
    assert {finding.path for finding in findings} == {f"{INSTRUCTIONS}/grove.fresh.v1.md"}
    first = find_slots(text)[0].offset
    assert min(finding.line for finding in findings) == 2 + text.count("\n", 0, first)


@pytest.mark.parametrize("rel", ["drafts/grove.fresh.v1.md", "notes.txt", ".hidden"])
def test_neuter_every_file_of_the_instruction_folder_is_walked(tree, rel):
    path = tree / INSTRUCTIONS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Summarise {source_text}.\n")
    assert check(tree) == [SlotFinding(f"{INSTRUCTIONS}/{rel}", 1, "slots/brace")]


def test_neuter_a_slot_planted_in_the_rules_turns_the_walk_red(tree):
    (tree / RULES_PATH).write_bytes(RULES_BYTES + b"Then do $ARGUMENTS.\n")
    assert SlotFinding(RULES_PATH, RULES_BYTES.count(b"\n") + 1, "slots/opencode-arguments") in (
        check(tree)
    )


def test_neuter_a_new_agent_file_is_walked(tree):
    (tree / AGENTS).mkdir(parents=True)
    (tree / AGENTS / "learner.md").write_text("---\nmode: subagent\n---\nRead $ARGUMENTS.\n")
    assert check(tree) == [
        SlotFinding(f"{AGENTS}/learner.md", 4, "slots/dollar-name"),
        SlotFinding(f"{AGENTS}/learner.md", 4, "slots/opencode-arguments"),
    ]


def _config(tree: Path, text: str) -> None:
    (tree / OPENCODE).parent.mkdir(parents=True, exist_ok=True)
    (tree / OPENCODE).write_text(text)


def test_neuter_the_prompt_fields_of_the_opencode_config_are_walked(tree):
    config = {
        "provider": {"gateway": {"options": {"apiKey": "{env:GROVE_GATEWAY_TOKEN}"}}},
        "agent": {
            "grove": {"prompt": "Plain words only.", "description": "Runs {env:GROVE_PASS}"},
            "learner": {"prompt": "Use {file:./agents/learner.md}"},
        },
        "command": {"pass": {"template": "Run on $ARGUMENTS"}},
    }
    _config(tree, json.dumps(config, indent=2))
    assert check(tree) == [
        SlotFinding(f"{OPENCODE}#/agent/grove/description", 1, "slots/brace"),
        SlotFinding(f"{OPENCODE}#/agent/grove/description", 1, "slots/opencode-env"),
        SlotFinding(f"{OPENCODE}#/agent/learner/prompt", 1, "slots/brace"),
        SlotFinding(f"{OPENCODE}#/agent/learner/prompt", 1, "slots/opencode-file"),
        SlotFinding(f"{OPENCODE}#/command/pass/template", 1, "slots/dollar-name"),
        SlotFinding(f"{OPENCODE}#/command/pass/template", 1, "slots/opencode-arguments"),
    ]
    walked = {where for where, _ in walk(tree).texts}
    assert f"{OPENCODE}#/agent/grove/prompt" in walked


def test_neuter_a_duplicate_prompt_key_cannot_hide_its_first_value(tree):
    _config(tree, '{"agent": {"grove": {"prompt": "Do {x}", "prompt": "Plain words."}}}')
    assert check(tree) == [SlotFinding(f"{OPENCODE}#/agent/grove/prompt", 1, "slots/brace")]


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('{"agent": {"grove": {"prompt": ["Do {x}"]}}}', "slots/not-text"),
        ('{\n  // a comment\n  "agent": {}\n}', "slots/not-json"),
    ],
    ids=["prompt-not-text", "not-json"],
)
def test_neuter_an_opencode_config_the_walk_cannot_read_is_a_finding(tree, text, rule):
    _config(tree, text)
    assert [finding.rule for finding in check(tree)] == [rule]


def test_neuter_a_missing_instruction_folder_is_a_finding(tmp_path):
    assert check(tmp_path) == [SlotFinding(INSTRUCTIONS, 1, "slots/missing-folder")]


def test_neuter_a_symlink_is_a_finding_and_is_not_followed(tree, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("Summarise {source_text}.\n")
    (tree / INSTRUCTIONS / "grove.linked.v1.md").symlink_to(outside)
    assert check(tree) == [SlotFinding(f"{INSTRUCTIONS}/grove.linked.v1.md", 1, "slots/symlink")]


def test_neuter_a_file_that_is_not_utf8_is_a_finding(tree):
    (tree / INSTRUCTIONS / "grove.latin.v1.md").write_bytes(b"caf\xe9\n")
    assert check(tree) == [SlotFinding(f"{INSTRUCTIONS}/grove.latin.v1.md", 1, "slots/not-utf8")]


def test_a_finding_never_holds_the_text(tree):
    (tree / INSTRUCTIONS / "grove.fresh.v1.md").write_text("Summarise {Orla Vance}.\n")
    report = "\n".join(str(finding) for finding in check(tree))
    assert report == f"{INSTRUCTIONS}/grove.fresh.v1.md:1: slots/brace"
