"""Setup's files land in the world's folder, the working folder of its OpenCode passes."""

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api import agent_files
from grove.api.errors import ApiError, install_error_handlers
from grove.api.session import current_session
from grove.runner.loop import LoopSettings, pass_folder
from grove.views.session import ViewSession

BINARY = {"content-type": "application/octet-stream"}
JSON = {"content-type": "application/json"}
# Written out here, not read from the rule, so a name dropped from the rule fails these tests.
INSTRUCTION_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "opencode.json",
    "opencode.jsonc",
    "tui.json",
    "tui.jsonc",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(agent_files.FOLDER_VAR, str(tmp_path))
    monkeypatch.setattr(agent_files, "is_demo_world", lambda driver, world_id: False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(agent_files.router)
    app.state.driver = None
    session = ViewSession("user-one", "world-one", None)
    app.dependency_overrides[current_session] = lambda: session
    monkeypatch.setattr(agent_files, "current_session", lambda request: session)
    return TestClient(app)


def add_file(client, name, content=b"x"):
    return client.post(agent_files.PREFIX, params={"name": name}, content=content, headers=BINARY)


def add_note(client, title, body="text"):
    text = json.dumps({"title": title, "body": body})
    return client.post(agent_files.PREFIX + "/notes", content=text, headers=JSON)


def on_disk(tmp_path):
    folder = tmp_path / "world-one"
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if not p.is_dir())


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


def test_a_note_titled_notes_is_a_plain_note(client, tmp_path):
    """Only a file of that name would meet the notes folder. The note is stored as notes.md."""
    assert add_note(client, "notes").status_code == 200
    assert on_disk(tmp_path) == ["notes/notes.md"]


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


@pytest.mark.parametrize(
    "name",
    [
        *INSTRUCTION_FILES,
        "agents.md",
        "Agents.Md",
        "CLAUDE.MD",
        "Context.md",
        "OpenCode.JSON",
        "OPENCODE.JSONC",
        "TUI.JSON",
        "tsconfig.json",
        "TSConfig.JSON",
        "jsconfig.json",
        "bunfig.toml",
        "package.json",
        "node_modules",
        "HEAD",
        "commondir",
        "bun.lock",
        "bun.lockb",
        "AGENTS.md ",
        "folder/AGENTS.md",
        "folder\\opencode.json",
        ". .AGENTS.md",
        "AGENTS\u200b.md",
        "AGENT\u017f.md",
    ],
)
def test_a_file_with_an_instruction_name_is_refused(client, tmp_path, name):
    response = add_file(client, name)
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("This name is kept for the agent")
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize(
    "title",
    [
        "AGENTS",
        "agents",
        "Agents",
        "CLAUDE",
        "claude",
        "CONTEXT",
        "context",
        " AGENTS ",
        "AGENTS\n",
    ],
)
def test_a_note_title_that_makes_an_instruction_name_is_refused(client, tmp_path, title):
    response = add_note(client, title)
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("This name is kept for the agent")
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("AGENTS.md", "AGENTS.md.md"),
        ("opencode.json", "opencode.json.md"),
        ("My agents", "My agents.md"),
        ("../AGENTS", "-AGENTS.md"),
    ],
)
def test_a_title_near_an_instruction_name_is_a_plain_note(client, title, name):
    assert add_note(client, title).json()["file"]["name"] == name


def test_an_instruction_file_from_an_earlier_build_can_be_removed(client, tmp_path):
    """The runner refuses the world's folder while the file is there, so the person must be able
    to take it off, although the API no longer lets anyone add it."""
    folder = tmp_path / "world-one"
    (folder / "notes").mkdir(parents=True)
    (folder / "notes" / "AGENTS.md").write_text("planted")
    (folder / "opencode.json").write_text("{}")
    listed = {(row["kind"], row["name"]) for row in client.get(agent_files.PREFIX).json()["files"]}
    assert listed == {("note", "AGENTS.md"), ("file", "opencode.json")}
    assert client.delete(agent_files.PREFIX + "/note/AGENTS.md").status_code == 200
    assert client.delete(agent_files.PREFIX + "/file/opencode.json").status_code == 200
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (". .a", "a"),
        (" . .a", "a"),
        (". . . .a", "a"),
        (".. ..a", "a"),
        (". .  . .a.txt", "a.txt"),
        (".\u200b .a", "a"),
        ("x/. .a", "a"),
        ("x\\. .a", "a"),
    ],
)
def test_no_clean_name_ever_starts_with_a_dot(raw, clean):
    assert agent_files.clean_name(raw) == clean


@pytest.mark.parametrize("raw", [". .", " . . ", ". . .", ".. ..", ". .notes"])
def test_a_name_of_only_dots_and_spaces_is_refused(client, tmp_path, raw):
    assert add_file(client, raw).status_code == 400
    assert on_disk(tmp_path) == []


def test_six_hidden_uploads_cannot_pass_a_lowered_limit(client, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_files, "MAX_WORLD_FILES", 2)
    codes = [add_file(client, f". .{letter}").status_code for letter in "abcdef"]
    assert codes == [200, 200, 413, 413, 413, 413]
    assert on_disk(tmp_path) == ["a", "b"]


def test_the_quota_counts_hidden_files_that_are_already_on_disk(client, tmp_path, monkeypatch):
    folder = tmp_path / "world-one"
    (folder / "notes").mkdir(parents=True)
    (folder / ".a").write_bytes(b"xxxx")
    (folder / "notes" / ".b").write_bytes(b"xxxx")
    assert client.get(agent_files.PREFIX).json()["files"] == []
    monkeypatch.setattr(agent_files, "MAX_WORLD_FILES", 2)
    assert add_file(client, "plan.txt").status_code == 413
    monkeypatch.setattr(agent_files, "MAX_WORLD_FILES", 200)
    monkeypatch.setattr(agent_files, "MAX_WORLD_BYTES", 9)
    assert add_file(client, "plan.txt", b"xx").status_code == 413
    assert add_file(client, "plan.txt", b"x").status_code == 200


def test_a_full_world_still_takes_a_file_of_a_name_it_holds(client, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_files, "MAX_WORLD_FILES", 1)
    assert add_file(client, "plan.txt", b"one").status_code == 200
    assert add_file(client, "plan.txt", b"two").status_code == 200
    assert (tmp_path / "world-one" / "plan.txt").read_bytes() == b"two"


def test_a_stale_temp_file_goes_and_a_running_one_counts(client, tmp_path, monkeypatch):
    folder = tmp_path / "world-one"
    (folder / "notes").mkdir(parents=True)
    stale, running = folder / ".upload-stale", folder / ".upload-running"
    stale.write_bytes(b"xxxx")
    running.write_bytes(b"xxxx")
    old = running.stat().st_mtime - 2 * 3600
    os.utime(stale, (old, old))
    monkeypatch.setattr(agent_files, "MAX_WORLD_FILES", 2)
    assert add_file(client, "a.txt").status_code == 200
    assert not stale.exists() and running.exists()
    assert add_file(client, "b.txt").status_code == 413


def test_a_repeated_note_title_keeps_the_first_note(client, tmp_path):
    assert add_note(client, "Plan", "first").status_code == 200
    second = add_note(client, "Plan", "second")
    assert second.status_code == 409
    assert second.json()["error"]["message"] == (
        "A note with this title already exists. Choose another title."
    )
    assert (tmp_path / "world-one" / "notes" / "Plan.md").read_text() == "# Plan\n\nfirst\n"
    assert on_disk(tmp_path) == ["notes/Plan.md"]


def test_of_two_requests_for_one_note_name_only_one_gets_it(client, tmp_path, monkeypatch):
    """Another request takes the name after this one has checked for room: never replace it."""
    target = tmp_path / "world-one" / "notes" / "Plan.md"
    check_room = agent_files._check_room

    def the_other_request_lands(*args):
        check_room(*args)
        target.write_text("the other request's note")

    monkeypatch.setattr(agent_files, "_check_room", the_other_request_lands)
    assert add_note(client, "Plan", "this note").status_code == 409
    assert target.read_text() == "the other request's note"
    assert on_disk(tmp_path) == ["notes/Plan.md"]


@pytest.mark.parametrize(
    ("first", "second", "names"),
    [
        ("Q1/plan", "Q2/plan", ["Q1-plan.md", "Q2-plan.md"]),
        ("Q1\\plan", "Q2\\plan", ["Q1-plan.md", "Q2-plan.md"]),
        ("a/b/c", "x/b/c", ["a-b-c.md", "x-b-c.md"]),
    ],
)
def test_a_title_with_a_slash_keeps_its_whole_text(client, first, second, names):
    assert add_note(client, first, "one").json()["file"]["name"] == names[0]
    assert add_note(client, second, "two").json()["file"]["name"] == names[1]


def test_a_file_of_the_same_name_still_replaces(client, tmp_path):
    assert add_file(client, "plan.txt", b"one").status_code == 200
    assert add_file(client, "plan.txt", b"two").status_code == 200
    assert (tmp_path / "world-one" / "plan.txt").read_bytes() == b"two"


def test_a_long_cjk_title_saves_and_comes_off(client, tmp_path):
    saved = add_note(client, "\u96f2" * 70)
    assert saved.status_code == 200
    name = saved.json()["file"]["name"]
    assert name.endswith(".md") and len(name.encode()) <= agent_files.MAX_NAME_BYTES
    assert client.delete(f"{agent_files.PREFIX}/note/{name}").status_code == 200
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("\u96f2" * 66 + "ab.md", id="suffix-added-after-the-cut"),
        pytest.param("a" * 199 + " ", id="cut-left-a-space"),
        pytest.param("e\u0301.txt", id="not-nfc"),
        pytest.param("broken \ufffd.txt", id="replacement-character"),
    ],
)
def test_a_name_that_an_earlier_build_stored_comes_off_as_it_is(client, tmp_path, name):
    folder = tmp_path / "world-one" / "notes"
    folder.mkdir(parents=True)
    (folder / name).write_text("note")
    listed = client.get(agent_files.PREFIX).json()["files"][0]["name"]
    assert client.delete(f"{agent_files.PREFIX}/note/{listed}").status_code == 200
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize(
    "raw",
    [
        "\u96f2" * 90 + ".pdf",
        "\U0001f331" * 70,
        "a" * 400,
        "a" * 199 + " b",
        "e\u200b\u0301.txt",
        "name.",
        "a .txt",
        "U+FFFC \ufffc.txt",
    ],
)
def test_every_name_the_api_gives_comes_off_as_a_file_and_as_a_note(client, tmp_path, raw):
    for kind, saved in (("file", add_file(client, raw)), ("note", add_note(client, raw))):
        assert saved.status_code == 200, saved.text
        name = saved.json()["file"]["name"]
        assert {"name": name, "kind": kind} in [
            {"name": row["name"], "kind": row["kind"]}
            for row in client.get(agent_files.PREFIX).json()["files"]
        ]
        removed = client.delete(f"{agent_files.PREFIX}/{kind}/{name}")
        assert removed.status_code == 200, removed.text
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize("name", ["a" * 300, ".hidden", "a%5Cb", "a%00b", "a%0Ab"])
def test_a_delete_of_a_name_no_listing_gives_is_refused(client, name):
    assert client.delete(f"{agent_files.PREFIX}/file/{name}").status_code == 400


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("\ud800", "text"),
        ("plan \udfff", "text"),
        ("x" * 120 + "\ud800", "text"),
        ("Plan", "text \ud800"),
        ("Plan", "\udc00"),
    ],
)
def test_a_lone_surrogate_in_a_note_is_refused(client, tmp_path, title, body):
    assert add_note(client, title, body).status_code == 400
    assert on_disk(tmp_path) == []


@pytest.mark.parametrize("query", ["%ED%A0%80.txt", "a%ED%B0%80.txt", "a%FF.txt"])
def test_a_lone_surrogate_in_a_file_name_is_refused(client, tmp_path, query):
    response = client.post(f"{agent_files.PREFIX}?name={query}", content=b"x", headers=BINARY)
    assert response.status_code == 400
    assert on_disk(tmp_path) == []


def test_a_lone_surrogate_never_passes_clean_name():
    with pytest.raises(ApiError) as refused:
        agent_files.clean_name("a\ud800.txt")
    assert refused.value.status == 400


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
    grant = {"world_id": "world-one", "agent": "learner"}
    work = tmp_path / "passes" / "run"
    assert pass_folder(settings, grant, work) == work
    (tmp_path / "worlds" / "world-one").mkdir(parents=True)
    assert pass_folder(settings, grant, work) == tmp_path / "worlds" / "world-one"
    # OpenCode's read tool follows a link, so a folder with one must not become the working folder.
    os.symlink(tmp_path / "other", tmp_path / "worlds" / "world-one" / "link.txt")
    assert pass_folder(settings, grant, work) == work
