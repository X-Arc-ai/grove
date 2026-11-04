"""Real synthetic filesystem controls for the explicitly read-only adapter."""

import ast
import os
import stat
from pathlib import Path

import pytest

from grove.authority import legacy_reader as reader
from grove.authority.codec import PARTITIONS, DecoderBudget
from grove.authority.legacy import MAX_FILE, legacy_row
from grove.authority.records import AuthorityCodecRefused
from tests.privacy.test_authority_legacy import WORLD, payload, row


@pytest.fixture
def ledger_root(tmp_path):
    root = (tmp_path / "private-ledger").resolve()
    root.mkdir(mode=0o700)
    target = root / reader.ledger_name(WORLD)
    target.write_bytes(payload([row()]))
    target.chmod(0o600)
    return root, target


def read(root, *, budget=None):
    return reader.read_legacy(str(root), world_id=WORLD, budget=budget or DecoderBudget())


def rejected(root, code=None, *, budget=None):
    owner = budget or DecoderBudget()
    before = owner.usage
    with pytest.raises((reader.LegacyReadRefused, AuthorityCodecRefused)) as caught:
        read(root, budget=owner)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.args == (caught.value.code,)
    assert owner.usage == before
    if code:
        assert caught.value.code == code


def test_exact_capture_does_not_create_lock_or_change_bytes_or_permissions(ledger_root):
    root, target = ledger_root
    before = target.stat()
    raw = target.read_bytes()
    result = read(root)
    assert result.binding.count == 1
    assert legacy_row(result, 0).id == row()["id"]
    assert result._data.obj == raw
    after = target.stat()
    assert (before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    assert list(root.iterdir()) == [target]


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o606])
def test_file_privacy_failures_refuse_before_read(ledger_root, monkeypatch, mode):
    root, target = ledger_root
    target.chmod(mode)
    monkeypatch.setattr(
        reader.os, "read", lambda *_: pytest.fail("payload read before privacy check")
    )
    rejected(root, "private")


def test_root_and_ancestor_privacy_failures(ledger_root):
    root, _ = ledger_root
    root.chmod(0o755)
    rejected(root, "private")
    root.chmod(0o700)
    parent = root.parent
    old = parent.stat().st_mode
    try:
        parent.chmod(0o777)
        rejected(root, "private")
    finally:
        parent.chmod(old)


def test_wrong_owner_refuses_before_payload(ledger_root, monkeypatch):
    root, _ = ledger_root
    real = os.fstat

    def different_owner(fd):
        info = real(fd)
        if stat.S_ISREG(info.st_mode):
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(reader.os, "fstat", different_owner)
    monkeypatch.setattr(reader.os, "read", lambda *_: pytest.fail("unsafe payload read"))
    rejected(root, "private")


@pytest.mark.parametrize("where", ["file", "root", "ancestor"])
def test_symlink_components_refuse_without_following(ledger_root, where):
    root, target = ledger_root
    if where == "file":
        held = target.with_suffix(".held")
        target.rename(held)
        target.symlink_to(held)
    elif where == "root":
        alternate = root.with_name("root-link")
        alternate.symlink_to(root, target_is_directory=True)
        root = alternate
    else:
        alternate = root.parent.with_name(root.parent.name + "-link")
        alternate.symlink_to(root.parent, target_is_directory=True)
        root = alternate / root.name
    rejected(root)


@pytest.mark.parametrize("kind", ["fifo", "directory", "hardlink"])
def test_special_files_and_extra_links_refuse_without_payload_read(ledger_root, monkeypatch, kind):
    root, target = ledger_root
    target.unlink()
    if kind == "fifo":
        os.mkfifo(target, mode=0o600)
    elif kind == "directory":
        target.mkdir(mode=0o700)
    else:
        held = target.with_suffix(".held")
        held.write_bytes(payload([]))
        held.chmod(0o600)
        os.link(held, target)
    monkeypatch.setattr(reader.os, "read", lambda *_: pytest.fail("special payload read"))
    rejected(root, "private" if kind == "hardlink" else "file_type")


def test_missing_ledger_or_root_is_never_repaired(ledger_root):
    root, target = ledger_root
    target.unlink()
    rejected(root, "missing")
    assert list(root.iterdir()) == []
    root.rmdir()
    rejected(root, "missing")
    assert not root.exists()


@pytest.mark.parametrize(
    "root", [None, "relative", "/", "/a/../b", "/a/./b", "/a//b", "/a/", "/a\x00"]
)
def test_only_explicit_unambiguous_absolute_string_paths(root):
    with pytest.raises(reader.LegacyReadRefused, match="path"):
        reader.read_legacy(root, world_id=WORLD, budget=DecoderBudget())


def test_world_binding_never_falls_back_to_another_file(ledger_root):
    root, target = ledger_root
    other = WORLD[:-1] + "2"
    with pytest.raises(reader.LegacyReadRefused, match="missing"):
        reader.read_legacy(str(root), world_id=other, budget=DecoderBudget())
    target.rename(root / reader.ledger_name(other))
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        reader.read_legacy(str(root), world_id=other, budget=DecoderBudget())


def test_oversize_is_rejected_before_read_or_allocation(ledger_root, monkeypatch):
    root, target = ledger_root
    with target.open("wb") as file:
        file.truncate(MAX_FILE + 1)
    monkeypatch.setattr(reader.os, "read", lambda *_: pytest.fail("oversized read"))
    rejected(root, "size")


def test_combined_input_partition_is_reserved_before_os_read(ledger_root, monkeypatch):
    root, _ = ledger_root
    budget = DecoderBudget()
    reservation = budget.reserve("input", PARTITIONS["input"])
    monkeypatch.setattr(reader.os, "read", lambda *_: pytest.fail("unreserved read"))
    rejected(root, "allocation_limit", budget=budget)
    reservation.release()


@pytest.mark.parametrize("change", ["replace", "rewrite", "append", "truncate", "root_replace"])
def test_observed_change_or_replacement_refuses_complete_capture(ledger_root, monkeypatch, change):
    root, target = ledger_root
    real = os.read
    changed = False

    def mutate_after_read(fd, size):
        nonlocal changed
        raw = real(fd, size)
        if not changed:
            changed = True
            if change == "replace":
                other = target.with_suffix(".replacement")
                other.write_bytes(raw)
                other.chmod(0o600)
                other.replace(target)
            elif change == "rewrite":
                target.write_bytes(raw.replace(b"owner", b"other"))
            elif change == "append":
                with target.open("ab") as file:
                    file.write(b" ")
            elif change == "truncate":
                target.write_bytes(b"")
            else:
                held = root.with_name("held-root")
                root.rename(held)
                root.mkdir(mode=0o700)
        return raw

    monkeypatch.setattr(reader.os, "read", mutate_after_read)
    rejected(root, "size" if change == "append" else "changed")


def test_short_payload_and_extra_eof_byte_refuse(ledger_root, monkeypatch):
    root, _ = ledger_root
    real = os.read
    with monkeypatch.context() as patch:
        patch.setattr(reader.os, "read", lambda fd, size: real(fd, max(0, size - 1)))
        rejected(root, "size")
    with monkeypatch.context() as patch:
        patch.setattr(reader.os, "read", lambda fd, size: b"x" if size == 1 else real(fd, size))
        rejected(root, "size")


@pytest.mark.parametrize("bad", [False, True])
def test_every_owned_descriptor_closes_on_success_and_parser_failure(ledger_root, monkeypatch, bad):
    root, target = ledger_root
    if bad:
        target.write_bytes(b"{broken")
    opened = []
    real = os.open

    def track(*args, **kwargs):
        fd = real(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(reader.os, "open", track)
    if bad:
        rejected(root)
    else:
        read(root)
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_reader_imports_calls_and_outputs_cannot_grant_or_write():
    path = Path(reader.__file__)
    tree = ast.parse(path.read_text())
    imports = {"os", "stat", "hashlib", "codec", "legacy", "records"}
    os_calls = {"open", "read", "close", "stat", "fstat", "getuid"}
    forbidden = {
        "environ",
        "getenv",
        "locked_ledger",
        "_root",
        "append_intent",
        "mkdir",
        "makedirs",
        "chmod",
        "chown",
        "write",
        "append",
        "truncate",
        "rename",
        "replace",
        "unlink",
        "remove",
        "fsync",
        "flock",
        "authorize",
        "activate",
        "publish",
        "register",
        "genesis",
        "exec",
        "eval",
        "__import__",
        "O_CREAT",
        "O_WRONLY",
        "O_RDWR",
        "O_TRUNC",
        "O_APPEND",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in imports for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module in imports
        if isinstance(node, ast.Attribute):
            # list.append is internal descriptor accounting, never a file API.
            assert node.attr not in forbidden - {"append"}
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                assert node.func.attr in os_calls
    for name in ("authorize", "activate", "publish", "locked_ledger", "genesis", "append_intent"):
        assert not hasattr(reader, name)
