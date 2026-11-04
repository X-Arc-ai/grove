"""Synthetic object databases; no fetch, private repository or personal history."""

import json
import os
import subprocess
import sys
import zlib
from datetime import datetime

import pytest

from grove import clock
from grove.connectors import git
from grove.ingest import NotAnExport, chat_locator, message_key


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "repository"
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "anas-xarc",
        "GIT_AUTHOR_EMAIL": "261762116+anas-xarc@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "anas-xarc",
        "GIT_COMMITTER_EMAIL": "261762116+anas-xarc@users.noreply.github.com",
        "GIT_AUTHOR_DATE": "2026-10-01T09:00:00+02:00",
        "GIT_COMMITTER_DATE": "2026-10-01T08:00:00+00:00",
    }

    def run(*args, data=None, env=None):
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            env=environment | (env or {}),
            input=data,
            capture_output=True,
            check=True,
        )
        return result.stdout.decode().strip()

    path.mkdir()
    run("init", "--template=", "--initial-branch=main")
    tree = run("mktree", data=b"")

    def commit(message="Synthetic change", parents=(), *, at=None, raw=None):
        env = {} if at is None else {"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at}
        if raw is not None:
            sha = run("hash-object", "-t", "commit", "-w", "--stdin", data=raw)
        else:
            args = ["commit-tree", tree]
            for parent in parents:
                args.extend(["-p", parent])
            sha = run(*args, data=message.encode(), env=env)
        run("update-ref", "refs/heads/main", sha)
        return sha

    return path, run, commit


def payload(chat):
    assert len(chat.messages) == 1
    return json.loads(chat.messages[0].text)


def test_commit_keeps_source_times_author_and_provenance(repository):
    path, _, commit = repository
    head = commit("Change the parser\n\nThe local change passed its test.")
    result = git.read_repository(path, "repo-one")
    assert result.head == head
    assert len(result.chats) == 1
    chat = result.chats[0]
    message = chat.messages[0]
    data = payload(chat)
    assert data["artifact"] == "commit"
    assert data["commit"] == head
    assert data["repository"] == "repo-one"
    assert data["parents"] == []
    assert data["author"] == message.sender == "anas-xarc"
    assert data["authored_at"] == "2026-10-01T09:00:00+02:00"
    assert data["committed_at"] == "2026-10-01T08:00:00+00:00"
    assert message.at == datetime.fromisoformat(data["authored_at"])
    assert message.speaker_kind == "third_party"
    assert message.modality == "text"
    assert message.key == message_key("git", "repo-one", head)
    assert chat.chat_id == message.key
    assert chat_locator("git", chat.chat_id).startswith("git")
    assert "does not prove a feature is live" in data["proves"]
    assert data["message"].startswith("Change the parser\n\n")


def test_replay_and_incremental_read_repeat_no_keys(repository):
    path, _, commit = repository
    first = commit()
    before = git.read_repository(path, "repo-one")
    assert git.read_repository(path, "repo-one", previous_head=first).chats == []
    second = commit("Second change", [first], at="2026-10-02T09:00:00Z")
    after = git.read_repository(path, "repo-one", previous_head=first)
    assert after.head == second
    assert [payload(chat)["commit"] for chat in after.chats] == [second]
    replay = git.read_repository(path, "repo-one")
    assert replay.chats[0].messages[0].key == before.chats[0].messages[0].key
    assert replay.chats[1].messages[0].key == after.chats[0].messages[0].key
    other = git.read_repository(path, "repo-two")
    assert other.chats[0].messages[0].key != replay.chats[0].messages[0].key


def test_rewrite_and_unknown_cursor_replay_reachable_history(repository):
    path, _, commit = repository
    common = commit()
    discarded = commit("Old branch", [common])
    replacement = commit("Replacement", [common])
    after = git.read_repository(path, "repo-one", previous_head=discarded)
    assert after.head == replacement
    assert {payload(chat)["commit"] for chat in after.chats} == {common, replacement}
    unknown = git.read_repository(path, "repo-one", previous_head="1" * 40)
    assert unknown == after


def test_merge_is_one_episode_and_commits_follow_their_own_time(repository):
    path, _, commit = repository
    base = commit()
    left = commit("Left", [base], at="2026-10-03T09:00:00Z")
    right = commit("Right", [base], at="2026-10-02T09:00:00Z")
    merged = commit("Merge both changes", [left, right], at="2026-10-04T09:00:00Z")
    result = git.read_repository(path, "repo-one", previous_head=base)
    assert [payload(chat)["commit"] for chat in result.chats] == [right, left, merged]
    assert payload(result.chats[-1])["artifact"] == "merge"
    assert payload(result.chats[-1])["parents"] == [left, right]


def test_detached_and_packed_refs_are_supported(repository):
    path, run, commit = repository
    head = commit()
    run("pack-refs", "--all")
    assert git.read_repository(path, "repo-one").head == head
    (path / ".git" / "HEAD").write_text(head + "\n")
    assert git.read_repository(path, "repo-one").head == head


def test_bare_clone_object_database_is_supported(repository):
    path, _, commit = repository
    head = commit()
    assert git.read_repository(path / ".git", "repo-one").head == head


@pytest.mark.parametrize("cursor", ["main", "--all", "1" * 39, "A" * 40, "x" * 2048, 3])
def test_malformed_cursor_is_refused_without_echo(repository, cursor):
    path, _, commit = repository
    commit()
    with pytest.raises(git.GitReadError) as error:
        git.read_repository(path, "repo-one", previous_head=cursor)
    assert str(cursor) not in str(error.value)


def test_identifier_limit_precedes_process_or_key_generation(repository, monkeypatch):
    path, _, _ = repository

    def forbidden(*args, **kwargs):
        pytest.fail("an oversized identifier reached a process or hash")

    monkeypatch.setattr(git, "_metadata", forbidden)
    monkeypatch.setattr(git, "message_key", forbidden)
    with pytest.raises(NotAnExport, match="1024-character"):
        git.read_repository(path, "x" * 1025)


def test_repository_identity_is_clean_text_but_its_key_stays_exact(repository):
    path, _, commit = repository
    head = commit()
    identifier = "repo-\ud800\x00"
    result = git.read_repository(path, identifier)
    message = result.chats[0].messages[0]
    assert payload(result.chats[0])["repository"] == "repo-\ufffd"
    assert message.key == message_key("git", identifier, head)
    message.text.encode("utf-8")


def test_invalid_or_empty_repository_is_failed_not_empty(repository, tmp_path):
    path, _, _ = repository
    for selected in [path, tmp_path / "missing", tmp_path]:
        with pytest.raises(git.GitReadError):
            git.read_repository(selected, "repo-one")


@pytest.mark.parametrize(
    "name",
    ["shallow", "commondir", "reftable", "objects/info/alternates", "objects/info/http-alternates"],
)
def test_indirection_or_partial_history_is_refused(repository, name):
    path, _, commit = repository
    commit()
    (path / ".git" / name).write_text("do not follow this\n")
    with pytest.raises(git.GitReadError):
        git.read_repository(path, "repo-one")


def test_linked_worktree_metadata_is_refused(repository, tmp_path):
    path, _, commit = repository
    commit()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {path / '.git'}\n")
    with pytest.raises(git.GitReadError):
        git.read_repository(linked, "repo-one")


def test_ref_symlink_and_traversal_are_refused(repository, tmp_path):
    path, _, commit = repository
    head = commit()
    metadata = path / ".git"
    target = tmp_path / "outside"
    target.write_text(head + "\n")
    ref = metadata / "refs" / "heads" / "main"
    ref.unlink()
    ref.symlink_to(target)
    with pytest.raises(git.GitReadError):
        git.read_repository(path, "repo-one")
    (metadata / "HEAD").write_text("ref: refs/../../outside\n")
    with pytest.raises(git.GitReadError):
        git.read_repository(path, "repo-one")


def test_source_config_hooks_replace_refs_and_workspace_are_not_read(
    repository, tmp_path, monkeypatch
):
    path, run, commit = repository
    real = commit("Real object")
    replacement = commit("Replacement object")
    run("update-ref", "refs/heads/main", real)
    run("replace", real, replacement)
    marker = tmp_path / "executed"
    attack = tmp_path / "attack.sh"
    attack.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    attack.chmod(0o700)
    metadata = path / ".git"
    (metadata / "hooks").mkdir(exist_ok=True)
    (metadata / "hooks" / "post-checkout").write_text(attack.read_text())
    (metadata / "hooks" / "post-checkout").chmod(0o700)
    (metadata / "config").write_text(
        "[this config is deliberately malformed and must not be parsed\n"
    )
    (path / ".mailmap").write_text("Wrong Person <wrong@example.test> <source@example.test>\n")
    (path / "private.txt").write_text("This workspace file must never enter an episode")
    injected = tmp_path / "global-config"
    injected.write_text(f"[core]\n\tfsmonitor = {attack}\n\tpager = {attack}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(injected))
    monkeypatch.setenv("GIT_TRACE", str(marker))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(attack))
    result = git.read_repository(path, "repo-one")
    assert result.head == real
    assert payload(result.chats[0])["message"].strip() == "Real object"
    assert "workspace file" not in result.chats[0].messages[0].text
    assert not marker.exists()


def test_missing_object_never_fetches_or_claims_empty_success(repository, tmp_path):
    path, _, commit = repository
    head = commit()
    (path / ".git" / "objects" / head[:2] / head[2:]).unlink()
    marker = tmp_path / "fetched"
    (path / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = ext::touch {marker}\n\tpromisor = true\n'
        "[extensions]\n\tpartialClone = origin\n"
    )
    with pytest.raises(git.GitReadError):
        git.read_repository(path, "repo-one")
    assert not marker.exists()


def test_commit_count_limit_fails_before_partial_result(repository, monkeypatch):
    path, _, commit = repository
    head = commit()
    commit("Next", [head])
    monkeypatch.setattr(git, "MAX_COMMITS", 1)
    with pytest.raises(git.GitReadError, match="limits"):
        git.read_repository(path, "repo-one")


def test_commit_object_size_limit_precedes_content_read(repository, monkeypatch):
    path, _, commit = repository
    commit("x" * 2000)
    monkeypatch.setattr(git, "MAX_COMMIT_BYTES", 1000)
    with pytest.raises(git.GitReadError, match="limits"):
        git.read_repository(path, "repo-one")


def test_total_size_limit_is_enforced(repository, monkeypatch):
    path, _, commit = repository
    head = commit("x" * 1000)
    commit("y" * 1000, [head])
    monkeypatch.setattr(git, "MAX_TOTAL_BYTES", 1000)
    with pytest.raises(NotAnExport):
        git.read_repository(path, "repo-one")


def test_serialized_commit_is_not_silently_truncated(repository, monkeypatch):
    path, _, commit = repository
    commit("x" * 2000)
    monkeypatch.setattr(git, "MAX_MESSAGE_CHARS", 1000)
    with pytest.raises(git.GitReadError, match="limits"):
        git.read_repository(path, "repo-one")


def test_corrupt_commit_object_never_yields_evidence(repository):
    path, run, commit = repository
    head = commit("Original message")
    raw = run("cat-file", "commit", head).replace("Original message", "Tampered message").encode()
    loose = path / ".git" / "objects" / head[:2] / head[2:]
    loose.chmod(0o600)
    loose.write_bytes(zlib.compress(f"commit {len(raw)}\0".encode() + raw))
    with pytest.raises(git.GitReadError):
        git.read_repository(path, "repo-one")


def test_legacy_message_encoding_preserves_text(repository):
    path, run, commit = repository
    head = commit("Original message")
    raw = run("cat-file", "commit", head).encode()
    header, _, _ = raw.partition(b"\n\n")
    commit(raw=header + b"\nencoding iso-8859-1\n\nCaf\xe9 revision\n")
    result = git.read_repository(path, "repo-one")
    assert payload(result.chats[0])["message"] == "Caf\u00e9 revision\n"


def test_subprocess_output_is_bounded(tmp_path):
    # Test the actual pipe reader, not a fake process or an assertion on its flags.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "git"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 100000)\n")
    fake.chmod(0o700)
    with pytest.raises(git.GitReadError, match="limits"):
        git._run(tmp_path, {"PATH": str(fake_bin)}, [], limit=100, deadline=clock.monotonic() + 5)


def test_subprocess_deadline_kills_owned_process(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "git"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake.chmod(0o700)
    with pytest.raises(git.GitReadError, match="limits"):
        git._run(tmp_path, {"PATH": str(fake_bin)}, [], limit=100, deadline=clock.monotonic() + 0.1)
