"""Synthetic private local files only; no live resource or database."""

import os
from dataclasses import replace

import pytest

from grove import clock
from grove.authority.lease import LockIdentity, RootIdentity, open_existing_root
from grove.authority.publication import PublicationBindings, PublicationRefused
from grove.authority.publication_files import PublicationFiles
from grove.authority.records import PROFILE


def private_root(tmp_path):
    root = tmp_path.resolve() / "root"
    root.mkdir(mode=0o700)
    (root / ".lock").write_bytes(b"")
    (root / ".lock").chmod(0o600)
    (root / "authority").mkdir(mode=0o700)
    (root / "authority-manifest.json").write_bytes(b"old")
    (root / "authority-manifest.json").chmod(0o600)
    r, lock, authority = (root.stat(), (root / ".lock").stat(), (root / "authority").stat())
    binding = PublicationBindings(
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "a" * 64,
        "a" * 64,
        PROFILE,
        RootIdentity(r.st_dev, r.st_ino),
        LockIdentity(lock.st_dev, lock.st_ino),
        RootIdentity(authority.st_dev, authority.st_ino),
        "b" * 64,
    )
    return root, binding


@pytest.fixture
def files(tmp_path):
    root, binding = private_root(tmp_path)
    lease = open_existing_root(str(root), binding.root_identity)
    lease.acquire_exclusive(clock.monotonic() + 5)
    helper = PublicationFiles(str(root), binding, lease, lambda: None)
    yield root, helper
    try:
        helper.close()
    except (PublicationRefused, OSError):
        pass
    lease.close()


def test_file_fsync_replace_directory_fsync_order(files, monkeypatch):
    root, helper = files
    calls = []
    real_sync, real_replace = os.fsync, os.replace

    def sync(fd):
        calls.append("directory" if os.fstat(fd).st_mode & 0o170000 == 0o040000 else "file")
        return real_sync(fd)

    def rename(*args, **kwargs):
        calls.append("replace")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", rename)
    before = helper.observe("manifest", "world-1")
    candidate = helper.stage_bytes("manifest", b"new")
    final = helper.replace(candidate, before, "world-1")
    assert calls == ["file", "replace", "directory"]
    assert final.digest == candidate.observation.digest
    assert (root / "authority-manifest.json").read_bytes() == b"new"
    assert not tuple(root.glob(".publication-*"))


def test_short_write_eintr_and_zero_write(files, monkeypatch):
    root, helper = files
    write = os.write
    calls = 0

    def short(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError()
        return write(fd, data[:2])

    monkeypatch.setattr(os, "write", short)
    candidate = helper.stage_bytes("manifest", b"abcdef")
    assert candidate.observation.size == 6 and calls == 4
    helper.discard(candidate)
    monkeypatch.setattr(os, "write", lambda *_: 0)
    with pytest.raises(PublicationRefused, match="io"):
        helper.stage_bytes("manifest", b"abcdef")
    assert (root / "authority-manifest.json").read_bytes() == b"old"
    assert not tuple(root.glob(".publication-*"))


@pytest.mark.parametrize("fault", ["file_sync", "replace", "directory_sync", "inspection"])
def test_failed_replace_preserves_current_bytes(files, monkeypatch, fault):
    root, helper = files
    before = helper.observe("manifest", "world-1")
    if fault == "file_sync":
        monkeypatch.setattr(os, "fsync", lambda *_: (_ for _ in ()).throw(OSError()))
        with pytest.raises(PublicationRefused):
            helper.stage_bytes("manifest", b"new")
        assert (root / "authority-manifest.json").read_bytes() == b"old"
        return
    candidate = helper.stage_bytes("manifest", b"new")
    if fault == "replace":
        monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        error = PublicationRefused
    elif fault == "directory_sync":
        monkeypatch.setattr(os, "fsync", lambda *_: (_ for _ in ()).throw(OSError()))
        error = PublicationRefused
    else:
        original = helper.observe
        count = 0

        def observe(*args):
            nonlocal count
            count += 1
            if count > 1:
                raise OSError()
            return original(*args)

        monkeypatch.setattr(helper, "observe", observe)
        error = PublicationRefused
    with pytest.raises(error):
        helper.replace(candidate, before, "world-1")
    assert (root / "authority-manifest.json").read_bytes() == (
        b"old" if fault == "replace" else b"new"
    )


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "mode"])
def test_private_path_and_descriptor_bindings(files, kind):
    root, helper = files
    path = root / "authority-manifest.json"
    if kind == "symlink":
        path.unlink()
        path.symlink_to(root / ".lock")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "hardlink":
        os.link(path, root / "linked")
    else:
        path.chmod(0o644)
    with pytest.raises(PublicationRefused):
        helper.observe("manifest", "world-1")


def test_changed_same_size_content_refuses(files):
    root, helper = files
    before = helper.observe("manifest", "world-1")
    candidate = helper.stage_bytes("manifest", b"new")
    (root / "authority-manifest.json").write_bytes(b"BAD")
    with pytest.raises(PublicationRefused, match="changed"):
        helper.replace(candidate, before, "world-1")
    assert (root / "authority-manifest.json").read_bytes() == b"BAD"


def test_forged_or_wrong_context_candidate_refuses(files):
    _, helper = files
    candidate = helper.stage_bytes("manifest", b"new")
    with pytest.raises(PublicationRefused, match="binding"):
        helper.candidate(replace(candidate))
    with pytest.raises(PublicationRefused, match="binding"):
        helper.candidate(replace(candidate, owner=object()))


def test_no_ordinary_creation_of_root_or_authority(tmp_path):
    root, binding = private_root(tmp_path)
    (root / "authority").rmdir()
    lease = open_existing_root(str(root), binding.root_identity)
    lease.acquire_exclusive(clock.monotonic() + 5)
    try:
        with pytest.raises(PublicationRefused, match="missing"):
            PublicationFiles(str(root), binding, lease, lambda: None)
        assert not (root / "authority").exists()
    finally:
        lease.close()


def test_orphan_temp_is_never_authority(files):
    root, helper = files
    (root / ".publication-unused.tmp").write_bytes(b"newer")
    assert helper.observe("manifest", "world-1").size == 3
    helper.close()
    assert (root / ".publication-unused.tmp").read_bytes() == b"newer"


def test_candidate_role_and_total_limits(files):
    _, helper = files
    for _ in range(3):
        helper.stage_bytes("manifest", b"new")
    with pytest.raises(PublicationRefused, match="capacity"):
        helper.stage_bytes("manifest", b"fourth")
    with pytest.raises(PublicationRefused, match="capacity"):
        helper.stage_bytes("manifest", b"x" * 1048577)


def test_read_io_failure_has_no_payload_or_exception_chain(files, monkeypatch):
    from grove.authority.codec import DecoderBudget

    _, helper = files

    def fail(*args):
        raise OSError("private-path-and-payload")

    monkeypatch.setattr(os, "read", fail)
    with pytest.raises(PublicationRefused) as caught:
        with helper.read("manifest", "world-1", DecoderBudget()):
            raise AssertionError("unreachable")
    assert str(caught.value) == "inspect:io"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
