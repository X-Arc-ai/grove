"""The runner rule, red-tested on planted workflow files in a temp repo."""

import re

import pytest

from tests.lints.conftest import REPO_ROOT, assert_clean, assert_red

FALLBACK = "${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}"
FRESH = ".github/workflows/fresh.yml"
PINNED = "actions/checkout@" + "0" * 40


def workflow(runs_on: str, *, more_jobs: str = "") -> str:
    """A small workflow whose `runs-on` line is line 5."""
    return (
        "name: Fresh\n"
        "on: pull_request\n"
        "jobs:\n"
        "  build:\n"
        f"    {runs_on}\n"
        "    steps:\n"
        f"      - uses: {PINNED}\n"
        "      - name: Run\n"
        "        uses: ./.github/actions/local\n"
        "        with:\n"
        "          uses: not-a-job-key\n"
        "      - run: echo ok\n"
        f"{more_jobs}"
    )


ALLOWED = [
    f"runs-on: {FALLBACK}",
    f'runs-on: "{FALLBACK}"',
    "runs-on: ${{  vars.XARC_CI_RUNNER   ||  'ubuntu-latest'  }}",
    f"runs-on: {FALLBACK}  # the pool while the repo is private",
    "runs-on: ubuntu-latest",
    "runs-on: 'ubuntu-latest'",
    '"runs-on": ubuntu-latest',
]

NOT_ALLOWED = [
    "runs-on: grove-ci",
    "runs-on: grove-ci-scratch",
    "runs-on: self-hosted",
    "runs-on: [self-hosted, linux]",
    "runs-on: ${{ vars.XARC_CI_RUNNER }}",
    "runs-on: ${{ vars.XARC_CI_RUNNER || 'grove-ci' }}",
    "runs-on: ${{ vars.OTHER_RUNNER || 'ubuntu-latest' }}",
    "runs-on: ${{ matrix.runner }}",
    "runs-on: ubuntu-24.04",
    "runs-on: Ubuntu-Latest",
    "runs-on: >-",
    "runs-on: 'ubuntu-latest' extra",
]

BLOCK_FORMS = [
    "runs-on:\n      group: grove",
    "runs-on:\n      - self-hosted\n      - linux",
    "runs-on:   # labels below\n      labels: [grove-ci]",
]


def test_the_real_workflows_are_clean():
    from tests.lints.conftest import run_lint

    assert_clean(run_lint("runner", "--root", str(REPO_ROOT)))


def test_validate_runs_on_the_fallback_expression_and_runs_this_lint():
    text = (REPO_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    assert re.findall(r"^\s*runs-on:\s*(.+?)\s*$", text, re.MULTILINE) == [FALLBACK]
    assert re.search(
        r"^      - name: Runner lint\n        run: uv run python -m grove.lints runner$",
        text,
        re.MULTILINE,
    )


@pytest.mark.parametrize("line", ALLOWED)
def test_the_allowed_values_are_clean(repo, line):
    repo.plant(FRESH, workflow(line))
    assert_clean(repo.lint("runner"))


@pytest.mark.parametrize("line", NOT_ALLOWED)
def test_each_other_value_in_a_new_file_is_red(repo, line):
    assert_clean(repo.lint("runner"))
    repo.plant(FRESH, workflow(line))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/not-allowed")


@pytest.mark.parametrize("lines", BLOCK_FORMS)
def test_a_block_form_is_red(repo, lines):
    repo.plant(FRESH, workflow(lines))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/block-form")


@pytest.mark.parametrize(
    "rel",
    [
        ".github/workflows/fresh.yaml",
        ".github/workflows/Fresh.YML",
        ".github/workflows/nested/fresh.yml",
    ],
)
def test_every_workflow_file_name_is_read(repo, rel):
    repo.plant(rel, workflow("runs-on: grove-ci"))
    assert_red(repo.lint("runner"), f"{rel}:5: runner/not-allowed")


def test_a_committed_file_is_read_as_well_as_a_new_one(repo):
    repo.plant(FRESH, workflow("runs-on: grove-ci"))
    repo.commit("Add a workflow")
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/not-allowed")


def test_a_second_job_is_checked(repo):
    second = "  deploy:\n    runs-on: grove-ci\n    steps:\n      - run: echo ok\n"
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=second))
    result = repo.lint("runner")
    assert_red(result, f"{FRESH}:14: runner/not-allowed")
    assert result.stdout.count("\n") == 1


def test_a_call_to_another_repositorys_workflow_is_red(repo):
    call = "  release:\n    uses: someorg/other/.github/workflows/build.yml@main\n"
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=call))
    assert_red(repo.lint("runner"), f"{FRESH}:14: runner/reusable-call")


def test_a_call_as_the_second_key_of_a_job_is_red(repo):
    call = "  release:\n    needs: build\n    uses: someorg/other/.github/workflows/b.yml@v1\n"
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=call))
    assert_red(repo.lint("runner"), f"{FRESH}:15: runner/reusable-call")


def test_a_local_call_is_clean_and_the_called_file_is_checked(repo):
    call = "  release:\n    uses: ./.github/workflows/called.yml\n"
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=call))
    repo.plant(".github/workflows/called.yml", workflow("runs-on: ubuntu-latest"))
    assert_clean(repo.lint("runner"))
    repo.plant(".github/workflows/called.yml", workflow("runs-on: grove-ci"))
    assert_red(repo.lint("runner"), ".github/workflows/called.yml:5: runner/not-allowed")


def test_a_symlinked_workflow_is_red(repo):
    repo.plant("elsewhere/real.yml", workflow("runs-on: ubuntu-latest"))
    link = repo.path / ".github/workflows/linked.yml"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(repo.path / "elsewhere/real.yml")
    assert_red(repo.lint("runner"), ".github/workflows/linked.yml:1: runner/symlink")


@pytest.mark.parametrize(
    "rel",
    ["docs/example.yml", ".github/actions/local/action.yml", ".github/workflows-old/old.yml"],
)
def test_files_that_github_does_not_run_as_workflows_are_out_of_scope(repo, rel):
    repo.plant(rel, workflow("runs-on: grove-ci"))
    assert_clean(repo.lint("runner"))


UNREADABLE = [
    '"runs\\x2don": grove-ci',
    '"runs\\u002don": grove-ci',
    "? runs-on",
    "!!str runs-on: grove-ci",
    "&k runs-on: grove-ci",
    "<<: *base",
    '"use\\x73": someorg/other/.github/workflows/b.yml@v1',
    "{runs-on: grove-ci}",
]


@pytest.mark.parametrize("line", UNREADABLE)
def test_a_key_the_scanner_cannot_read_is_red(repo, line):
    repo.plant(FRESH, workflow(line))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/unreadable")


def test_a_flow_mapping_job_is_red(repo):
    text = "on: push\njobs:\n  build: {runs-on: grove-ci, steps: [{run: echo}]}\n"
    repo.plant(FRESH, text)
    assert_red(repo.lint("runner"), f"{FRESH}:3: runner/unreadable")


def test_a_column_0_line_that_is_not_a_key_is_red(repo):
    text = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        '    name: "a\n'
        'b"\n'
        "  release:\n"
        "    uses: someorg/other/.github/workflows/b.yml@v1\n"
    )
    repo.plant(FRESH, text)
    assert_red(repo.lint("runner"), f"{FRESH}:6: runner/unreadable")


def test_a_document_start_mark_is_clean(repo):
    repo.plant(FRESH, "---\n" + workflow("runs-on: ubuntu-latest"))
    assert_clean(repo.lint("runner"))


def test_a_plain_scalar_that_goes_on_in_the_next_line_is_red(repo):
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest\n      grove-ci"))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/block-form")


@pytest.mark.parametrize(
    "line",
    ["runs-on: ubuntu-latest ", "runs-on: ${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}"],
)
def test_a_no_break_space_makes_the_value_differ(repo, line):
    repo.plant(FRESH, workflow(line))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/not-allowed")


def test_the_text_of_a_block_scalar_is_not_read(repo):
    script = (
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          runs-on: grove-ci\n"
        "          uses: someorg/other/.github/workflows/b.yml@v1\n"
        "          ? not a key\n"
        "      - run: >-\n"
        "          {runs-on: grove-ci}\n"
    )
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=script))
    assert_clean(repo.lint("runner"))


def test_a_line_after_a_block_scalar_is_read_again(repo):
    script = (
        "  check:\n"
        "    env: |\n"
        "      text\n"
        "    runs-on: grove-ci\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=script))
    assert_red(repo.lint("runner"), f"{FRESH}:16: runner/not-allowed")


@pytest.mark.parametrize("linked", [".github/workflows", ".github"])
def test_a_symlinked_workflows_dir_is_red(repo, linked):
    repo.plant("elsewhere/.github/workflows/fresh.yml", workflow("runs-on: grove-ci"))
    target = repo.path / "elsewhere" / linked
    link = repo.path / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    assert_red(repo.lint("runner"), f"{linked}:1: runner/symlink")


def test_steps_in_the_compact_sequence_style_are_clean(repo):
    text = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"    - uses: {PINNED}\n"
        "    - run: echo ok\n"
    )
    repo.plant(FRESH, text)
    assert_clean(repo.lint("runner"))


def test_a_uses_key_outside_jobs_is_clean(repo):
    text = (
        "on:\n"
        "  workflow_dispatch:\n"
        "    uses: not-a-job\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    repo.plant(FRESH, text)
    assert_clean(repo.lint("runner"))


def test_output_never_holds_the_matched_text(repo):
    repo.plant(FRESH, workflow("runs-on: private-label-of-a-box"))
    result = repo.lint("runner")
    assert_red(result, f"{FRESH}:5: runner/not-allowed")
    assert "private-label" not in result.stdout + result.stderr
