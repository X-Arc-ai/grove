# Fixture imports are collected by pytest and injected into the test functions.
# ruff: noqa: F811
import json

from grove.store.query import WorldQuery
from grove.writer.connectors import connection
from tests.connectors.test_connections import deps  # noqa: F401
from tests.connectors.test_git import repository  # noqa: F401
from tests.ingest.conftest import counts


def test_git_api_reimport_incremental_and_rewrite_keep_one_episode_per_commit(
    driver, seeded_world, client_for, deps, repository
):
    path, _, commit = repository
    first = commit("The parser changed. This commit does not prove a deployment.")
    deps.repositories[(seeded_world, "sample")] = path
    client = client_for(seeded_world)
    reply = client.post("/v1/connectors/git", json={"repository": "sample"})
    assert reply.status_code == 200
    chosen = reply.json()["id"]
    endpoint = f"/v1/connectors/{chosen}/sync"
    read = client.post(endpoint, json={})
    assert read.status_code == 200 and read.json()["episodes_added"] == 1
    assert connection(driver, seeded_world, chosen)["sync_cursor"] == first
    before = counts(driver, seeded_world)
    assert client.post(endpoint, json={}).json()["episodes_added"] == 0
    assert counts(driver, seeded_world) == before
    second = commit("Second commit", [first], at="2026-10-02T08:00:00Z")
    assert client.post(endpoint, json={}).json()["episodes_added"] == 1
    replacement = commit("Replacement commit", [first], at="2026-10-03T08:00:00Z")
    assert client.post(endpoint, json={}).json()["episodes_added"] == 1
    assert counts(driver, seeded_world)["episodes"] == 3
    rows = WorldQuery(driver, seeded_world).read(
        "MATCH (s:Source) RETURN s.content AS content, s.parser_version AS parser"
    )
    messages = [json.loads(row["content"])[0] for row in rows]
    commits = {json.loads(message["text"])["commit"] for message in messages}
    assert commits == {first, second, replacement}
    assert all(row["parser"] == "git_clone.v1" for row in rows)
    assert all(message["speaker_kind"] == "third_party" for message in messages)


def test_git_repository_alias_is_world_scoped_and_path_input_is_refused(
    seeded_world, client_for, deps, repository, make_world
):
    path, _, commit = repository
    commit()
    client = client_for(seeded_world)
    deps.repositories[(make_world(), "sample")] = path
    assert client.post("/v1/connectors/git", json={"repository": "sample"}).status_code == 404
    assert client.post("/v1/connectors/git", json={"repository": str(path)}).status_code == 400


def test_git_failed_read_preserves_cursor_and_success_time(
    driver, seeded_world, client_for, deps, repository
):
    path, _, commit = repository
    commit()
    deps.repositories[(seeded_world, "sample")] = path
    client = client_for(seeded_world)
    chosen = client.post("/v1/connectors/git", json={"repository": "sample"}).json()["id"]
    endpoint = f"/v1/connectors/{chosen}/sync"
    assert client.post(endpoint, json={}).status_code == 200
    before = connection(driver, seeded_world, chosen)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/absent\n")
    assert client.post(endpoint, json={}).status_code == 503
    after = connection(driver, seeded_world, chosen)
    assert after["sync_cursor"] == before["sync_cursor"]
    assert after["last_successful_read"] == before["last_successful_read"]
    assert after["last_outcome"] == "failed" and after["failed_count"] == 1
