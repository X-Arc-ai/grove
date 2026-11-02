"""The email code: hashes only, the first sign-in, every way a code fails, and the limits."""

import json
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.api import auth as auth_module
from grove.store.errors import STORE_UNAVAILABLE
from grove.store.query import WorldQuery
from grove.writer.accounts import email_hash
from tests.auth.conftest import (
    LOOPBACK,
    START,
    VERIFY,
    browser,
    codes_of,
    cookie_flags,
    email_sign_in,
    items_of,
    sessions_of,
    user_of,
    worlds_of,
)

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SESSION_KEYS = {"signed_in", "self_item_id", "has_items", "account", "method"}


@pytest.fixture
def test_clock():
    held = clock.TestClock()
    held.set(T0)
    with clock.using(held):
        yield held


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "000001"


def test_start_stores_only_hashes(client, driver, accounts, mailer):
    address = accounts.address()
    response = client.post(START, json={"email": address})
    assert response.status_code == 200
    assert response.json() == {"sent": True}
    assert response.headers["cache-control"] == "no-store"
    code = mailer.last_code(address)
    assert len(code) == 6 and code.isdigit()
    rows = codes_of(driver, address)
    assert len(rows) == 1
    row = rows[0]["c"]
    text = json.dumps(row, default=str)
    assert address not in text and code not in row["code_hash"]
    assert address.split("@")[0] not in text
    assert code not in {str(value) for value in row.values()}
    assert row["email_hash"] == email_hash(address)
    assert row["attempts"] == 0


def test_verify_signs_in_and_sets_the_cookie_with_the_right_flags(client, driver, accounts, mailer):
    address = accounts.address()
    response = email_sign_in(client, mailer, address)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    token, flags = cookie_flags(response, "grove_session")
    assert len(token) >= 32
    assert {"httponly", "samesite=lax", "path=/", "secure", f"max-age={30 * 24 * 3600}"} <= flags
    assert client.get("/v1/views/session").status_code == 200


def test_the_cookie_is_not_secure_on_a_loopback_base(make_app, accounts, mailer):
    client = browser(make_app(LOOPBACK), LOOPBACK)
    address = accounts.address()
    response = email_sign_in(client, mailer, address)
    _, flags = cookie_flags(response, "grove_session")
    assert "secure" not in flags
    assert "httponly" in flags


def test_the_first_sign_in_makes_a_user_a_seeded_world_and_one_own_item(
    client, driver, accounts, mailer
):
    address = accounts.address()
    body = email_sign_in(client, mailer, address).json()
    assert set(body) == SESSION_KEYS

    user = user_of(driver, address)
    worlds = worlds_of(driver, user["id"])
    assert body["account"] == user["id"]
    assert body["method"] == "email"
    assert body["has_items"] is False
    assert len(worlds) == 1 and worlds[0]["seed_version"] is not None
    world = worlds[0]["id"]
    assert worlds[0]["self_item_id"] == body["self_item_id"]

    items = items_of(driver, world)
    assert [item["id"] for item in items] == [body["self_item_id"]]
    assert items[0]["title"] == "You"
    query = WorldQuery(driver, world)
    kind = query.read("MATCH (k:Kind {id: $id}) RETURN k.name AS name", id=items[0]["kind"])
    assert kind == [{"name": "person"}]

    # Through the writer: a change entry, evidence, and a pointer source with no text.
    entries = query.read(
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) RETURN e.actor AS actor",
        id=items[0]["id"],
    )
    assert entries and {entry["actor"] for entry in entries} == {"person:signin"}
    evidence = query.read(
        "MATCH (e:Evidence {target_id: $id}) RETURN e.locator AS locator, e.source_id AS source, "
        "e.statement_class AS statement_class, e.derivation AS derivation, "
        "e.speaker_kind AS speaker, e.modality AS modality",
        id=items[0]["id"],
    )
    assert len(evidence) == 1
    assert evidence[0]["locator"] == "whole"
    assert evidence[0]["statement_class"] == "report_state"
    assert evidence[0]["derivation"] == "observed"
    assert (evidence[0]["speaker"], evidence[0]["modality"]) == ("person", "text")
    source = query.read(
        "MATCH (s:Source {id: $id}) RETURN s.content AS content, s.content_pointer AS pointer, "
        "s.parser_version AS parser",
        id=evidence[0]["source"],
    )[0]
    assert source == {"content": None, "pointer": "signin:email", "parser": "signin.v1"}


@pytest.mark.parametrize("how", ["wrong", "expired", "used", "superseded", "absent"])
def test_a_code_that_fails_gets_one_answer(how, client, driver, accounts, mailer, test_clock):
    address = accounts.address()
    if how != "absent":
        assert client.post(START, json={"email": address}).status_code == 200
    code = mailer.last_code(address) if how != "absent" else "123456"
    if how == "wrong":
        code = _wrong(code)
    elif how == "expired":
        test_clock.advance(minutes=10, seconds=1)
    elif how == "used":
        assert client.post(VERIFY, json={"email": address, "code": code}).status_code == 200
        client.cookies.clear()
    elif how == "superseded":
        assert client.post(START, json={"email": address}).status_code == 200
        if mailer.last_code(address) == code:
            code = _wrong(code)
    response = client.post(VERIFY, json={"email": address, "code": code})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.json()["error"]["message"] == "the code does not match"
    assert "set-cookie" not in response.headers


def test_a_wrong_try_commits_its_attempt(client, driver, accounts, mailer):
    address = accounts.address()
    client.post(START, json={"email": address})
    wrong = _wrong(mailer.last_code(address))
    client.post(VERIFY, json={"email": address, "code": wrong})
    client.post(VERIFY, json={"email": address, "code": wrong})
    assert codes_of(driver, address)[0]["c"]["attempts"] == 2


def test_the_fifth_try_can_succeed_and_the_sixth_fails_even_with_the_right_code(
    client, driver, accounts, mailer
):
    address, other = accounts.address(), accounts.address()
    client.post(START, json={"email": address})
    right = mailer.last_code(address)
    for _ in range(5):
        response = client.post(VERIFY, json={"email": address, "code": _wrong(right)})
        assert response.status_code == 401
    response = client.post(VERIFY, json={"email": address, "code": right})
    assert response.status_code == 401
    # The code is dead now, not only refused once.
    assert codes_of(driver, address)[0]["c"].get("used_at") is not None
    assert user_of(driver, address) is None

    client.post(START, json={"email": other})
    right = mailer.last_code(other)
    for _ in range(4):
        client.post(VERIFY, json={"email": other, "code": _wrong(right)})
    assert client.post(VERIFY, json={"email": other, "code": right}).status_code == 200


@pytest.mark.parametrize(
    "code", ["12345", "1234567", "abcdef", "١٢٣٤٥٦", 123456, None, "", " 12345"]
)
def test_a_code_of_another_form_is_400_and_uses_no_try(code, client, driver, accounts, mailer):
    address = accounts.address()
    client.post(START, json={"email": address})
    response = client.post(VERIFY, json={"email": address, "code": code})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "code"
    assert codes_of(driver, address)[0]["c"]["attempts"] == 0


def test_an_address_over_its_limit_gets_429_with_the_wait(client, driver, accounts, test_clock):
    address = accounts.address()
    for _ in range(5):
        assert client.post(START, json={"email": address}).status_code == 200
        test_clock.advance(minutes=1)
    response = client.post(START, json={"email": address})
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limited"
    # The oldest of the five is five minutes old and leaves a window of fifteen.
    assert error["details"]["retry_after_seconds"] == 600
    assert error["retryable"] is True
    assert address not in response.text
    assert len(codes_of(driver, address)) == 5

    test_clock.advance(minutes=10, seconds=1)
    assert client.post(START, json={"email": address}).status_code == 200


def test_a_new_code_ends_the_earlier_one_and_old_rows_go(client, driver, accounts, test_clock):
    address = accounts.address()
    client.post(START, json={"email": address})
    assert codes_of(driver, address)[0]["c"].get("used_at") is None
    test_clock.advance(minutes=1)
    client.post(START, json={"email": address})
    rows = codes_of(driver, address)
    assert rows[0]["c"].get("used_at") is not None and rows[1]["c"].get("used_at") is None

    test_clock.advance(days=2)
    client.post(START, json={"email": address})
    assert len(codes_of(driver, address)) == 1


def test_the_global_window_gives_429_and_opens_again(client, app, accounts, driver, test_clock):
    window = app.state.auth.window
    for _ in range(auth_module.MAX_CODES_PER_HOUR):
        assert window.take() is None
    address = accounts.address()
    response = client.post(START, json={"email": address})
    assert response.status_code == 429
    assert response.json()["error"]["details"]["retry_after_seconds"] == 3600
    assert codes_of(driver, address) == []

    test_clock.advance(minutes=60)
    assert client.post(START, json={"email": address}).status_code == 200


def test_a_busy_address_spends_no_slot_of_the_global_window(client, app, accounts, test_clock):
    address = accounts.address()
    for _ in range(5):
        client.post(START, json={"email": address})
    window = app.state.auth.window
    taken = len(window._taken)
    assert client.post(START, json={"email": address}).status_code == 429
    assert len(window._taken) == taken


@pytest.mark.parametrize(
    "address",
    [
        "no-at-sign",
        "two@@example.test",
        "two@at@example.test",
        "@example.test",
        "local@",
        "local@nodot",
        "local@.test",
        "local@example.",
        "has space@example.test",
        "tab\t@example.test",
        "ctrl\x01@example.test",
        "x" * 250 + "@a.test",
        "",
        None,
        123,
    ],
)
def test_an_invalid_address_is_400_naming_the_field_and_not_the_value(address, client):
    response = client.post(START, json={"email": address})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert error["details"]["field"] == "email"
    if isinstance(address, str) and address:
        assert address not in response.text


def test_a_look_alike_address_reaches_no_other_account(client, driver, accounts, mailer):
    """U+212A, the Kelvin sign, lowers to k. A start with it must be refused, not mailed."""
    victim = "k" + accounts.address()
    look_alike = "\u212a" + victim[1:]
    # The collision this rule closes: both forms give one account key.
    assert email_hash(look_alike) == email_hash(victim)
    sent = len(mailer.sent)
    response = client.post(START, json={"email": look_alike})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "email"
    assert len(mailer.sent) == sent


def test_a_lone_surrogate_in_an_address_is_400(client):
    body = b'{"email": "\\ud800@example.test"}'
    response = client.post(START, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "email"


@pytest.mark.parametrize("path", [START, VERIFY, "/v1/auth/logout"])
@pytest.mark.parametrize(
    "headers",
    [{"Content-Type": "application/x-www-form-urlencoded"}, {"Content-Type": "text/plain"}, {}],
)
def test_a_post_that_is_not_json_is_refused(path, headers, client):
    response = client.post(path, content=b'{"email": "a@example.test"}', headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "content-type"


def test_a_body_past_the_cap_or_with_other_fields_is_refused(client):
    big = {"email": "a" * 5000 + "@example.test"}
    assert client.post(START, json=big).json()["error"]["details"]["field"] == "body"
    extra = {"email": "a@example.test", "more": 1}
    assert client.post(START, json=extra).json()["error"]["details"]["field"] == "body"
    assert client.post(START, json=["a@example.test"]).status_code == 400
    bad = client.post(START, content=b"{not json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_a_mail_failure_is_503_with_no_leak(client, driver, accounts, mailer):
    address = accounts.address()
    mailer.fail = True
    response = client.post(START, json={"email": address})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "unavailable" and error["retryable"] is True
    assert address not in response.text
    assert address.split("@")[0] not in response.text


def test_no_mail_config_is_503_and_stores_nothing(make_app, driver, accounts, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    client = browser(make_app(mailer=None))
    address = accounts.address()
    response = client.post(START, json={"email": address})
    assert response.status_code == 503
    assert codes_of(driver, address) == []


def test_the_same_answer_for_a_new_and_a_known_address(client, accounts, mailer):
    known, new = accounts.address(), accounts.address()
    assert email_sign_in(client, mailer, known).status_code == 200
    client.cookies.clear()
    known_answer = client.post(START, json={"email": known})
    new_answer = client.post(START, json={"email": new})
    assert known_answer.status_code == new_answer.status_code == 200
    assert known_answer.content == new_answer.content

    wrong_known = client.post(
        VERIFY, json={"email": known, "code": _wrong(mailer.last_code(known))}
    )
    wrong_new = client.post(VERIFY, json={"email": new, "code": _wrong(mailer.last_code(new))})
    assert wrong_known.status_code == wrong_new.status_code == 401
    assert wrong_known.content == wrong_new.content


@pytest.mark.parametrize("step", ["ensure_account", "create_session", "try_email_code"])
def test_a_store_failure_before_the_cookie_is_503_with_no_cookie(
    step, client, accounts, mailer, monkeypatch
):
    address = accounts.address()
    client.post(START, json={"email": address})

    def down(*args, **kwargs):
        raise STORE_UNAVAILABLE[0]("the store went away")

    monkeypatch.setattr(auth_module, step, down)
    response = client.post(VERIFY, json={"email": address, "code": mailer.last_code(address)})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert "set-cookie" not in response.headers


def test_a_store_failure_at_start_spends_no_slot(client, app, accounts, mailer, monkeypatch):
    def down(*args, **kwargs):
        raise STORE_UNAVAILABLE[0]("the store went away")

    monkeypatch.setattr(auth_module, "store_email_code", down)
    response = client.post(START, json={"email": accounts.address()})
    assert response.status_code == 503
    assert mailer.sent == []
    assert len(app.state.auth.window._taken) == 0


def test_a_session_lasts_thirty_days(client, driver, accounts, mailer, test_clock):
    address = accounts.address()
    body = email_sign_in(client, mailer, address).json()
    row = sessions_of(driver, body["account"])[0]
    assert (row["expires_at"] - T0).days == 30
    test_clock.advance(days=29)
    assert client.get("/v1/views/session").status_code == 200
    test_clock.advance(days=1, seconds=1)
    assert client.get("/v1/views/session").status_code == 401
