"""The workflow, read as text: the project carries no YAML parser and needs none for these."""

import re

from tests.lints.conftest import REPO_ROOT

WORKFLOW = (REPO_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
COMPOSE = (REPO_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")


def _image(text):
    return re.search(r"^\s*image:\s*(\S+)\s*$", text, re.MULTILINE).group(1)


def _run_scripts(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run:(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        script = [match.group(2)]
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            script.append(following)
        yield "\n".join(script)


def test_the_store_image_is_the_compose_image_by_digest():
    assert "@sha256:" in _image(COMPOSE)
    assert _image(WORKFLOW) == _image(COMPOSE)


def test_the_store_memory_settings_match_the_compose_file():
    for name in (
        "NEO4J_server_memory_heap_initial__size",
        "NEO4J_server_memory_heap_max__size",
        "NEO4J_server_memory_pagecache_size",
        "NEO4J_db_query_default__language",
    ):
        pattern = rf"^\s*{name}:\s*(\S+)\s*$"
        wanted = re.search(pattern, COMPOSE, re.MULTILINE).group(1)
        assert re.search(pattern, WORKFLOW, re.MULTILINE).group(1) == wanted


def test_the_check_name_is_exactly_validate():
    assert re.search(r"^name: Validate$", WORKFLOW, re.MULTILINE)
    assert re.search(r"^  validate:\n    name: Validate$", WORKFLOW, re.MULTILINE)


def test_no_script_interpolates_an_expression():
    scripts = list(_run_scripts(WORKFLOW))
    assert len(scripts) >= 9
    assert not [script for script in scripts if "${{" in script]


def test_pr_text_reaches_the_lint_through_env():
    for name, expression in (
        ("PR_TITLE", "github.event.pull_request.title"),
        ("PR_BODY", "github.event.pull_request.body"),
        ("GITHUB_HEAD_REF", "github.head_ref"),
    ):
        assert re.search(rf"^\s*{name}: \$\{{\{{ {re.escape(expression)} \}}\}}$", WORKFLOW, re.M)


def test_every_action_is_pinned_by_a_full_sha():
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW)
    assert len(uses) == 4
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action) for action in uses)


def test_no_secret_is_used_and_the_token_is_read_only():
    assert "secrets." not in WORKFLOW
    assert re.search(r"^permissions:\n  contents: read$", WORKFLOW, re.MULTILINE)


def test_an_edit_of_the_pr_text_runs_the_check_again():
    trigger = (
        r"^on:\n"
        r"  pull_request:\n"
        r"    types: \[opened, synchronize, reopened, edited\]\n"
        r"    branches: \[develop, main\]\n"
        r"  workflow_dispatch:$"
    )
    assert re.search(trigger, WORKFLOW, re.MULTILINE)


def test_a_newer_run_of_the_same_pr_cancels_the_older_one():
    group = re.escape("validate-${{ github.event.pull_request.number || github.ref }}")
    block = rf"^concurrency:\n  group: {group}\n  cancel-in-progress: true$"
    assert re.search(block, WORKFLOW, re.MULTILINE)
