"""Delete my Grove: the route, the writer and the restore, on synthetic t_ worlds only.

Every person signs in through the real email code flow against the real store, and Pipedream is a
fake behind httpx.MockTransport. Every name and address is invented.
"""

import importlib.util
import json
import stat
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api import agent_files, pipedream, uploads
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.world_activity import world_deleting, world_write
from grove.ids import new_id
from grove.store.catalogue import WORLD_LABELS
from grove.store.connection import DATABASE
from grove.store.query import WorldQuery
from grove.views.session import SESSION_COOKIE, ViewSession, token_hash
from grove.writer.accounts import create_session, email_hash, ensure_demo_account
from grove.writer.forget_ledger import (
    LedgerUnavailable,
    _name,
    append_intent,
    checked_under_lock,
    locked_ledger,
    read_ledger,
)
from grove.writer.restore import load_snapshot, restore, restore_loaded, take_snapshot
from grove.writer.testing import plant_link, plant_node, purge_test_account, purge_test_world
from grove.writer.tx import WorldWriter
from tests.auth.conftest import Accounts, CaptureMailer
from tests.ingest.conftest import make_screener
from tests.ingest.test_upload_api import JSON_TYPE, UPLOAD_PATH, export_of
from tests.writer.helpers import raw, world_dump
from tests.writer.test_restore import purge_world_rows

BASE = "https://grove.example"
ROUTE = "/v1/world/delete"
SESSION_VIEW = "/v1/views/session"
SAME_SITE = {"Origin": BASE}
CONFIRM = {"confirm": "DELETE"}
DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
PLATFORM_ROWS = ("OAuthGrant", "OAuthToken", "AuthCode", "UploadToken")


def writer():
    """Read at use, so that a test that needs the writer fails by itself before the writer lands."""
    from grove.writer import world_delete

    return world_delete


class FakePipedream:
    """Accounts by id. `fail` names the method that answers 500; it stays until a test clears it."""

    def __init__(self) -> None:
        self.accounts: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: str | None = None
        self.expected_user: str | None = None

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.method == self.fail:
            return httpx.Response(500)
        if request.method == "DELETE":
            del self.accounts[request.url.path.rsplit("/", 1)[1]]
            return httpx.Response(204)
        assert request.url.path == "/v1/connect/proj_test/accounts"
        assert request.url.params["external_user_id"] == self.expected_user
        assert request.url.params["include_credentials"] == "false"
        wanted = request.url.params.get("app")
        rows = [
            {"id": key, "app": {"name_slug": slug}}
            for key, slug in self.accounts.items()
            if wanted in (None, slug)
        ]
        info = {"count": len(rows), "total_count": len(rows), "end_cursor": None}
        return httpx.Response(200, json={"data": rows, "page_info": info})

    async def http(self):
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(self.transport)
        ) as client:
            yield client


@dataclass
class Person:
    client: TestClient
    address: str
    user_id: str
    world: str
    folder: Path

    @property
    def cookie(self) -> str:
        return self.client.cookies.get(SESSION_COOKIE)

    def delete(self, **kwargs):
        kwargs.setdefault("json", CONFIRM)
        kwargs.setdefault("headers", SAME_SITE)
        return self.client.post(ROUTE, **kwargs)


@dataclass
class Stack:
    app: object
    driver: object
    mailer: CaptureMailer
    accounts: Accounts
    fake: FakePipedream
    ledger: Path
    files: Path

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=BASE, raise_server_exceptions=False)

    def sign_in(self, client: TestClient, address: str) -> dict:
        assert client.post("/v1/auth/email/start", json={"email": address}).status_code == 200
        code = self.mailer.sent[-1][1]
        answer = client.post("/v1/auth/email/verify", json={"email": address, "code": code})
        assert answer.status_code == 200
        return answer.json()

    def person(self) -> Person:
        client = self.client()
        address = self.accounts.address()
        self.sign_in(client, address)
        rows = raw(
            self.driver,
            "MATCH (u:User {email_hash: $hash}) MATCH (w:World {owner_user_id: u.id}) "
            "RETURN u.id AS user_id, w.id AS world",
            hash=email_hash(address),
        )
        user_id, world = rows[0]["user_id"], rows[0]["world"]
        folder = self.files / world
        (folder / "notes").mkdir(parents=True)
        (folder / "plan.pdf").write_bytes(b"synthetic file")
        (folder / "notes" / "idea.md").write_text("synthetic note")
        return Person(client, address, user_id, world, folder)

    def use_fake_for(self, person: Person) -> None:
        self.fake.expected_user = pipedream.external_user_id(
            ViewSession(person.user_id, person.world, None)
        )


@pytest.fixture
def stack(driver, migrated, monkeypatch, tmp_path):
    ledger = tmp_path / "ledger"
    files = tmp_path / "worlds"
    files.mkdir()
    monkeypatch.setenv("GROVE_FORGET_LEDGER_DIR", str(ledger))
    monkeypatch.setenv("GROVE_AGENT_FILES_DIR", str(files))
    for key in ("PROJECT_ID", "CLIENT_ID", "CLIENT_SECRET", "ENVIRONMENT"):
        monkeypatch.delenv("PIPEDREAM_" + key, raising=False)
    made, mailer, fake = Accounts(), CaptureMailer(), FakePipedream()
    app = create_app(
        ApiSettings(public_base_url=BASE),
        driver=driver,
        mailer=mailer,
        world_id_for=made.world_id_for,
    )
    app.dependency_overrides[pipedream.http_client] = fake.http
    yield Stack(app, driver, mailer, made, fake, ledger, files)
    for world in made.worlds:
        purge_test_world(driver, world)
    for hashed in made.hashes:
        purge_test_account(driver, hashed)


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "PROJECT_ID": "proj_test",
        "CLIENT_ID": "test-client",
        "CLIENT_SECRET": "test-secret",
        "ENVIRONMENT": "production",
    }.items():
        monkeypatch.setenv("PIPEDREAM_" + key, value)


def write(driver, cypher: str, **params) -> None:
    with driver.session(database=DATABASE) as session:
        session.run("CYPHER 25 " + cypher, **params).consume()


def fill(driver, person: Person) -> dict[str, list[str]]:
    """One row of every world label, a link, and the platform rows that name this world."""
    world = person.world
    planted = {label: [plant_node(driver, world, label)["id"]] for label in sorted(WORLD_LABELS)}
    second = plant_node(driver, world, "Item")["id"]
    plant_link(driver, world, planted["Item"][0], second)
    planted["Item"].append(second)
    for label in PLATFORM_ROWS:
        row_id = new_id()
        write(
            driver,
            f"CREATE (n:{label}) SET n = $row",
            row={
                "id": row_id,
                "world_id": world,
                "user_id": person.user_id,
                "client_id": "client-" + row_id,
                "token_hash": "hash-" + row_id,
                "code_hash": "hash-" + row_id,
            },
        )
        planted[label] = [row_id]
    return planted


def session_count(driver, user_id: str) -> int:
    rows = raw(driver, "MATCH (s:Session {user_id: $u}) RETURN count(s) AS c", u=user_id)
    return rows[0]["c"]


def label_counts(driver, world: str) -> Counter:
    rows = raw(
        driver,
        "MATCH (n {world_id: $w}) UNWIND labels(n) AS label RETURN label, count(*) AS c",
        w=world,
    )
    return Counter({row["label"]: row["c"] for row in rows})


def tree(folder: Path) -> list[str]:
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


def ledger_rows(stack: Stack, world: str) -> list[dict]:
    files = list(stack.ledger.glob("*.json")) if stack.ledger.exists() else []
    rows = [row for path in files for row in json.loads(path.read_text())["rows"]]
    return [row for row in rows if row.get("world_id") == world]


def assert_untouched(stack: Stack, person: Person, dump: str, files: list[str]) -> None:
    assert world_dump(stack.driver, person.world) == dump
    assert tree(person.folder) == files


# The route ---------------------------------------------------------------------------------------


def test_the_route_removes_every_row_of_the_caller_and_none_of_another(stack):
    alice, bob = stack.person(), stack.person()
    planted = fill(stack.driver, alice)
    fill(stack.driver, bob)
    bob_dump, bob_files = world_dump(stack.driver, bob.world), tree(bob.folder)
    bob_sessions = session_count(stack.driver, bob.user_id)
    fresh = label_counts(stack.driver, stack.person().world)

    response = alice.delete()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    today = clock.now().date()
    assert response.json() == {
        "deleted": True,
        "backups_clear_after": (today + timedelta(days=30)).isoformat(),
        "signed_in": True,
    }
    left = {
        label: raw(
            stack.driver,
            f"MATCH (n:{label}) WHERE n.id IN $ids RETURN count(n) AS c",
            ids=ids,
        )[0]["c"]
        for label, ids in planted.items()
    }
    assert set(left.values()) == {0}
    assert raw(stack.driver, "MATCH ()-[l:LINK {world_id: $w}]->() RETURN l", w=alice.world) == []
    assert label_counts(stack.driver, alice.world) == fresh
    assert world_dump(stack.driver, bob.world) == bob_dump
    assert tree(bob.folder) == bob_files
    assert session_count(stack.driver, bob.user_id) == bob_sessions
    assert bob.client.get(SESSION_VIEW).status_code == 200


def test_the_new_world_is_empty_and_the_person_stays_signed_in(stack):
    alice = stack.person()
    fill(stack.driver, alice)
    old_cookie = alice.cookie
    other_device = stack.client()
    stack.sign_in(other_device, alice.address)
    user_rows = raw(
        stack.driver, "MATCH (u:User {id: $u}) RETURN properties(u) AS p", u=alice.user_id
    )

    assert alice.delete().status_code == 200

    new_cookie = alice.cookie
    assert new_cookie and new_cookie != old_cookie
    view = alice.client.get(SESSION_VIEW)
    assert view.status_code == 200
    held = view.json()
    assert held["account"] == alice.user_id
    assert held["has_items"] is False
    items = WorldQuery(stack.driver, alice.world).read("MATCH (i:Item) RETURN i.id AS id")
    assert [row["id"] for row in items] == [held["self_item_id"]]
    # The old cookie and every other device end; the login row stays as it was.
    stale = stack.client()
    stale.cookies.set(SESSION_COOKIE, old_cookie)
    assert stale.get(SESSION_VIEW).status_code == 401
    assert other_device.get(SESSION_VIEW).status_code == 401
    assert session_count(stack.driver, alice.user_id) == 1
    assert (
        raw(stack.driver, "MATCH (u:User {id: $u}) RETURN properties(u) AS p", u=alice.user_id)
        == user_rows
    )
    # The same address signs in again, to the same empty world.
    again = stack.sign_in(other_device, alice.address)
    assert again["self_item_id"] == held["self_item_id"]
    assert session_count(stack.driver, alice.user_id) == 2


def test_a_second_delete_of_the_new_world_works(stack):
    alice = stack.person()
    assert alice.delete().status_code == 200
    plant_node(stack.driver, alice.world, "Hold")
    assert alice.delete().status_code == 200
    assert label_counts(stack.driver, alice.world)["Hold"] == 0
    assert alice.client.get(SESSION_VIEW).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"confirm": "delete"},
        {"confirm": True},
        {"confirm": "DELETE "},
        {},
        {"confirm": "DELETE", "world_id": "t_other"},
        ["DELETE"],
        "DELETE",
    ],
    ids=["lower", "true", "space", "empty", "extra", "list", "text"],
)
def test_a_wrong_confirmation_removes_nothing(stack, body):
    alice = stack.person()
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    cookie = alice.cookie

    response = alice.delete(json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert_untouched(stack, alice, dump, files)
    assert alice.cookie == cookie
    assert ledger_rows(stack, alice.world) == []


def test_only_a_json_body_counts(stack):
    alice = stack.person()
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    for kwargs in (
        {"json": None, "content": "confirm=DELETE", "headers": {**SAME_SITE}},
        {
            "json": None,
            "content": json.dumps(CONFIRM),
            "headers": {**SAME_SITE, "Content-Type": "text/plain"},
        },
        {
            "json": None,
            "content": b"x" * 5000,
            "headers": {**SAME_SITE, "Content-Type": "application/json"},
        },
    ):
        response = alice.delete(**kwargs)
        assert response.status_code in (400, 413)
    assert_untouched(stack, alice, dump, files)


def test_a_world_id_in_the_query_is_refused(stack):
    alice, bob = stack.person(), stack.person()
    dump = world_dump(stack.driver, bob.world)
    response = alice.client.post(
        ROUTE, params={"world_id": bob.world}, json=CONFIRM, headers=SAME_SITE
    )
    assert response.status_code == 400
    assert world_dump(stack.driver, bob.world) == dump


def test_no_session_removes_nothing(stack):
    alice = stack.person()
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    response = stack.client().post(ROUTE, json=CONFIRM, headers=SAME_SITE)
    assert response.status_code == 401
    assert_untouched(stack, alice, dump, files)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://outside.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
    ids=["origin", "null", "fetch-site"],
)
def test_a_foreign_origin_removes_nothing(stack, headers):
    alice = stack.person()
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    response = alice.delete(headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"
    assert_untouched(stack, alice, dump, files)
    assert ledger_rows(stack, alice.world) == []


@pytest.mark.parametrize(
    "value", ["Bearer anything", "Basic abc", ""], ids=["bearer", "basic", "e"]
)
@pytest.mark.parametrize("cookie", [True, False], ids=["cookie", "no-cookie"])
def test_any_authorization_header_is_refused(stack, value, cookie):
    alice = stack.person()
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    client = alice.client if cookie else stack.client()
    response = client.post(ROUTE, json=CONFIRM, headers={**SAME_SITE, "Authorization": value})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    assert_untouched(stack, alice, dump, files)


def test_a_demo_world_is_refused(stack, monkeypatch, configured):
    address = stack.accounts.address()
    hashed = email_hash(address)
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", hashed)
    account = ensure_demo_account(stack.driver, hashed, world_id_for=stack.accounts.world_id_for)
    # The stored designation refuses, also with the runtime setting gone.
    monkeypatch.delenv("GROVE_DEMO_EMAIL_HASH")
    token = "world-delete-test-" + uuid4().hex
    create_session(
        stack.driver,
        account.user_id,
        token_hash(token),
        method="email",
        expires_at=clock.now() + timedelta(minutes=5),
    )
    folder = stack.files / account.world_id
    folder.mkdir()
    (folder / "keep.txt").write_text("synthetic")
    dump = world_dump(stack.driver, account.world_id)
    client = stack.client()
    client.cookies.set(SESSION_COOKIE, token)

    response = client.post(ROUTE, json=CONFIRM, headers=SAME_SITE)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    assert world_dump(stack.driver, account.world_id) == dump
    assert tree(folder) == ["keep.txt"]
    assert stack.fake.calls == []
    assert ledger_rows(stack, account.world_id) == []


def unset(stack, monkeypatch):
    monkeypatch.delenv("GROVE_FORGET_LEDGER_DIR")


def relative(stack, monkeypatch):
    monkeypatch.setenv("GROVE_FORGET_LEDGER_DIR", "ledger")


def shared(stack, monkeypatch):
    stack.ledger.mkdir(mode=0o755)
    stack.ledger.chmod(0o755)


def linked(stack, monkeypatch):
    target = stack.ledger.parent / "real-ledger"
    target.mkdir(mode=0o700)
    stack.ledger.symlink_to(target)


def corrupt(stack, monkeypatch):
    stack.ledger.mkdir(mode=0o700)


@pytest.mark.parametrize(
    "spoil", [unset, relative, shared, linked, corrupt], ids=lambda spoil: spoil.__name__
)
def test_a_missing_or_unsafe_ledger_removes_nothing(stack, monkeypatch, configured, spoil):
    alice = stack.person()
    stack.use_fake_for(alice)
    stack.fake.accounts["apn_gmail1"] = "gmail"
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    spoil(stack, monkeypatch)
    if spoil is corrupt:
        (stack.ledger / _name(alice.world)).write_text("{}")
        (stack.ledger / _name(alice.world)).chmod(0o600)

    response = alice.delete()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert_untouched(stack, alice, dump, files)
    assert stack.fake.accounts == {"apn_gmail1": "gmail"}
    assert stack.fake.calls == []


# Pipedream ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["GET", "DELETE"])
def test_a_pipedream_failure_removes_nothing_and_a_retry_finishes(stack, configured, failing):
    alice = stack.person()
    stack.use_fake_for(alice)
    stack.fake.accounts.update({"apn_gmail1": "gmail", "apn_slack1": "slack_v2"})
    stack.fake.fail = failing
    plant_node(stack.driver, alice.world, "Hold")
    dump, files = world_dump(stack.driver, alice.world), tree(alice.folder)
    cookie = alice.cookie

    failed = alice.delete()

    assert failed.status_code == 502
    assert failed.json()["error"]["retryable"] is True
    assert_untouched(stack, alice, dump, files)
    assert alice.cookie == cookie
    assert alice.client.get(SESSION_VIEW).status_code == 200

    stack.fake.fail = None
    done = alice.delete()

    assert done.status_code == 200
    assert stack.fake.accounts == {}
    assert label_counts(stack.driver, alice.world)["Hold"] == 0
    assert not alice.folder.exists()


def test_every_account_goes_whatever_its_app_and_the_list_is_read_again(stack, configured):
    alice = stack.person()
    stack.use_fake_for(alice)
    # Airtable is no fixed row: the catalog search connects any app.
    stack.fake.accounts.update({"apn_gmail1": "gmail", "apn_air1": "airtable"})
    assert alice.delete().status_code == 200
    assert stack.fake.accounts == {}
    assert ("DELETE", "/v1/connect/proj_test/accounts/apn_air1") in stack.fake.calls
    listed = [call for call in stack.fake.calls if call[0] == "GET"]
    assert len(listed) == 2


def test_with_no_pipedream_settings_the_delete_makes_no_call(stack):
    alice = stack.person()
    assert alice.delete().status_code == 200
    assert stack.fake.calls == []


# Files -------------------------------------------------------------------------------------------


def test_the_folder_goes_and_a_link_inside_it_is_unlinked_with_its_target_kept(stack):
    alice, bob = stack.person(), stack.person()
    outside_file = stack.files.parent / "outside.txt"
    outside_file.write_text("synthetic, outside the root")
    outside_folder = stack.files.parent / "outside"
    outside_folder.mkdir()
    (outside_folder / "kept.txt").write_text("synthetic")
    (alice.folder / "to-file").symlink_to(outside_file)
    (alice.folder / "notes" / "to-folder").symlink_to(outside_folder)
    (alice.folder / "to-bob").symlink_to(bob.folder)
    bob_files = tree(bob.folder)

    assert alice.delete().status_code == 200

    assert not alice.folder.exists()
    assert not alice.folder.is_symlink()
    assert outside_file.read_text() == "synthetic, outside the root"
    assert tree(outside_folder) == ["kept.txt"]
    assert tree(bob.folder) == bob_files


def test_an_upload_that_lands_during_the_store_step_goes_too(stack, monkeypatch):
    """The new world has the same id, so a file left here would be in the new world's folder."""
    alice = stack.person()
    module = writer()
    real = module._remove_rows

    def land_a_file_first(driver, world_id):
        alice.folder.mkdir()
        (alice.folder / "late.pdf").write_bytes(b"synthetic late upload")
        real(driver, world_id)

    monkeypatch.setattr(module, "_remove_rows", land_a_file_first)

    assert alice.delete().status_code == 200
    assert not alice.folder.exists()


def test_a_world_folder_that_is_a_link_is_unlinked_and_its_target_stays(stack):
    alice = stack.person()
    elsewhere = stack.files.parent / "elsewhere"
    alice.folder.rename(elsewhere)
    alice.folder.symlink_to(elsewhere)

    assert alice.delete().status_code == 200

    assert not alice.folder.is_symlink()
    assert (elsewhere / "plan.pdf").read_bytes() == b"synthetic file"


def test_with_no_files_setting_the_files_step_is_skipped(stack, monkeypatch):
    alice = stack.person()
    monkeypatch.delenv("GROVE_AGENT_FILES_DIR")
    plant_node(stack.driver, alice.world, "Hold")
    assert alice.delete().status_code == 200
    assert label_counts(stack.driver, alice.world)["Hold"] == 0
    assert alice.folder.exists()


def test_a_files_failure_removes_no_row_and_a_retry_finishes(stack, monkeypatch):
    alice = stack.person()
    plant_node(stack.driver, alice.world, "Hold")
    dump = world_dump(stack.driver, alice.world)
    alice.folder.chmod(0o500)
    try:
        response = alice.delete()
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True
        assert world_dump(stack.driver, alice.world) == dump
    finally:
        alice.folder.chmod(0o700)
    assert alice.delete().status_code == 200
    assert not alice.folder.exists()


def test_a_files_root_that_is_gone_refuses_rather_than_reports_the_files_removed(
    stack, monkeypatch
):
    alice = stack.person()
    dump = world_dump(stack.driver, alice.world)
    monkeypatch.setenv("GROVE_AGENT_FILES_DIR", str(stack.files.parent / "absent"))
    assert alice.delete().status_code == 503
    assert world_dump(stack.driver, alice.world) == dump


# The store ---------------------------------------------------------------------------------------


def test_the_label_list_is_only_the_fast_path(stack, monkeypatch):
    """A row of a label nobody listed still goes: the last pass names no label."""
    alice = stack.person()
    plant_node(stack.driver, alice.world, "Hold")
    monkeypatch.setattr(writer(), "_LABELS", ())
    assert alice.delete().status_code == 200
    assert label_counts(stack.driver, alice.world)["Hold"] == 0


def test_rows_go_in_batches(stack, monkeypatch):
    alice = stack.person()
    for _ in range(5):
        plant_node(stack.driver, alice.world, "Hold")
    monkeypatch.setattr(writer(), "_BATCH", 2)
    assert alice.delete().status_code == 200
    assert label_counts(stack.driver, alice.world)["Hold"] == 0


def test_a_store_failure_part_way_keeps_the_session_and_a_retry_finishes(stack, monkeypatch):
    alice = stack.person()
    for _ in range(5):
        plant_node(stack.driver, alice.world, "Hold")
    module = writer()
    monkeypatch.setattr(module, "_BATCH", 2)
    real, calls = module._remove_batch, []

    def fail_on_second(driver, statement, world_id):
        calls.append(statement)
        if len(calls) == 2:
            raise ServiceUnavailable("synthetic store failure")
        return real(driver, statement, world_id)

    monkeypatch.setattr(module, "_remove_batch", fail_on_second)

    failed = alice.delete()

    assert failed.status_code == 503
    assert failed.json()["error"]["retryable"] is True
    assert raw(stack.driver, "MATCH (w:World {id: $w}) RETURN w.id AS id", w=alice.world)
    assert alice.client.get(SESSION_VIEW).status_code == 200

    monkeypatch.setattr(module, "_remove_batch", real)
    assert alice.delete().status_code == 200
    assert label_counts(stack.driver, alice.world)["Hold"] == 0
    assert alice.client.get(SESSION_VIEW).status_code == 200


def test_a_writer_waits_for_the_whole_delete(stack, monkeypatch):
    """The lock stays held from the first batch to the commit, so no pass writes between them."""
    alice = stack.person()
    plant_node(stack.driver, alice.world, "Hold")
    module = writer()
    real = module._remove_rows
    outcome: dict = {}
    started = threading.Event()

    def attempt():
        def work(tx):
            started.set()
            tx.run("CREATE (n:Hold) SET n.id = $id", id="late-" + uuid4().hex)

        try:
            WorldWriter(stack.driver, alice.world, timeout=20.0).transaction(work)
            outcome["result"] = "committed"
        except Exception as error:  # noqa: BLE001 - the test reads only that it did not commit
            outcome["result"] = type(error).__name__

    thread = threading.Thread(target=attempt)

    def remove_with_a_writer_waiting(driver, world_id):
        thread.start()
        # The writer takes the lock before its work, so `started` proves it passed the lock.
        assert not started.wait(1.5)
        real(driver, world_id)

    monkeypatch.setattr(module, "_remove_rows", remove_with_a_writer_waiting)

    assert alice.delete().status_code == 200
    thread.join(30)
    assert not thread.is_alive()
    late = raw(
        stack.driver,
        "MATCH (n:Hold {world_id: $w}) WHERE n.id STARTS WITH 'late-' RETURN n.id AS id",
        w=alice.world,
    )
    assert label_counts(stack.driver, alice.world)["Hold"] == len(late)


def test_the_writer_refuses_a_world_of_another_owner(stack):
    alice, bob = stack.person(), stack.person()
    dump = world_dump(stack.driver, alice.world)
    module = writer()
    with pytest.raises(module.ErasureRefused):
        module.begin_erasure(stack.driver, alice.world, bob.user_id)
    assert world_dump(stack.driver, alice.world) == dump
    assert ledger_rows(stack, alice.world) == []


# The ledger --------------------------------------------------------------------------------------


def test_the_ledger_holds_ids_and_times_only_and_comes_before_the_files(
    stack, configured, monkeypatch
):
    alice = stack.person()
    stack.use_fake_for(alice)

    def no_files(world_id):
        raise writer().FilesUnavailable("the files folder is not available")

    with monkeypatch.context() as patched:
        patched.setattr("grove.api.world_delete.remove_world_files", no_files)
        assert alice.delete().status_code == 503
    (intent,) = ledger_rows(stack, alice.world)
    assert set(intent) == {
        "id",
        "target_type",
        "state",
        "world_id",
        "user_id",
        "erased_at",
        "backups_clear_after",
    }
    assert intent["target_type"] == "world"
    assert intent["state"] == "started"
    assert intent["user_id"] == alice.user_id
    erased = datetime.fromisoformat(intent["erased_at"])
    assert date.fromisoformat(intent["backups_clear_after"]) == erased.date() + timedelta(days=30)

    response = alice.delete()

    assert response.status_code == 200
    rows = ledger_rows(stack, alice.world)
    assert [row["state"] for row in rows] == ["started", "started", "finished"]
    assert response.json()["backups_clear_after"] == rows[-1]["backups_clear_after"]
    info = stack.ledger.stat()
    assert stat.S_IMODE(info.st_mode) == 0o700
    (path,) = stack.ledger.glob("*.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert alice.address not in path.read_text()


def test_the_deploy_preflight_accepts_a_journal_with_an_erasure(stack):
    """The preflight refuses any entry that is not <sha256>.json in the known envelope."""
    alice = stack.person()
    assert alice.delete().status_code == 200
    spec = importlib.util.spec_from_file_location(
        "materials_preflight", DEPLOY / "materials_preflight.py"
    )
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    names = sorted(path.name for path in stack.ledger.iterdir())
    assert len(names) == 2 and names[0] == ".lock"
    assert preflight.envelope((stack.ledger / names[1]).read_bytes(), names[1]) == 2


def plant_forget(stack: Stack, person: Person) -> dict:
    """A D14 intent in both homes, as a forget of the old world leaves it."""
    row = {
        "id": new_id(),
        "world_id": person.world,
        "target_type": "item",
        "internal_ids": [],
        "requested_by": person.user_id,
        "approved_at": clock.now().isoformat(),
    }
    with locked_ledger(stack.ledger) as root:
        append_intent(root, person.world, row, [])
    plant_node(stack.driver, person.world, "ForgetLedger", id=row["id"], target_type="item")
    return row


def write_valid_forget(stack: Stack, person: Person) -> None:
    """A real forget of one item, so that the snapshot passes every validation."""
    from grove.writer.forget import forget
    from grove.writer.forget_approvals import mint_approval

    item = plant_node(stack.driver, person.world, "Item", title="Cobalt orchard")
    target = {"type": "item", "item_id": item["id"]}
    token = mint_approval(stack.driver, person.world, person.user_id, target).token
    forget(
        stack.driver,
        person.world,
        target,
        approval_token=token,
        actor=person.user_id,
        ledger_dir=stack.ledger,
    )


def store_ledger(stack: Stack, person: Person) -> list[dict]:
    rows = WorldQuery(stack.driver, person.world).read("MATCH (f:ForgetLedger) RETURN f")
    return [row["f"] for row in rows]


def test_a_finished_erasure_ends_the_old_forget_rows_for_the_new_world(stack):
    """Material work compares the journal with the store: the new world must pass that check."""
    alice = stack.person()
    plant_forget(stack, alice)

    assert alice.delete().status_code == 200

    assert store_ledger(stack, alice) == []
    assert read_ledger(stack.ledger, alice.world) == []
    with locked_ledger(stack.ledger) as root:
        assert checked_under_lock(root, alice.world, []) == []
    # The earlier intent is never rewritten away.
    assert len(ledger_rows(stack, alice.world)) == 3


def test_a_started_erasure_leaves_the_forget_rows_of_the_standing_world(stack, configured):
    alice = stack.person()
    forgotten = plant_forget(stack, alice)
    stack.use_fake_for(alice)
    stack.fake.fail = "GET"

    assert alice.delete().status_code == 502

    assert [row["id"] for row in read_ledger(stack.ledger, alice.world)] == [forgotten["id"]]


# The restore -------------------------------------------------------------------------------------


def world_gone(stack: Stack, world: str) -> bool:
    left = raw(stack.driver, "MATCH (w:World {id: $w}) RETURN count(w) AS c", w=world)[0]["c"]
    return left == 0 and sum(label_counts(stack.driver, world).values()) == 0


def snapshot_of(stack: Stack, person: Person) -> dict:
    return json.loads(json.dumps(take_snapshot(stack.driver, person.world)))


def test_the_snapshot_says_when_it_was_taken(stack):
    alice = stack.person()
    before = clock.now()
    taken = datetime.fromisoformat(take_snapshot(stack.driver, alice.world)["taken_at"])
    assert before <= taken <= clock.now()


def test_a_loaded_dump_older_than_the_erasure_does_not_bring_the_world_back(stack):
    alice = stack.person()
    snapshot = snapshot_of(stack, alice)
    assert alice.delete().status_code == 200
    # The dump replaces the store: the new world goes, and the old rows and sessions are back.
    purge_world_rows(stack.driver, alice.world)
    load_snapshot(stack.driver, alice.world, snapshot)
    assert sum(label_counts(stack.driver, alice.world).values()) > 0

    result = restore_loaded(stack.driver, alice.world, snapshot, [], ledger_dir=stack.ledger)

    assert result["erased"] is True
    assert world_gone(stack, alice.world)
    assert session_count(stack.driver, alice.user_id) == 0
    assert raw(stack.driver, "MATCH (u:User {id: $u}) RETURN u.id AS id", u=alice.user_id)


def test_a_logical_restore_older_than_the_erasure_leaves_nothing(stack):
    alice = stack.person()
    snapshot = snapshot_of(stack, alice)
    assert alice.delete().status_code == 200
    purge_world_rows(stack.driver, alice.world)

    result = restore(stack.driver, alice.world, snapshot, [], ledger_dir=stack.ledger)

    assert result["erased"] is True
    assert world_gone(stack, alice.world)


def test_a_loaded_dump_with_old_forget_rows_leaves_nothing(stack):
    """A dump holds :ForgetLedger rows, and the journal's standing rows end at the erasure. That
    must read as a deleted world, not as a ledger that lost a row."""
    alice = stack.person()
    write_valid_forget(stack, alice)
    snapshot = snapshot_of(stack, alice)
    held = store_ledger(stack, alice)
    assert held
    assert alice.delete().status_code == 200
    purge_world_rows(stack.driver, alice.world)
    load_snapshot(stack.driver, alice.world, snapshot)
    for row in held:
        kept = {key: value for key, value in row.items() if key != "world_id"}
        plant_node(stack.driver, alice.world, "ForgetLedger", **kept)

    result = restore_loaded(
        stack.driver,
        alice.world,
        snapshot,
        read_ledger(stack.ledger, alice.world),
        ledger_dir=stack.ledger,
    )

    assert result["erased"] is True
    assert world_gone(stack, alice.world)


def test_a_snapshot_newer_than_the_erasure_restores_the_new_world(stack):
    alice = stack.person()
    assert alice.delete().status_code == 200
    snapshot = snapshot_of(stack, alice)
    expected = label_counts(stack.driver, alice.world)
    purge_world_rows(stack.driver, alice.world)

    result = restore(
        stack.driver,
        alice.world,
        snapshot,
        read_ledger(stack.ledger, alice.world),
        ledger_dir=stack.ledger,
    )

    assert "erased" not in result
    assert result["fingerprint"] == snapshot["fingerprint"]["fingerprint"]
    assert label_counts(stack.driver, alice.world) == expected
    assert session_count(stack.driver, alice.user_id) == 1


def test_a_dump_taken_while_the_delete_ran_is_erased_too(stack):
    """The first record is older than such a dump. The finished record, written when the rows are
    gone, is newer, so what the dump caught does not come back."""
    alice = stack.person()
    module = writer()
    module.begin_erasure(stack.driver, alice.world, alice.user_id)
    mid_delete = clock.now()
    plant_node(stack.driver, alice.world, "Hold")
    module.erase_world_rows(stack.driver, alice.world, alice.user_id)
    write(stack.driver, "CREATE (w:World {id: $w, read_generation: 0})", w=alice.world)
    plant_node(stack.driver, alice.world, "Hold")

    erased = module.replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=mid_delete)

    assert erased == [alice.world]
    assert world_gone(stack, alice.world)


def test_a_failed_pipedream_call_leaves_no_record_that_a_restore_would_act_on(stack, configured):
    """The page says the Grove was not removed, and the person goes on using it."""
    alice = stack.person()
    stack.use_fake_for(alice)
    before = clock.now()
    stack.fake.fail = "GET"
    assert alice.delete().status_code == 502

    assert (
        writer().replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=before) == []
    )
    assert sum(label_counts(stack.driver, alice.world).values()) > 0


def test_an_intent_with_no_finish_still_erases_an_older_snapshot(stack, monkeypatch):
    """As with D14: once removing began, the intent is applied at recovery, finished or not."""
    alice = stack.person()
    before = clock.now()
    module = writer()

    def no_files(world_id):
        raise module.FilesUnavailable("the files folder is not available")

    monkeypatch.setattr("grove.api.world_delete.remove_world_files", no_files)
    assert alice.delete().status_code == 503

    assert (
        module.replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=clock.now())
        == []
    )
    assert sum(label_counts(stack.driver, alice.world).values()) > 0
    assert module.replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=before) == [
        alice.world
    ]
    assert world_gone(stack, alice.world)


def test_a_running_upload_holds_the_delete_back(stack, monkeypatch):
    """The new world keeps the old id, so the upload would land the old chats in the new world."""
    alice = stack.person()
    other = stack.client()
    other.cookies.set(SESSION_COOKIE, alice.cookie)
    monkeypatch.setattr(uploads, "screener_for_upload", lambda world: make_screener())
    real, seen = uploads._spool_body, {}

    async def spool_then_delete(request, cap):
        result = await real(request, cap)
        seen["answer"] = await run_in_threadpool(
            lambda: other.post(ROUTE, json=CONFIRM, headers=SAME_SITE)
        )
        return result

    monkeypatch.setattr(uploads, "_spool_body", spool_then_delete)
    taken = alice.client.post(UPLOAD_PATH, content=export_of(), headers=JSON_TYPE)
    monkeypatch.setattr(uploads, "_spool_body", real)

    assert taken.status_code == 200
    assert seen["answer"].status_code == 409
    assert seen["answer"].json()["error"]["retryable"] is True
    assert alice.folder.exists()
    held = "MATCH (s:Source) WHERE s.content CONTAINS 'greenhouse' RETURN s.id AS id"
    assert WorldQuery(stack.driver, alice.world).read(held)

    assert alice.delete().status_code == 200
    assert not WorldQuery(stack.driver, alice.world).read(held)


def test_nothing_of_the_old_world_starts_while_the_delete_runs(stack):
    alice = stack.person()
    with world_deleting(alice.world):
        upload = alice.client.post(UPLOAD_PATH, content=export_of(), headers=JSON_TYPE)
        note = alice.client.post(
            agent_files.PREFIX + "/notes", json={"title": "Late", "body": "Late text"}
        )
        added = alice.client.post(
            agent_files.PREFIX + "?name=late.txt",
            content=b"late",
            headers={"content-type": agent_files.FILE_TYPE},
        )
    assert [upload.status_code, note.status_code, added.status_code] == [409, 409, 409]
    assert not (alice.folder / "late.txt").exists()


def test_a_second_delete_waits_for_the_first(stack):
    alice = stack.person()
    with world_deleting(alice.world):
        assert alice.delete().status_code == 409
    assert alice.folder.exists()


def test_a_guard_on_one_world_leaves_the_others_alone(stack):
    alice, bob = stack.person(), stack.person()
    with world_write(alice.world):
        assert bob.delete().status_code == 200
    with world_deleting(alice.world):
        answer = bob.client.post(
            agent_files.PREFIX + "/notes", json={"title": "Mine", "body": "My text"}
        )
    assert answer.status_code == 200


@pytest.mark.parametrize("route", ["upload", "note", "file"])
def test_a_request_that_lost_the_race_to_the_delete_stops(stack, monkeypatch, route):
    """The caller was checked, then the delete ended, then the guard was free. It looks again."""
    alice = stack.person()
    module = uploads if route == "upload" else agent_files
    real = module.world_write

    def delete_finished_first(world_id):
        write(stack.driver, "MATCH (s:Session {user_id: $id}) DETACH DELETE s", id=alice.user_id)
        return real(world_id)

    monkeypatch.setattr(module, "world_write", delete_finished_first)
    if route == "upload":
        answer = alice.client.post(UPLOAD_PATH, content=export_of(), headers=JSON_TYPE)
    elif route == "note":
        answer = alice.client.post(
            agent_files.PREFIX + "/notes", json={"title": "Late", "body": "Late text"}
        )
    else:
        answer = alice.client.post(
            agent_files.PREFIX + "?name=late.txt",
            content=b"late",
            headers={"content-type": agent_files.FILE_TYPE},
        )
    assert answer.status_code == 401
    assert not (alice.folder / "late.txt").exists()


def test_a_ledger_failure_past_the_first_step_never_says_nothing_went(stack, monkeypatch):
    """Files and rows may be gone by then, so the page must offer a retry."""
    alice = stack.person()
    real = writer().erase_world_rows

    def ledger_down(driver, world_id, user_id, **kwargs):
        raise LedgerUnavailable("the disk is full")

    monkeypatch.setattr("grove.api.world_delete.erase_world_rows", ledger_down)
    failed = alice.delete()
    monkeypatch.setattr("grove.api.world_delete.erase_world_rows", real)

    assert failed.status_code == 503
    assert failed.json()["error"]["retryable"] is True
    assert alice.delete().status_code == 200


def test_the_replay_covers_every_erased_world_and_only_them(stack):
    alice, bob, carol = stack.person(), stack.person(), stack.person()
    taken = clock.now()
    assert alice.delete().status_code == 200
    assert bob.delete().status_code == 200
    carol_dump = world_dump(stack.driver, carol.world)
    module = writer()

    erased = module.replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=taken)

    assert erased == sorted([alice.world, bob.world])
    assert world_gone(stack, alice.world) and world_gone(stack, bob.world)
    assert world_dump(stack.driver, carol.world) == carol_dump
    # A second replay finds nothing more to do.
    module.replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=taken)
    assert world_dump(stack.driver, carol.world) == carol_dump


def test_a_logical_restore_of_one_world_leaves_a_live_world_with_a_newer_erasure(stack):
    """A logical restore loads one world, so another world's rows are live, not restored."""
    alice, bob = stack.person(), stack.person()
    snapshot = snapshot_of(stack, alice)
    assert bob.delete().status_code == 200
    bob_dump = world_dump(stack.driver, bob.world)
    purge_world_rows(stack.driver, alice.world)

    result = restore(stack.driver, alice.world, snapshot, [], ledger_dir=stack.ledger)

    assert "erased" not in result
    assert world_dump(stack.driver, bob.world) == bob_dump


def test_a_snapshot_with_no_time_counts_as_older_than_every_erasure(stack):
    alice = stack.person()
    snapshot = snapshot_of(stack, alice)
    del snapshot["taken_at"]
    assert alice.delete().status_code == 200
    purge_world_rows(stack.driver, alice.world)

    result = restore(stack.driver, alice.world, snapshot, [], ledger_dir=stack.ledger)

    assert result["erased"] is True
    assert world_gone(stack, alice.world)


def test_a_restore_with_no_ledger_is_refused_before_it_loads(stack, monkeypatch):
    alice = stack.person()
    snapshot = snapshot_of(stack, alice)
    purge_world_rows(stack.driver, alice.world)
    monkeypatch.delenv("GROVE_FORGET_LEDGER_DIR")

    with pytest.raises(LedgerUnavailable):
        restore(stack.driver, alice.world, snapshot, [])

    assert sum(label_counts(stack.driver, alice.world).values()) == 0
    with pytest.raises(LedgerUnavailable):
        restore_loaded(stack.driver, alice.world, snapshot, [])


def test_a_damaged_erasure_row_stops_the_replay(stack):
    alice = stack.person()
    assert alice.delete().status_code == 200
    with locked_ledger(stack.ledger) as root:
        append_intent(
            root,
            alice.world,
            {"id": new_id(), "target_type": "world", "state": "started", "erased_at": "soon"},
            [],
        )
    dump = world_dump(stack.driver, alice.world)
    with pytest.raises(LedgerUnavailable):
        writer().replay_world_erasures(stack.driver, stack.ledger, snapshot_taken_at=clock.now())
    assert world_dump(stack.driver, alice.world) == dump


def test_the_command_replays_every_erasure_for_a_dump(stack, capsys):
    from grove.writer import restore as module

    alice = stack.person()
    taken = clock.now()
    assert alice.delete().status_code == 200

    code = module.main(["erasures", "--taken-at", taken.isoformat()])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"erased": 1}
    assert world_gone(stack, alice.world)


def test_the_command_reads_the_stamp_of_a_dump_name(stack, capsys):
    """deploy/backup.sh names a dump with date -u +%Y%m%dT%H%M%SZ."""
    from grove.writer import restore as module

    assert module.main(["erasures", "--taken-at", "20261105T033000Z"]) == 0
    assert json.loads(capsys.readouterr().out) == {"erased": 0}


def test_the_files_setting_names_the_folder_the_api_writes():
    from grove.api import agent_files

    assert writer().FILES_ENV == agent_files.FOLDER_VAR
