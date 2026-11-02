"""The runner rule, red-tested on planted workflow files in a temp repo."""

import re
import sys

import pytest

from grove.lints._scan import LintError
from grove.lints.runner_rule import check
from tests.lints.conftest import REPO_ROOT, assert_clean, assert_red, run_lint

FALLBACK = "${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}"
FRESH = ".github/workflows/fresh.yml"
PINNED = "actions/checkout@" + "0" * 40
FOREIGN = "someorg/other/.github/workflows/b.yml@v1"


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


CLEAN_JOB = "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"


def plain(jobs: str, *, head: str = "on: push\n") -> str:
    return f"{head}jobs:\n{jobs}"


def test_the_real_workflows_are_clean():
    assert_clean(run_lint("runner", "--root", str(REPO_ROOT)))


def test_validate_runs_on_the_fallback_expression_and_runs_this_lint():
    text = (REPO_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    assert re.findall(r"^\s*runs-on:\s*(.+?)\s*$", text, re.MULTILINE) == [FALLBACK]
    assert re.search(
        r"^      - name: Runner lint\n        run: uv run python -m grove.lints runner$",
        text,
        re.MULTILINE,
    )


@pytest.mark.parametrize(
    "line",
    [
        f"runs-on: {FALLBACK}",
        f'runs-on: "{FALLBACK}"',
        "runs-on: ${{  vars.XARC_CI_RUNNER   ||  'ubuntu-latest'  }}",
        f"runs-on: {FALLBACK}  # the pool while the repo is private",
        "runs-on: ubuntu-latest",
        "runs-on: 'ubuntu-latest'",
        '"runs-on": ubuntu-latest',
    ],
)
def test_the_allowed_values_are_clean(repo, line):
    repo.plant(FRESH, workflow(line))
    assert_clean(repo.lint("runner"))


@pytest.mark.parametrize(
    "line",
    [
        "runs-on: grove-ci",
        "runs-on: grove-ci-scratch",
        "runs-on: self-hosted",
        "runs-on: [self-hosted, linux]",
        "runs-on: [ubuntu-latest]",
        "runs-on: ${{ vars.XARC_CI_RUNNER }}",
        "runs-on: ${{ vars.XARC_CI_RUNNER || 'grove-ci' }}",
        "runs-on: ${{ vars.OTHER_RUNNER || 'ubuntu-latest' }}",
        "runs-on: ${{ matrix.runner }}",
        "runs-on: ubuntu-24.04",
        "runs-on: Ubuntu-Latest",
        "runs-on: >-",
        "runs-on:",
        "RUNS-ON: grove-ci",
        '"runs\\x2don": grove-ci',
        '"runs\\u002don": grove-ci',
        "!!str runs-on: grove-ci",
        "&k runs-on: grove-ci",
        "? runs-on",
        'runs-on: " ubuntu-latest "',
        "runs-on:\n      group: grove",
        "runs-on:\n      - self-hosted\n      - linux",
        "runs-on:   # labels below\n      labels: [grove-ci]",
        "runs-on: ubuntu-latest\n      grove-ci",
    ],
)
def test_each_other_value_in_a_new_file_is_red(repo, line):
    assert_clean(repo.lint("runner"))
    repo.plant(FRESH, workflow(line))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/not-allowed")


@pytest.mark.parametrize(
    "line",
    [
        "runs-on: ubuntu-latest\\u00a0",
        "runs-on: ${{ vars.XARC_CI_RUNNER ||\\u00a0'ubuntu-latest' }}",
    ],
)
def test_a_no_break_space_makes_the_value_differ(repo, line):
    repo.plant(FRESH, workflow(line.encode().decode("unicode_escape")))
    assert_red(repo.lint("runner"), f"{FRESH}:5: runner/not-allowed")


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (plain(f"  release:\n    uses: {FOREIGN}\n"), 4),
        (plain(f"  release:\n    needs: build\n    uses: {FOREIGN}\n"), 5),
        (plain(f"  release:\n    USES: {FOREIGN}\n"), 4),
        (plain(f'  release:\n    "use\\x73": {FOREIGN}\n'), 4),
        (plain(f"  a.b:\n    uses: {FOREIGN}\n"), 4),
        (plain(f"  'a b':\n    env: {{A: b}}\n    uses: {FOREIGN}\n"), 5),
        (plain("  release:\n    uses: [a, b]\n"), 4),
        (plain("  release:\n    uses:\n"), 4),
    ],
)
def test_a_call_to_another_repositorys_workflow_is_red(repo, text, expected):
    repo.plant(FRESH, text)
    assert_red(repo.lint("runner"), f"{FRESH}:{expected}: runner/reusable-call")


@pytest.mark.parametrize(
    "target",
    [
        "./docs/x.yml",
        "./.github/workflows/../../docs/x.yml",
        "./.github/workflows/sub/x.yml",
        "./.github/workflows/x.txt",
        "./.github/actions/x.yml",
    ],
)
def test_a_local_call_outside_the_workflows_dir_is_red(repo, target):
    repo.plant(FRESH, plain(f"  release:\n    uses: {target}\n"))
    assert_red(repo.lint("runner"), f"{FRESH}:4: runner/reusable-call")


def test_a_local_call_is_clean_and_the_called_file_is_checked(repo):
    call = "  release:\n    uses: ./.github/workflows/called.yml\n"
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest", more_jobs=call))
    repo.plant(".github/workflows/called.yml", workflow("runs-on: ubuntu-latest"))
    assert_clean(repo.lint("runner"))
    repo.plant(".github/workflows/called.yml", workflow("runs-on: grove-ci"))
    assert_red(repo.lint("runner"), ".github/workflows/called.yml:5: runner/not-allowed")


# Each spelling names a bad runner or a foreign call through YAML that a line scanner cannot follow.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # An indented root, also after a document mark.
        (f"  on: push\n  jobs:\n    r:\n      uses: {FOREIGN}\n", "4: runner/reusable-call"),
        (f"---\n  on: push\n  jobs:\n    r:\n      uses: {FOREIGN}\n", "5: runner/reusable-call"),
        # Flow style: one line, nested, at column 0, and over several lines.
        (plain("  b: {runs-on: grove-ci, steps: [{run: echo}]}\n"), "3: runner/not-allowed"),
        (plain("  b: {env: {A: b}, runs-on: grove-ci}\n"), "3: runner/not-allowed"),
        (plain('  b: {"runs\\x2don": grove-ci, steps: []}\n'), "3: runner/not-allowed"),
        (plain(f"  r: {{with: {{a: b}}, uses: {FOREIGN}}}\n"), "3: runner/reusable-call"),
        ("on: push\njobs: {b: {runs-on: grove-ci, steps: []}}\n", "2: runner/not-allowed"),
        (plain("  b: {\n    runs-on: grove-ci,\n    steps: [] }\n"), "4: runner/not-allowed"),
        (
            plain(f"  r: {{ needs: b,\n      secrets: inherit,\n    uses: {FOREIGN} }}\n"),
            "5: runner",
        ),
        # Aliases and merge keys: a value, a key, a whole job, all jobs.
        (
            "x-r: &r grove-ci\n" + plain("  b:\n    runs-on: *r\n    steps: []\n"),
            "5: runner/not-allowed",
        ),
        (
            "x-b: &b\n  runs-on: grove-ci\n" + plain("  c:\n    <<: *b\n    steps: []\n"),
            "2: runner/not-allowed",
        ),
        (
            f"x-b: &b\n  uses: {FOREIGN}\n" + plain("  c: *b\n"),
            "2: runner/reusable-call",
        ),
        (
            f"x-b: &b\n  uses: {FOREIGN}\n" + plain("  c: {<<: *b}\n"),
            "2: runner/reusable-call",
        ),
        (
            f"x-j: &j\n  c:\n    uses: {FOREIGN}\non: push\njobs: *j\n",
            "3: runner/reusable-call",
        ),
        (
            "name: &k runs-on\n" + plain("  b: {*k : grove-ci, steps: []}\n"),
            "1: runner/not-allowed",
        ),
        # A quoted scalar over several lines hides nothing from a parser.
        (
            plain(
                '  b:\n    runs-on: ubuntu-latest\n    name: "a\nz: 1"\n'
                f"  r:\n    uses: {FOREIGN}\n"
            ),
            "8: runner/reusable-call",
        ),
        # The key in another case.
        (f"on: push\nJOBS:\n  r:\n    uses: {FOREIGN}\n", "4: runner/reusable-call"),
    ],
)
def test_yaml_that_a_line_scanner_cannot_follow_is_red(repo, text, expected):
    repo.plant(FRESH, text)
    assert_red(repo.lint("runner"), f"{FRESH}:{expected}")


@pytest.mark.parametrize(
    "text",
    [
        "runs-on: 'ubuntu-latest' extra\n",
        "on: push\njobs:\n  b:\n    runs-on: 'ubuntu-latest' extra\n",
        "on: push\njobs:\n  b:\n    <<: *undefined\n",
        "on: push\njobs:\n  b: {runs-on: ubuntu-latest\n",
        "---\non: push\n---\non: pull_request\n",
        "- a\n- b\n",
        "plain text\n",
        "",
        "on: push\njobs: [a]\n",
        "on: push\njobs: none\n",
        "on: push\njobs:\n  b: text\n",
        "on: push\njobs:\n  b:\n    steps: []\n",
        "on: push\njobs:\n  b:\n    ? [a]\n    : c\n    runs-on: ubuntu-latest\n",
        "on: push\n? [a]\n: b\n",
        # A tab before the colon of a key: this parser refuses it, another one reads the key.
        "on: push\njobs:\n  b:\n    runs-on\t: grove-ci\n",
        f"on: push\njobs:\n  b:\n    uses\t: {FOREIGN}\n",
        # Nesting deeper than the parser's recursion.
        "on: push\njobs: " + "[" * 5000 + "\n",
    ],
)
def test_what_the_parser_refuses_or_what_holds_no_job_mapping_is_red(repo, text):
    repo.plant(FRESH, text)
    assert_red(repo.lint("runner"), "runner/unreadable")


@pytest.mark.parametrize("mark", ["\\u2028", "\\u2029", "\\x85", "\\x0b", "\\x0c", "\\x1c"])
def test_a_line_break_that_yaml_versions_read_differently_is_red(repo, mark):
    char = mark.encode().decode("unicode_escape")
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest").replace("echo ok", f"echo{char}ok"))
    assert_red(repo.lint("runner"), f"{FRESH}:12: runner/unreadable")


def test_a_tracked_workflow_that_is_not_on_disk_is_red(repo):
    repo.plant(FRESH, workflow("runs-on: ubuntu-latest"))
    repo.commit("Add a workflow")
    (repo.path / FRESH).unlink()
    assert_red(repo.lint("runner"), f"{FRESH}:1: runner/unreadable")


def test_a_symlinked_workflow_is_red(repo):
    repo.plant("elsewhere/real.yml", workflow("runs-on: ubuntu-latest"))
    link = repo.path / ".github/workflows/linked.yml"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(repo.path / "elsewhere/real.yml")
    assert_red(repo.lint("runner"), ".github/workflows/linked.yml:1: runner/symlink")


@pytest.mark.parametrize("linked", [".github/workflows", ".github"])
def test_a_symlinked_workflows_dir_is_red(repo, linked):
    repo.plant("elsewhere/.github/workflows/fresh.yml", workflow("runs-on: grove-ci"))
    target = repo.path / "elsewhere" / linked
    link = repo.path / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    assert_red(repo.lint("runner"), f"{linked}:1: runner/symlink")


@pytest.mark.parametrize(
    "rel",
    [
        "docs/example.yml",
        ".github/actions/local/action.yml",
        ".github/workflows-old/old.yml",
        ".github/workflows/notes.txt",
    ],
)
def test_files_that_github_does_not_run_as_workflows_are_out_of_scope(repo, rel):
    repo.plant(rel, workflow("runs-on: grove-ci"))
    assert_clean(repo.lint("runner"))


# Valid workflows that name no bad runner. A line scanner flagged or misread each of these.
@pytest.mark.parametrize(
    "text",
    [
        # Block scalars: their text is not structure.
        plain(
            CLEAN_JOB + "  c:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n"
            "          runs-on: grove-ci\n          ? not a key\n      - run: >-\n"
            "          {runs-on: grove-ci}\n"
        ),
        plain("  c:\n    runs-on: ubuntu-latest\n    steps:\n      - |\n          runs-on: x\n"),
        # A value with a brace or a word that looks like a key.
        plain(
            CLEAN_JOB + "  c:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: find . -name x -exec rm {} \\;\n"
            "      - if: ${{ inputs.uses-cache }}\n        run: echo\n"
            "      - uses: a/b@v1\n        with:\n          args: '{\"uses\": 1}'\n"
        ),
        # Steps in the compact sequence style, and steps in flow style.
        plain(
            f"  c:\n    runs-on: ubuntu-latest\n    steps:\n    - uses: {PINNED}\n    - run: echo\n"
        ),
        plain(f"  c:\n    runs-on: ubuntu-latest\n    steps: [{{uses: {PINNED}}}]\n"),
        # A key named uses or runs-on that is not a job's key.
        "on:\n  workflow_call:\n    inputs:\n      runs-on:\n        type: string\n      uses:\n"
        "        type: string\njobs:\n" + CLEAN_JOB,
        "runs-on: grove-ci\nuses: x\non: push\njobs:\n" + CLEAN_JOB,
        # Anchors used for what they are for, and an allowed value through an alias and a merge.
        plain(
            f"  c:\n    runs-on: ubuntu-latest\n    steps:\n      - &co\n        uses: {PINNED}\n"
            "      - *co\n"
        ),
        "x-r: &r ubuntu-latest\non: push\njobs:\n  c:\n    runs-on: *r\n    steps: []\n",
        "x-b: &b\n  runs-on: ubuntu-latest\non: push\njobs:\n  c:\n    <<: *b\n    steps: []\n",
        # A document mark, a byte order mark, a dotted job id, a quoted expression over two lines.
        "---\n" + plain(CLEAN_JOB),
        "\ufeff" + plain(CLEAN_JOB),
        plain("  a.b:\n    runs-on: ubuntu-latest\n    steps: []\n"),
        plain(
            "  c:\n    runs-on: '${{ vars.XARC_CI_RUNNER ||\n      ''ubuntu-latest'' }}'\n"
            "    steps: []\n"
        ),
        # A file with no jobs runs nothing, and a merge key that reaches itself leaves no job.
        "on: push\n",
        "x: &a\n  <<: *a\njobs: *a\n",
    ],
)
def test_a_valid_workflow_with_no_bad_runner_is_clean(repo, text):
    repo.plant(FRESH, text)
    assert_clean(repo.lint("runner"))


def test_a_missing_parser_stops_the_lint_and_is_never_a_pass(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(LintError, match="PyYAML"):
        check(tmp_path)


def test_output_never_holds_the_matched_text(repo):
    repo.plant(FRESH, workflow("runs-on: private-label-of-a-box"))
    result = repo.lint("runner")
    assert_red(result, f"{FRESH}:5: runner/not-allowed")
    assert "private-label" not in result.stdout + result.stderr
    repo.plant(FRESH, "on: push\njobs:\n  b: {runs-on: private-label-of-a-box\n")
    result = repo.lint("runner")
    assert_red(result, "runner/unreadable")
    assert "private-label" not in result.stdout + result.stderr
