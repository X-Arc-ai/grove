"""Setup's files land in the world's folder, the working folder of its OpenCode passes."""

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api import agent_files
from grove.api.errors import install_error_handlers
from grove.api.session import current_session
from grove.runner.loop import LoopSettings, pass_folder
from grove.views.session import ViewSession

BINARY = {"content-type": "application/octet-stream"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(agent_files.FOLDER_VAR, str(tmp_path))
    monkeypatch.setattr(agent_files, "is_demo_world", lambda driver, world_id: False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agent_files.router)
    app.state.driver = None
    app.dependency_overrides[current_session] = lambda: ViewSession("user-one", "world-one", None)
    return TestClient(app)


def test_a_file_lands_in_the_world_folder_and_lists_and_removes(client, tmp_path):
    response = client.post(agent_files.PREFIX + "?name=plan.pdf", content=b"%PDF", headers=BINARY)
    assert response.status_code == 200
    assert (tmp_path / "world-one" / "plan.pdf").read_bytes() == b"%PDF"
    assert not list((tmp_path / "world-one").glob(".upload-*"))
    assert client.get(agent_files.PREFIX).json()["files"] == [
        {"name": "plan.pdf", "kind": "file", "bytes": 4}
    ]
    assert client.delete(agent_files.PREFIX + "/file/plan.pdf").status_code == 200
    assert client.get(agent_files.PREFIX).json()["files"] == []


def test_a_note_is_a_markdown_file_beside_the_files(client, tmp_path):
    body = json.dumps({"title": "About me", "body": "I like cedar."})
    response = client.post(
        agent_files.PREFIX + "/notes", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    note = tmp_path / "world-one" / "notes" / "About me.md"
    assert note.read_text() == "# About me\n\nI like cedar.\n"
    assert client.get(agent_files.PREFIX).json()["files"][0]["kind"] == "note"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [("../../etc/passwd", "passwd"), ("a\\b.txt", "b.txt"), (".env", "env"), ("x\ny.md", "xy.md")],
)
def test_a_name_never_leaves_the_folder(raw, clean):
    assert agent_files.clean_name(raw) == clean


@pytest.mark.parametrize("raw", ["", "..", "/", "notes"])
def test_an_empty_or_reserved_name_is_refused(client, raw):
    response = client.post(agent_files.PREFIX, params={"name": raw}, content=b"x", headers=BINARY)
    assert response.status_code == 400


def test_a_long_name_keeps_its_suffix():
    name = agent_files.clean_name("a" * 400 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode()) <= agent_files.MAX_NAME_BYTES


def test_the_body_type_and_size_are_enforced(client, tmp_path, monkeypatch):
    assert client.post(agent_files.PREFIX + "?name=a.txt", content=b"x").status_code == 400
    empty = client.post(agent_files.PREFIX + "?name=a.txt", content=b"", headers=BINARY)
    assert empty.status_code == 400
    monkeypatch.setattr(agent_files, "MAX_FILE_BYTES", 3)
    large = client.post(agent_files.PREFIX + "?name=a.txt", content=b"xxxx", headers=BINARY)
    assert large.status_code == 413
    assert not (tmp_path / "world-one" / "a.txt").exists()
    assert not list((tmp_path / "world-one").glob(".upload-*"))


def test_one_world_cannot_reach_another_worlds_file(client, tmp_path):
    other = tmp_path / "world-two"
    other.mkdir()
    (other / "secret.txt").write_text("private")
    assert client.delete(agent_files.PREFIX + "/file/secret.txt").status_code == 404
    assert client.delete(agent_files.PREFIX + "/file/..%2Fworld-two%2Fsecret.txt").status_code in (
        400,
        404,
    )
    assert (other / "secret.txt").exists()
    assert client.get(agent_files.PREFIX).json()["files"] == []


def test_a_link_in_the_folder_is_neither_listed_nor_removed(client, tmp_path):
    folder = tmp_path / "world-one"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    os.symlink(outside, folder / "link.txt")
    assert client.get(agent_files.PREFIX).json()["files"] == []
    assert client.delete(agent_files.PREFIX + "/file/link.txt").status_code == 404
    assert outside.exists()


def test_a_demo_world_and_a_bearer_are_refused(client, monkeypatch):
    bearer = client.get(agent_files.PREFIX, headers={"authorization": "Bearer x"})
    assert bearer.status_code == 401
    monkeypatch.setattr(agent_files, "is_demo_world", lambda driver, world_id: True)
    assert client.get(agent_files.PREFIX).status_code == 403


def test_no_configured_folder_answers_unavailable(client, monkeypatch):
    monkeypatch.delenv(agent_files.FOLDER_VAR)
    assert client.get(agent_files.PREFIX).status_code == 503


def test_the_runner_starts_a_pass_in_the_world_folder_once_it_exists(tmp_path):
    settings = LoopSettings(
        api_url="http://api",
        runner_token="t",
        gateway_url="http://gateway",
        home=tmp_path,
        config_dir=Path("/config"),
        opencode_bin="opencode",
        brain_bin="brain",
        shell_bin="sh",
    )
    grant = {"world_id": "world-one"}
    work = tmp_path / "passes" / "run"
    assert pass_folder(settings, grant, work) == work
    (tmp_path / "worlds" / "world-one").mkdir(parents=True)
    assert pass_folder(settings, grant, work) == tmp_path / "worlds" / "world-one"
