import pytest

from tests.lints.conftest import (
    ACCOUNT,
    BANNED_WORDS,
    GITHUB,
    GITHUB_MERGE_AUTHOR,
    UTF8_LOCALE,
    assert_clean,
    assert_red,
    run_lint,
)

WORD = BANNED_WORDS[0]
ALL_FORMS = [form for word in BANNED_WORDS for form in (word, word.upper(), word.title())]


@pytest.mark.parametrize("word", ALL_FORMS)
def test_word_in_a_new_file(repo, word):
    assert_clean(repo.lint("no-names"))
    repo.plant("docs/fresh.md", f"first line\nwritten by {word} here\n")
    assert_red(repo.lint("no-names"), "docs/fresh.md:2: no-names/content")


def test_word_in_a_committed_file(repo):
    repo.plant("docs/fresh.md", "clean\n")
    repo.commit("Add a file")
    assert_clean(repo.lint("no-names"))
    repo.plant("docs/fresh.md", f"{WORD}\n")
    repo.commit("Change the file")
    assert_red(repo.lint("no-names"), "docs/fresh.md:1: no-names/content")


def test_word_in_a_binary_file(repo):
    assert_clean(repo.lint("no-names"))
    repo.plant("blob.bin", b"\x00\x01\x02" + WORD.encode() + b"\x00\n")
    assert_red(repo.lint("no-names"), "blob.bin:1: no-names/content")


def test_word_on_a_latin_1_line_in_a_utf_8_locale(repo):
    assert_clean(repo.lint("no-names", env=UTF8_LOCALE))
    repo.plant("kept.txt", b"caf\xe9 " + WORD.encode() + b"\n")
    assert_red(repo.lint("no-names", env=UTF8_LOCALE), "kept.txt:1: no-names/content")


def test_word_among_high_bytes_in_a_binary_file_in_a_utf_8_locale(repo):
    high = bytes(range(0x80, 0x100))
    repo.plant("blob.bin", high + WORD.encode() + high + b"\n")
    assert_red(repo.lint("no-names", env=UTF8_LOCALE), "blob.bin:1: no-names/content")


def test_latin_1_file_added_then_removed_stays_red_in_a_utf_8_locale(repo):
    repo.plant("notes.txt", b"caf\xe9 " + WORD.encode() + b"\n")
    repo.commit("Add notes")
    (repo.path / "notes.txt").unlink()
    repo.commit("Drop notes")
    assert_clean(repo.lint("no-names", env=UTF8_LOCALE))
    result = repo.lint("no-names", "--base", "base", env=UTF8_LOCALE)
    assert_red(result, "no-names/commit-diff")


def test_an_ignored_file_is_not_walked(repo):
    repo.plant(".gitignore", "local/\n")
    repo.plant("local/notes.txt", f"{WORD}\n")
    assert_clean(repo.lint("no-names"))


@pytest.mark.parametrize("rel", ["notes-{word}.md", "{word}/notes.md", "a/{word}x/b.py"])
def test_word_in_a_file_path(repo, rel):
    assert_clean(repo.lint("no-names"))
    repo.plant(rel.format(word=WORD), "clean\n")
    result = repo.lint("no-names")
    assert_red(result, "no-names/path")
    assert rel.format(word="#" * len(WORD)) in result.stdout


def test_output_never_holds_the_matched_text(repo):
    repo.plant(f"{WORD}.md", f"secret context {WORD} around it\n")
    result = repo.lint("no-names")
    assert_red(result, "no-names/content")
    assert WORD not in result.stdout + result.stderr
    assert "secret context" not in result.stdout


def test_word_in_a_commit_message(repo):
    repo.commit("A clean message")
    assert_clean(repo.lint("no-names", "--base", "base"))
    repo.commit(f"Fix\n\nReported by {WORD.title()}")
    assert_clean(repo.lint("no-names"))
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-message")


def test_word_added_then_removed_stays_red_in_the_history(repo):
    repo.plant("docs/fresh.md", f"written by {WORD}\n")
    repo.commit("Add a note")
    repo.plant("docs/fresh.md", "written by nobody\n")
    repo.commit("Clean the note")
    assert_clean(repo.lint("no-names"))
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-diff")


def test_word_in_a_path_renamed_away_stays_red_in_the_history(repo):
    repo.plant(f"docs/{WORD}-notes.txt", "x\n")
    repo.commit("Add notes")
    repo.git("mv", f"docs/{WORD}-notes.txt", "docs/notes.txt")
    repo.commit("Rename notes")
    assert_clean(repo.lint("no-names"))
    result = repo.lint("no-names", "--base", "base")
    assert_red(result, "docs/" + "#" * len(WORD) + "-notes.txt:1: no-names/commit-path")
    assert WORD not in result.stdout


def test_a_clean_history_has_no_diff_finding(repo):
    repo.plant("docs/fresh.md", "plain text\n")
    repo.commit("Add a note")
    assert_clean(repo.lint("no-names", "--base", "base"))


@pytest.mark.parametrize(
    ("role", "rule"),
    [("author", "no-names/commit-author"), ("committer", "no-names/commit-committer")],
)
@pytest.mark.parametrize(
    "person",
    [
        ("someone-else", ACCOUNT[1]),
        (ACCOUNT[0], "someone@example.com"),
        ("Anas-Xarc", ACCOUNT[1]),
        (ACCOUNT[0], "anas-xarc@users.noreply.github.com"),
    ],
)
def test_wrong_identity(repo, role, rule, person):
    repo.commit("Right identity")
    assert_clean(repo.lint("no-names", "--base", "base"))
    repo.commit("Wrong identity", **{role: person})
    assert_red(repo.lint("no-names", "--base", "base"), rule)


def test_commits_before_the_base_are_not_checked(repo):
    repo.commit("Old", author=("someone-else", "someone@example.com"))
    repo.git("branch", "-f", "base", "HEAD")
    repo.commit("New")
    assert_clean(repo.lint("no-names", "--base", "base"))


def test_merge_by_github_with_the_right_author_passes(repo):
    repo.merge_side_branch(committer=GITHUB)
    assert_clean(repo.lint("no-names", "--base", "base"))


def test_merge_by_github_with_the_verified_address_passes(repo):
    repo.merge_side_branch(author=GITHUB_MERGE_AUTHOR, committer=GITHUB)
    assert_clean(repo.lint("no-names", "--base", "base"))


@pytest.mark.parametrize("committer", [ACCOUNT, GITHUB])
def test_the_verified_address_on_a_commit_that_is_no_merge_fails(repo, committer):
    repo.commit("Right identity")
    assert_clean(repo.lint("no-names", "--base", "base"))
    repo.commit("Plain commit", author=GITHUB_MERGE_AUTHOR, committer=committer)
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-author")


def test_the_verified_address_on_a_merge_by_the_account_fails(repo):
    repo.merge_side_branch(author=GITHUB_MERGE_AUTHOR)
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-author")


@pytest.mark.parametrize(
    "author",
    [
        (ACCOUNT[0], "other@x-arc.ai"),
        (ACCOUNT[0], "anas@x-arc.ai.example"),
        (ACCOUNT[0], "someone@example.com"),
        ("someone-else", GITHUB_MERGE_AUTHOR[1]),
    ],
)
def test_another_address_on_a_merge_by_github_fails(repo, author):
    repo.merge_side_branch(author=author, committer=GITHUB)
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-author")


def test_merge_by_the_account_passes(repo):
    repo.merge_side_branch()
    assert_clean(repo.lint("no-names", "--base", "base"))


def test_merge_by_github_with_a_wrong_author_fails(repo):
    repo.merge_side_branch(author=("someone-else", "someone@example.com"), committer=GITHUB)
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-author")


@pytest.mark.parametrize("committer", [("GitHub", "other@github.com"), ("Git-Hub", GITHUB[1])])
def test_merge_by_a_near_copy_of_github_fails(repo, committer):
    repo.merge_side_branch(committer=committer)
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-committer")


def test_non_merge_commit_by_github_fails(repo):
    repo.commit("Right identity")
    assert_clean(repo.lint("no-names", "--base", "base"))
    repo.commit("Made on the web", committer=GITHUB)
    assert_red(repo.lint("no-names", "--base", "base"), "no-names/commit-committer")


@pytest.mark.parametrize("name", ["PR_BODY", "PR_TITLE", "GITHUB_HEAD_REF"])
def test_word_in_the_pr_text(repo, name):
    assert_clean(repo.lint("no-names", env={name: "A clean value"}))
    result = repo.lint("no-names", env={name: f"clean line\nnamed {WORD} there"})
    assert_red(result, f"env/{name}:2: no-names/env")
    assert WORD not in result.stdout


def test_a_base_that_does_not_resolve_is_an_error_and_not_a_pass(repo):
    result = repo.lint("no-names", "--base", "origin/absent")
    assert result.returncode == 2


def test_a_folder_that_is_no_repo_is_an_error_and_not_a_pass(tmp_path_factory):
    folder = tmp_path_factory.mktemp("plain")
    assert run_lint("no-names", "--root", str(folder)).returncode == 2
