"""Logout, a second sign-in, two people, races, and a crash between the steps of a first sign-in."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from typing import Any

import pytest

from grove.api import auth as auth_module
from grove.store.connection import DATABASE
from grove.store.errors import STORE_UNAVAILABLE
from grove.store.query import WorldQuery
from grove.store.scope import check_world_id
from grove.views.session import token_hash
from grove.writer import accounts as accounts_module
from grove.writer import apply as apply_module
from grove.writer.accounts import (
    create_session,
    default_world_id,
    email_hash,
    ensure_account,
    own_title,
    revoke_session,
)
from grove.writer.errors import BatchDeclined, Refusal
from grove.writer.sources import record_source
from grove.writer.testing import plant_node, purge_test_account
from grove.writer.tx import WorldWriter
from grove.writer.worlds import create_world
from tests.auth.conftest import (
    LOGOUT,
    SESSION_VIEW,
    START,
    VERIFY,
    browser,
    codes_of,
    cookie_flags,
    email_sign_in,
    google_sign_in,
    items_of,
    sessions_of,
    user_of,
    world_counts,
    worlds_of,
)
from tests.writer.harness import wait_until_blocked
from tests.writer.helpers import apply as apply_batch_of
from tests.writer.helpers import create_item, created_id, make_source, observation


def _world_of(driver, account_id: str) -> dict:
    worlds = worlds_of(driver, account_id)
    assert len(worlds) == 1
    return worlds[0]


def test_logout_revokes_clears_and_keeps_every_row_of_the_world(client, driver, accounts, mailer):
    address = accounts.address()
    body = email_sign_in(client, mailer, address).json()
    world = _world_of(driver, body["account"])
    counts = world_counts(driver, world["id"])
    item = items_of(driver, world["id"])[0]
    assert all(counts[label] > 0 for label in ("Item", "Kind", "Evidence", "ChangeEntry"))

    response = client.post(LOGOUT, json={})
    assert response.status_code == 200
    assert response.json() == {"signed_in": False}
    assert response.headers["cache-control"] == "no-store"
    _, cleared = cookie_flags(response, "grove_session")
    assert "max-age=0" in cleared
    assert "secure" in cleared and "httponly" in cleared

    assert world_counts(driver, world["id"]) == counts
    after = items_of(driver, world["id"])[0]
    assert (after["id"], after["version"]) == (item["id"], item["version"])
    assert _world_of(driver, body["account"])["self_item_id"] == body["self_item_id"]
    assert sessions_of(driver, body["account"])[0]["revoked_at"] is not None

    assert client.get(SESSION_VIEW).status_code == 401
    assert client.get("/v1/views/graph").status_code == 401


def test_the_old_cookie_is_dead_after_logout(app, driver, accounts, mailer):
    address = accounts.address()
    client = browser(app)
    first = email_sign_in(client, mailer, address)
    token, _ = cookie_flags(first, "grove_session")
    client.post(LOGOUT, json={})
    stolen = browser(app)
    stolen.cookies.set("grove_session", token, domain="grove.example", path="/")
    assert stolen.get(SESSION_VIEW).status_code == 401


def test_logout_with_no_session_still_answers_and_clears(client):
    response = client.post(LOGOUT, json={})
    assert response.status_code == 200
    assert response.json() == {"signed_in": False}
    _, cleared = cookie_flags(response, "grove_session")
    assert "max-age=0" in cleared


def test_logout_with_the_store_down_clears_and_says_so(client, accounts, mailer, monkeypatch):
    email_sign_in(client, mailer, accounts.address())

    def down(*args, **kwargs):
        raise STORE_UNAVAILABLE[0]("the store went away")

    monkeypatch.setattr(auth_module, "revoke_session", down)
    response = client.post(LOGOUT, json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    _, cleared = cookie_flags(response, "grove_session")
    assert "max-age=0" in cleared
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("second_method", ["email", "google"])
def test_a_second_sign_in_resumes_the_same_world_and_own_item(
    second_method, client, driver, accounts, mailer, google
):
    address = accounts.address()
    first = email_sign_in(client, mailer, address).json()
    world = _world_of(driver, first["account"])
    counts = world_counts(driver, world["id"])
    client.post(LOGOUT, json={})

    if second_method == "email":
        second = email_sign_in(client, mailer, address).json()
    else:
        assert google_sign_in(client, google, address).status_code == 303
        second = client.get(SESSION_VIEW).json()

    assert second["account"] == first["account"]
    assert second["self_item_id"] == first["self_item_id"]
    assert second["method"] == second_method
    assert _world_of(driver, first["account"])["id"] == world["id"]
    assert world_counts(driver, world["id"]) == counts
    assert len(items_of(driver, world["id"])) == 1


def test_two_addresses_have_two_worlds_and_no_read_crosses(app, driver, accounts, mailer):
    first_client, second_client = browser(app), browser(app)
    first = email_sign_in(first_client, mailer, accounts.address()).json()
    second = email_sign_in(second_client, mailer, accounts.address()).json()
    assert first["account"] != second["account"]
    assert first["self_item_id"] != second["self_item_id"]
    first_world = _world_of(driver, first["account"])["id"]
    second_world = _world_of(driver, second["account"])["id"]
    assert first_world != second_world

    assert first_client.get(f"/v1/views/node/{first['self_item_id']}").status_code == 200
    refused = first_client.get(f"/v1/views/node/{second['self_item_id']}")
    assert refused.status_code == 404
    graph = first_client.get("/v1/views/graph").json()
    assert {node["data"]["id"] for node in graph["elements"]["nodes"]} == {first["self_item_id"]}


def test_two_first_sign_ins_of_one_address_make_one_of_everything(driver, accounts):
    address = accounts.address()
    hashed = email_hash(address)
    barrier = Barrier(2)

    def sign_in(method):
        barrier.wait()
        return ensure_account(driver, hashed, method=method, world_id_for=accounts.world_id_for)

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(sign_in, ["email", "google"])

    assert first.user_id == second.user_id
    assert first.world_id == second.world_id
    assert first.self_item_id == second.self_item_id
    assert [first.created, second.created].count(True) == 1
    assert user_of(driver, address)["id"] == first.user_id
    world = _world_of(driver, first.user_id)
    assert world["self_item_id"] == first.self_item_id
    assert [item["id"] for item in items_of(driver, world["id"])] == [first.self_item_id]
    query = WorldQuery(driver, world["id"])
    assert query.read("MATCH (c:Connector) RETURN count(c) AS n") == [{"n": 1}]
    assert query.read("MATCH (e:Episode) RETURN count(e) AS n") == [{"n": 1}]
    assert query.read("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] in (1, 2)


@pytest.mark.parametrize("seam", ["_ensure_connector", "_create_own_item"])
def test_a_first_sign_in_that_waited_for_the_world_lock_leaves_the_counter_alone(
    driver, accounts, monkeypatch, seam
):
    """Regression: sign-in locked with `SET w.next_sequence = w.next_sequence`, which reads the
    value before it waits and writes it back after. The waiter set the counter behind the entries
    the holder committed, so every later batch of the world collided with a used sequence."""
    hashed = email_hash(accounts.address())
    holding, opened = Event(), Event()
    seen: dict[str, str] = {}
    real = getattr(accounts_module, seam)
    barrier = Barrier(2, timeout=30)

    def one_then_the_other(*args: Any, **kwargs: Any) -> Any:
        seen["world_id"] = args[1]
        if barrier.wait() != 0:
            assert holding.wait(60), "the first sign-in never held the world's lock"
        return real(*args, **kwargs)

    def hold_the_first() -> None:
        if not holding.is_set():
            holding.set()
            assert opened.wait(60), "the test never opened the gate"

    monkeypatch.setattr(accounts_module, seam, one_then_the_other)
    monkeypatch.setattr(apply_module, "_before_commit", hold_the_first)

    def sign_in(method: str):
        return ensure_account(driver, hashed, method=method, world_id_for=accounts.world_id_for)

    with ThreadPoolExecutor(2) as pool:
        running = [pool.submit(sign_in, method) for method in ("email", "google")]
        assert holding.wait(60)
        waited = wait_until_blocked(driver, seen["world_id"], "SET w.lock_marker = true")
        opened.set()
        first, second = (each.result(timeout=60) for each in running)

    world_id = first.world_id
    assert second.world_id == world_id and first.self_item_id == second.self_item_id
    counter, last_entry = _counter_and_last_entry(driver, world_id)
    assert counter == last_entry, "the counter fell behind the entries"
    pages = make_source(driver, world_id, "web_pages", ["Maren Dovik signed the charter"])
    made = create_item("person", "Maren Dovik", "e1")
    receipt = apply_batch_of(
        driver, world_id, [made], [observation(pages, "e1", 1, locator="whole")]
    )
    assert receipt.state == "saved" and created_id(receipt)
    assert waited, "the second sign-in did not wait for the world's lock"


def _counter_and_last_entry(driver, world_id: str) -> tuple[int, int]:
    query = WorldQuery(driver, world_id)
    last = query.read("MATCH (c:ChangeEntry) RETURN max(c.sequence) AS last")[0]["last"]
    with driver.session(database=DATABASE) as session:
        record = session.run(
            "MATCH (w:World {id: $w}) RETURN w.next_sequence AS counter", w=world_id
        ).single()
    return record["counter"], last


def test_two_first_sign_ins_that_meet_at_the_own_item_transaction_make_one_item(
    driver, accounts, monkeypatch
):
    """Both have passed the fast path: only the lock and the read under it can stop the second."""
    hashed = email_hash(accounts.address())
    real = accounts_module._create_own_item
    barrier = Barrier(2, timeout=30)

    def together(*args, **kwargs):
        barrier.wait()
        return real(*args, **kwargs)

    monkeypatch.setattr(accounts_module, "_create_own_item", together)

    def sign_in(method):
        return ensure_account(driver, hashed, method=method, world_id_for=accounts.world_id_for)

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(sign_in, ["email", "google"])

    assert first.self_item_id == second.self_item_id
    assert [item["id"] for item in items_of(driver, first.world_id)] == [first.self_item_id]
    query = WorldQuery(driver, first.world_id)
    assert query.read("MATCH (c:Connector) RETURN count(c) AS n") == [{"n": 1}]
    assert query.read("MATCH (e:Episode) RETURN e.source_ids AS sources")[0]["sources"]
    assert query.read("MATCH (s:Source) RETURN count(s) AS n") == [{"n": 2}]


def test_the_own_item_transaction_waits_for_the_world_lock_and_then_reads_again(driver, accounts):
    """A deterministic order: a caller that read before taking the lock would make a second item."""
    user_id = accounts_module._ensure_user(driver, email_hash(accounts.address()))[0]
    world = accounts.world_id_for(user_id)
    create_world(driver, world, owner_user_id=user_id)
    connector = accounts_module._ensure_connector(driver, world)
    source = record_source(
        driver,
        world,
        connector_id=connector,
        external_locator="signin:email",
        content_pointer="signin:email",
        parser_version="signin.v1",
    )
    episode = accounts_module._record_first_episode(driver, world, connector, source)
    other_item = plant_node(driver, world, "Item", title="Made by the other caller")["id"]
    holding, release = Event(), Event()

    def hold_the_lock(tx):
        tx.lock_world()
        holding.set()
        assert release.wait(30)
        tx.unscoped().run(
            accounts_module._SET_OWN_ITEM, world_id=world, item_id=other_item
        ).consume()

    with ThreadPoolExecutor(2) as pool:
        holder = pool.submit(WorldWriter(driver, world).transaction, hold_the_lock)
        assert holding.wait(30)
        second = pool.submit(
            accounts_module._create_own_item, driver, world, "You", source, episode
        )
        with pytest.raises(TimeoutError):
            second.result(timeout=0.5)
        release.set()
        holder.result(timeout=30)
        assert second.result(timeout=30) == other_item

    assert [item["id"] for item in items_of(driver, world)] == [other_item]


def test_two_threads_with_one_right_code_give_exactly_one_session(app, driver, accounts, mailer):
    address = accounts.address()
    browser(app).post(START, json={"email": address})
    code = mailer.last_code(address)
    barrier = Barrier(2)

    def verify(_):
        client = browser(app)
        barrier.wait()
        return client.post(VERIFY, json={"email": address, "code": code})

    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(verify, range(2)))

    assert sorted(answer.status_code for answer in answers) == [200, 401]
    assert len(sessions_of(driver, user_of(driver, address)["id"])) == 1
    assert codes_of(driver, address)[0]["c"].get("used_at") is not None


class _Crash(Exception):
    pass


@pytest.mark.parametrize(
    "step", ["_ensure_connector", "record_source", "_record_first_episode", "_create_own_item"]
)
def test_a_crash_between_steps_leaves_one_own_item_after_the_next_sign_in(
    step, driver, accounts, monkeypatch
):
    hashed = email_hash(accounts.address())

    def crash(*args, **kwargs):
        raise _Crash

    with monkeypatch.context() as patch:
        patch.setattr(accounts_module, step, crash)
        with pytest.raises(_Crash):
            ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)

    account = ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)
    assert [item["id"] for item in items_of(driver, account.world_id)] == [account.self_item_id]
    world = _world_of(driver, account.user_id)
    assert world["id"] == account.world_id and world["self_item_id"] == account.self_item_id


def test_a_failure_in_the_own_item_transaction_leaves_neither_item_nor_pointer(
    driver, accounts, monkeypatch
):
    hashed = email_hash(accounts.address())

    def fail():
        raise _Crash

    with monkeypatch.context() as patch:
        patch.setattr(apply_module, "_before_commit", fail)
        with pytest.raises(_Crash):
            ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)

    worlds = worlds_of(driver, accounts_module._ensure_user(driver, hashed)[0])
    assert worlds[0]["self_item_id"] is None
    assert items_of(driver, worlds[0]["id"]) == []


def test_a_declined_title_makes_the_item_with_the_neutral_title(driver, accounts, monkeypatch):
    hashed = email_hash(accounts.address())
    real = accounts_module.apply_ops

    def picky(tx, *, stamp, ops, observations, pre_refused):
        if ops[0]["title"] != "You":
            refusal = Refusal("own_item", "create_item", "invalid_arguments", "title", "no")
            raise BatchDeclined([refusal], batch_size=1)
        real(tx, stamp=stamp, ops=ops, observations=observations, pre_refused=pre_refused)

    monkeypatch.setattr(accounts_module, "apply_ops", picky)
    account = ensure_account(
        driver,
        hashed,
        method="google",
        display_name="An odd name",
        world_id_for=accounts.world_id_for,
    )
    items = items_of(driver, account.world_id)
    assert [item["title"] for item in items] == ["You"]


def test_a_second_call_writes_nothing_new(driver, accounts):
    hashed = email_hash(accounts.address())
    first = ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)
    counts = world_counts(driver, first.world_id)
    second = ensure_account(driver, hashed, method="google", world_id_for=accounts.world_id_for)
    assert second.created is False and first.created is True
    assert (second.user_id, second.world_id, second.self_item_id) == (
        first.user_id,
        first.world_id,
        first.self_item_id,
    )
    assert world_counts(driver, first.world_id) == counts


def test_the_address_is_salted_folded_and_stays_out_of_the_rows(driver, accounts):
    address = accounts.address()
    assert email_hash(address) == email_hash(f"  {address.upper()} ")
    assert len(email_hash(address)) == 64
    plain = hashlib.sha256(address.encode()).hexdigest()
    assert email_hash(address) != plain

    account = ensure_account(
        driver, email_hash(address), method="email", world_id_for=accounts.world_id_for
    )
    query = WorldQuery(driver, account.world_id)
    for label in ("Source", "Episode", "Connector", "Evidence", "Item"):
        for row in query.read(f"MATCH (n:{label}) RETURN n"):
            assert address not in str(row["n"])


def test_the_default_world_id_is_one_id_per_user_and_a_legal_world_id():
    assert default_world_id("user-1") == default_world_id("user-1")
    assert default_world_id("user-1") != default_world_id("user-2")
    assert check_world_id(default_world_id("user-1")) == default_world_id("user-1")


@pytest.mark.parametrize(
    ("given", "wanted"),
    [
        ("Maya Chen", "Maya Chen"),
        ("  Maya  ", "Maya"),
        ("Ma\x00ya\x7f", "Maya"),
        ("Ma\ud800ya", "Maya"),
        ("", "You"),
        ("\n\t", "You"),
        (None, "You"),
        (5, "You"),
        ("x" * 130, "x" * 120),
    ],
)
def test_the_title_of_the_own_item(given, wanted):
    assert own_title(given) == wanted


def test_revoke_keeps_the_first_revoked_time_and_the_test_purge_clears(driver, accounts, mailer):
    hashed = email_hash(accounts.address())
    account = ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)
    token = "a-token-of-this-test"
    created = datetime(2026, 11, 2, tzinfo=UTC)
    create_session(driver, account.user_id, token_hash(token), method="email", expires_at=created)
    first, later = datetime(2026, 11, 3, tzinfo=UTC), datetime(2026, 11, 4, tzinfo=UTC)
    revoke_session(driver, token_hash(token), first)
    revoke_session(driver, token_hash(token), later)
    assert sessions_of(driver, account.user_id)[0]["revoked_at"] == first

    with pytest.raises(ValueError):
        create_session(
            driver, account.user_id, token_hash("other"), method="password", expires_at=created
        )
    purge_test_account(driver, hashed)
    assert sessions_of(driver, account.user_id) == []


def test_a_planted_extra_item_turns_has_items_on(client, driver, accounts, mailer):
    body = email_sign_in(client, mailer, accounts.address()).json()
    assert body["has_items"] is False
    world = _world_of(driver, body["account"])["id"]
    plant_node(driver, world, "Item", title="Another thing")
    answer = client.get(SESSION_VIEW).json()
    assert answer["has_items"] is True
    assert answer["self_item_id"] == body["self_item_id"]
