import pytest

from tests.lints.conftest import REPO_ROOT, assert_clean, run_lint


@pytest.mark.parametrize("lint", ["no-names", "clock", "writer", "runner", "schema"])
def test_each_lint_passes_on_the_real_repo(lint):
    assert_clean(run_lint(lint, "--root", str(REPO_ROOT)))


@pytest.mark.parametrize("lint", ["no-names", "clock", "writer", "runner", "schema"])
def test_the_root_defaults_to_the_repo_around_the_folder(lint):
    assert_clean(run_lint(lint))
