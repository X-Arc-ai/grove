"""POST /v1/sources/upload and GET /v1/sources/connectors: who may upload, what is refused before
a byte is read, screening on the way in, and the coverage a person sees afterwards.

A test-only connector reads a tiny JSON format, so these tests prove the route and not a real
parser. Every person, chat and id here is invented (R3).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from grove.api import uploads
from grove.ids import new_id
from grove.ingest import (
    ConnectorSpec,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    clean_text,
    message_key,
    register,
    spec_for,
)
from grove.ingest.archive import json_document
from grove.store.query import WorldQuery
from grove.writer.ingest import ensure_connector
from grove.writer.testing import seed_test_world
from grove.writer.upload_tokens import mint_upload_token, revoke_upload_token
from tests.ingest.conftest import STORE_TIME, counts

PROVIDER = "zz_upload_test"
UPLOAD_PATH = f"/v1/sources/upload?provider={PROVIDER}"
CONNECTORS_PATH = "/v1/sources/connectors"
FIRST_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
JSON_TYPE = {"content-type": "application/json"}
EMPTY = {"connectors": 0, "sources": 0, "episodes": 0, "receipts": 0}
MAX_TEST_BYTES = 1024 * 1024

CHATS = [
    {
        "id": "chat-alpha-001",
        "lines": [
            "Sketch a plan for the greenhouse shelves",
            "Three pine shelves fit along the north wall",
            "Order the brackets on Friday",
        ],
    },
    {
        "id": "chat-beta-002",
        "lines": [
            "Draft a packing list for the hiking weekend",
            "Tent, stove, two maps and a first aid kit",
        ],
    },
]


def _parse(upload):
    """The test format: {"chats": [{"id": str, "lines": [str, ...]}]}, one minute per line."""
    document = json_document(upload.read_all(MAX_TEST_BYTES))
    chats = document.get("chats") if isinstance(document, dict) else None
    if not isinstance(chats, list):
        raise NotAnExport("the file is not a test export")
    parsed = []
    for chat in chats:
        if not (
            isinstance(chat, dict)
            and isinstance(chat.get("id"), str)
            and isinstance(chat.get("lines"), list)
        ):
            raise NotAnExport("the file is not a test export")
        messages = tuple(
            ParsedMessage(
                key=message_key("zz", chat["id"], index),
                text=clean_text(line),
                at=FIRST_AT + timedelta(minutes=index),
                speaker_kind="person" if index % 2 == 0 else "ai_host",
            )
            for index, line in enumerate(chat["lines"])
        )
        parsed.append(ParsedChat(chat_id=chat["id"], messages=messages))
    return iter(parsed)


SPEC = spec_for(PROVIDER) or register(
    ConnectorSpec(
        provider=PROVIDER,
        connector_type="past_ai_conversations",
        parser_version="zz_upload_test.v1",
        batch_rule="session_delta.v1",
        key_prefix="zz",
        parse=_parse,
    )
)


def export_of(chats=CHATS) -> bytes:
    return json.dumps({"chats": chats}).encode("utf-8")


def upload(client, body=None, *, headers=None, path=UPLOAD_PATH):
    return client.post(
        path, content=export_of() if body is None else body, headers=headers or JSON_TYPE
    )


def bearer(token: str) -> dict[str, str]:
    return {**JSON_TYPE, "authorization": f"Bearer {token}"}


def stored_texts(driver, world_id: str) -> list[str]:
    rows = WorldQuery(driver, world_id).read("MATCH (s:Source) RETURN s.content AS content")
    return [row["text"] for found in rows for row in json.loads(found["content"])]


def assert_refused(response, status: int, code: str, field: str | None = None) -> dict:
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    if field is not None:
        assert error["details"]["field"] == field
    return error


def private_file(path, text: str):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The box's own screening and cap settings never reach these tests."""
    for name in ("GROVE_ENV", "GROVE_RENAME_MAP", "GROVE_DENYLIST", uploads.MAX_BYTES_VAR):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# The session upload and its re-import.


def test_a_session_upload_stores_each_chat(driver, seeded_world, client_for, test_clock):
    response = upload(client_for(seeded_world))
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "taken_in"
    assert body["provider"] == PROVIDER
    assert (body["chats"], body["chats_new"], body["sources_added"]) == (2, 2, 2)
    assert (body["episodes_added"], body["messages_read"], body["messages_kept"]) == (2, 5, 5)
    assert response.headers["cache-control"] == "no-store"
    assert counts(driver, seeded_world) == {
        "connectors": 1,
        "sources": 2,
        "episodes": 2,
        "receipts": 2,
    }
    assert sorted(stored_texts(driver, seeded_world)) == sorted(
        line for chat in CHATS for line in chat["lines"]
    )


def test_the_same_upload_again_is_nothing_new(driver, seeded_world, client_for, test_clock):
    client = client_for(seeded_world)
    assert upload(client).json()["state"] == "taken_in"
    before = counts(driver, seeded_world)
    again = upload(client)
    assert again.status_code == 200
    assert again.json()["state"] == "nothing_new"
    assert again.json()["sources_added"] == 0
    assert counts(driver, seeded_world) == before


def test_the_answer_holds_no_text_and_no_key(seeded_world, client_for, test_clock):
    raw = upload(client_for(seeded_world)).text
    assert "greenhouse" not in raw
    assert "chat-alpha-001" not in raw
    assert "zz-" not in raw


# Who may upload.


def test_no_credential_is_401(driver, seeded_world, anonymous, test_clock):
    assert_refused(upload(anonymous), 401, "unauthenticated")
    assert counts(driver, seeded_world) == EMPTY


@pytest.mark.parametrize(
    "header",
    [
        "Bearer gut_madeUpTokenThatNoMintEverGave0000000000000",
        "Bearer not-an-upload-token",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "",
    ],
    ids=["unknown-token", "other-kind", "empty-token", "basic", "empty-header"],
)
def test_a_bad_authorization_header_is_401(driver, seeded_world, anonymous, test_clock, header):
    response = upload(anonymous, headers={**JSON_TYPE, "authorization": header})
    error = assert_refused(response, 401, "unauthenticated")
    assert "gut_" not in error["message"]
    assert counts(driver, seeded_world) == EMPTY


def test_a_bad_token_never_falls_back_to_the_cookie(driver, seeded_world, client_for, test_clock):
    client = client_for(seeded_world)
    response = upload(client, headers=bearer("gut_madeUpTokenThatNoMintEverGave0000000000000"))
    assert_refused(response, 401, "unauthenticated")
    assert counts(driver, seeded_world) == EMPTY
    assert upload(client).status_code == 200


def test_a_revoked_token_is_401(driver, seeded_world, anonymous, test_clock):
    minted = mint_upload_token(driver, seeded_world)
    assert revoke_upload_token(driver, minted.id) is True
    assert_refused(upload(anonymous, headers=bearer(minted.token)), 401, "unauthenticated")
    assert counts(driver, seeded_world) == EMPTY


def test_an_expired_token_is_401(driver, seeded_world, anonymous, test_clock):
    minted = mint_upload_token(driver, seeded_world, minutes=5)
    test_clock.advance(minutes=4)
    assert upload(anonymous, headers=bearer(minted.token)).status_code == 200
    test_clock.advance(minutes=1)
    assert_refused(upload(anonymous, headers=bearer(minted.token)), 401, "unauthenticated")
    test_clock.advance(minutes=30)
    assert_refused(upload(anonymous, headers=bearer(minted.token)), 401, "unauthenticated")


def test_a_token_stores_into_its_own_world_only(
    driver, make_world, seeded_world, client_for, anonymous, test_clock
):
    world_a = seeded_world
    world_b = make_world()
    seed_test_world(driver, world_b, owner_user_id=new_id())
    minted = mint_upload_token(driver, world_a)
    response = upload(anonymous, headers=bearer(minted.token))
    assert response.status_code == 200
    assert response.json()["state"] == "taken_in"
    assert counts(driver, world_a)["sources"] == 2
    assert counts(driver, world_b) == EMPTY
    session_b = client_for(world_b)
    assert session_b.get(CONNECTORS_PATH).json() == {"connectors": []}


def test_the_token_decides_over_a_cookie_of_another_world(
    driver, make_world, seeded_world, client_for, test_clock
):
    """A present Authorization header decides alone, so the cookie never moves the upload."""
    world_a = seeded_world
    world_b = make_world()
    seed_test_world(driver, world_b, owner_user_id=new_id())
    minted = mint_upload_token(driver, world_a)
    session_b = client_for(world_b)
    assert upload(session_b, headers=bearer(minted.token)).status_code == 200
    assert counts(driver, world_a)["sources"] == 2
    assert counts(driver, world_b) == EMPTY


@pytest.mark.parametrize("path", ["/v1/views/graph", CONNECTORS_PATH])
def test_an_upload_token_is_no_session_elsewhere(driver, seeded_world, anonymous, test_clock, path):
    minted = mint_upload_token(driver, seeded_world)
    response = anonymous.get(path, headers={"authorization": f"Bearer {minted.token}"})
    assert_refused(response, 401, "unauthenticated")


# What is refused before the body is read.


@pytest.mark.parametrize(
    "content_type",
    [
        "multipart/form-data; boundary=zzboundary",
        "application/x-www-form-urlencoded",
        "text/plain",
        "text/plain; charset=utf-8",
        None,
    ],
    ids=["multipart", "form", "text", "text-charset", "none"],
)
def test_a_form_or_missing_type_is_400(driver, seeded_world, client_for, test_clock, content_type):
    headers = {} if content_type is None else {"content-type": content_type}
    response = client_for(seeded_world).post(UPLOAD_PATH, content=export_of(), headers=headers)
    assert_refused(response, 400, "invalid_arguments", "content-type")
    assert counts(driver, seeded_world) == EMPTY


@pytest.mark.parametrize(
    "content_type",
    ["Application/JSON; charset=utf-8", "application/octet-stream", "application/x-ndjson"],
)
def test_the_accepted_types_ignore_case_and_parameters(
    seeded_world, client_for, test_clock, content_type
):
    response = upload(client_for(seeded_world), headers={"content-type": content_type})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/v1/sources/upload?provider=zz_nobody_reads_this", "/v1/sources/upload"],
    ids=["unknown", "missing"],
)
def test_an_unknown_provider_is_400_with_the_valid_list(
    driver, seeded_world, client_for, test_clock, path
):
    error = assert_refused(
        upload(client_for(seeded_world), path=path), 400, "invalid_arguments", "provider"
    )
    assert set(error["details"]) == {"field", "valid"}
    assert PROVIDER in error["details"]["valid"]
    assert "claude_code" in error["details"]["valid"]
    assert "zz_nobody_reads_this" not in error["message"]
    assert counts(driver, seeded_world) == EMPTY


def test_a_body_over_the_cap_is_400(driver, seeded_world, client_for, test_clock, clean_env):
    clean_env.setenv(uploads.MAX_BYTES_VAR, "64")
    response = upload(client_for(seeded_world))
    assert_refused(response, 400, "invalid_arguments", "body")
    assert counts(driver, seeded_world) == EMPTY


def test_a_chunked_body_over_the_cap_is_400(
    driver, seeded_world, client_for, test_clock, clean_env
):
    """No Content-Length: the cap counts the bytes as they arrive."""
    clean_env.setenv(uploads.MAX_BYTES_VAR, "64")
    data = export_of()

    def chunks():
        for start in range(0, len(data), 16):
            yield data[start : start + 16]

    response = client_for(seeded_world).post(UPLOAD_PATH, content=chunks(), headers=JSON_TYPE)
    assert_refused(response, 400, "invalid_arguments", "body")
    assert counts(driver, seeded_world) == EMPTY


def test_a_body_at_the_cap_is_taken(driver, seeded_world, client_for, test_clock, clean_env):
    data = export_of()
    clean_env.setenv(uploads.MAX_BYTES_VAR, str(len(data)))
    assert upload(client_for(seeded_world), data).status_code == 200


@pytest.mark.parametrize("value", ["lots", "0", "-5", "", "1e6"])
def test_a_bad_cap_setting_is_503(driver, seeded_world, client_for, test_clock, clean_env, value):
    clean_env.setenv(uploads.MAX_BYTES_VAR, value)
    error = assert_refused(upload(client_for(seeded_world)), 503, "unavailable")
    assert error["retryable"] is False
    assert uploads.MAX_BYTES_VAR not in error["message"]
    assert counts(driver, seeded_world) == EMPTY


def test_an_empty_body_is_400(driver, seeded_world, client_for, test_clock):
    response = upload(client_for(seeded_world), b"")
    assert_refused(response, 400, "invalid_arguments", "body")
    assert counts(driver, seeded_world) == EMPTY


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json at all", "the file is not a JSON document"),
        (b'{"conversations": []}', "the file is not a test export"),
        (b'{"chats": [{"id": 7}]}', "the file is not a test export"),
    ],
    ids=["not-json", "other-shape", "bad-chat"],
)
def test_a_file_that_is_no_export_is_400_and_makes_no_connector(
    driver, seeded_world, client_for, test_clock, body, message
):
    error = assert_refused(upload(client_for(seeded_world), body), 400, "invalid_arguments", "file")
    # The parser's own generic message, which names the format and no value of the file.
    assert error["message"] == message
    assert counts(driver, seeded_world) == EMPTY


# Screening settings.


@pytest.mark.parametrize("unset", ["GROVE_RENAME_MAP", "GROVE_DENYLIST"])
def test_production_without_a_screening_file_is_503_and_stores_nothing(
    driver, seeded_world, client_for, test_clock, clean_env, tmp_path, unset
):
    clean_env.setenv("GROVE_ENV", "production")
    clean_env.setenv("GROVE_RENAME_MAP", str(private_file(tmp_path / "rename.json", "{}")))
    clean_env.setenv("GROVE_DENYLIST", str(private_file(tmp_path / "deny.txt", "")))
    clean_env.delenv(unset)
    response = upload(client_for(seeded_world))
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "unavailable",
            "message": "screening is not configured",
            "details": {},
            "retryable": False,
        }
    }
    assert counts(driver, seeded_world) == EMPTY


@pytest.mark.parametrize(
    ("body", "cap"),
    [(export_of(), "64"), (b"not json at all", None), (b"", None)],
    ids=["over-the-cap", "no-export", "empty"],
)
def test_screening_is_checked_before_the_body_is_read(
    driver, seeded_world, client_for, test_clock, clean_env, body, cap
):
    """A body the route would refuse still gets the 503: the settings are checked first, so a
    box that cannot screen never reads or parses a byte of the file."""
    clean_env.setenv("GROVE_ENV", "production")
    if cap is not None:
        clean_env.setenv(uploads.MAX_BYTES_VAR, cap)
    error = assert_refused(upload(client_for(seeded_world), body), 503, "unavailable")
    assert error["message"] == "screening is not configured"
    assert counts(driver, seeded_world) == EMPTY


def test_a_screening_file_open_to_others_is_503(
    driver, seeded_world, client_for, test_clock, clean_env, tmp_path
):
    loose = tmp_path / "rename.json"
    loose.write_text("{}", encoding="utf-8")
    loose.chmod(0o644)
    clean_env.setenv("GROVE_RENAME_MAP", str(loose))
    assert_refused(upload(client_for(seeded_world)), 503, "unavailable")
    assert counts(driver, seeded_world) == EMPTY


def test_production_screening_renames_and_drops(
    driver, seeded_world, client_for, test_clock, clean_env, tmp_path
):
    rename = private_file(tmp_path / "rename.json", json.dumps({"Quillon Varga": "Person-Q"}))
    deny = private_file(tmp_path / "deny.txt", "Zephrine Okoro\n")
    clean_env.setenv("GROVE_ENV", "production")
    clean_env.setenv("GROVE_RENAME_MAP", str(rename))
    clean_env.setenv("GROVE_DENYLIST", str(deny))
    chats = [
        {
            "id": "chat-gamma-003",
            "lines": [
                "Ask Quillon Varga about the shelf brackets",
                "Zephrine Okoro sent the tent list",
                "The stove needs a new valve",
            ],
        }
    ]
    response = upload(client_for(seeded_world), export_of(chats))
    assert response.status_code == 200
    body = response.json()
    assert (body["messages_read"], body["messages_kept"]) == (3, 2)
    assert body["screened"]["renamed"] == 1
    assert body["screened"]["denylisted"] == 1
    texts = stored_texts(driver, seeded_world)
    assert texts == ["Ask Person-Q about the shelf brackets", "The stove needs a new valve"]
    assert not any("Quillon" in text or "Zephrine" in text for text in texts)


# The upload slots.


def test_two_running_uploads_make_a_third_wait(driver, seeded_world, client_for, test_clock):
    client = client_for(seeded_world)
    assert uploads.UPLOAD_SLOTS.acquire(blocking=False)
    assert uploads.UPLOAD_SLOTS.acquire(blocking=False)
    try:
        response = upload(client)
    finally:
        uploads.UPLOAD_SLOTS.release()
        uploads.UPLOAD_SLOTS.release()
    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "rate_limited",
            "message": "another upload is running: try again soon",
            "details": {"retry_after_seconds": 30},
            "retryable": True,
        }
    }
    assert counts(driver, seeded_world) == EMPTY
    assert upload(client).status_code == 200


def test_one_running_upload_leaves_a_slot(seeded_world, client_for, test_clock):
    client = client_for(seeded_world)
    assert uploads.UPLOAD_SLOTS.acquire(blocking=False)
    try:
        assert upload(client).status_code == 200
    finally:
        uploads.UPLOAD_SLOTS.release()


def test_a_refused_upload_gives_its_slot_back(seeded_world, client_for, test_clock):
    client = client_for(seeded_world)
    for _ in range(3):
        assert upload(client, b"not json at all").status_code == 400
        assert upload(client, b"").status_code == 400
    assert uploads.UPLOAD_SLOTS.acquire(blocking=False)
    assert uploads.UPLOAD_SLOTS.acquire(blocking=False)
    uploads.UPLOAD_SLOTS.release()
    uploads.UPLOAD_SLOTS.release()


# The connectors read.


def test_the_connectors_read_shows_the_coverage(driver, seeded_world, client_for, test_clock):
    client = client_for(seeded_world)
    assert client.get(CONNECTORS_PATH).json() == {"connectors": []}
    taken = upload(client).json()
    response = client.get(CONNECTORS_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "connectors": [
            {
                "connector_id": taken["connector_id"],
                "type": "past_ai_conversations",
                "provider": PROVIDER,
                "last_attempted_read": STORE_TIME.isoformat(),
                "last_successful_read": STORE_TIME.isoformat(),
                "last_outcome": "took_in",
                "read_count": 2,
                "failed_count": 0,
                "oldest_unprocessed_item": FIRST_AT.isoformat(),
                "watermark": "at:" + (FIRST_AT + timedelta(minutes=2)).isoformat(),
            }
        ]
    }
    # Less than the planted session's hour, so the cookie still holds.
    test_clock.advance(minutes=30)
    assert upload(client).json()["state"] == "nothing_new"
    [row] = client.get(CONNECTORS_PATH).json()["connectors"]
    assert row["last_outcome"] == "nothing_new"
    assert row["read_count"] == 2
    assert row["last_attempted_read"] == (STORE_TIME + timedelta(minutes=30)).isoformat()


def test_the_connectors_read_sorts_by_type_then_provider(
    driver, seeded_world, client_for, test_clock
):
    """Rows are made out of order; a row never read shows empty coverage, not a guess."""
    for connector_type, provider in (
        ("whatsapp", "zz_b"),
        ("past_ai_conversations", "zz_c"),
        ("whatsapp", "zz_a"),
        ("calendar", "zz_d"),
    ):
        ensure_connector(
            driver, seeded_world, connector_type, provider, batch_rule="session_delta.v1"
        )
    rows = client_for(seeded_world).get(CONNECTORS_PATH).json()["connectors"]
    assert [(row["type"], row["provider"]) for row in rows] == [
        ("calendar", "zz_d"),
        ("past_ai_conversations", "zz_c"),
        ("whatsapp", "zz_a"),
        ("whatsapp", "zz_b"),
    ]
    for row in rows:
        assert row["last_attempted_read"] is None
        assert row["last_outcome"] is None
        assert (row["read_count"], row["failed_count"]) == (0, 0)
        assert row["oldest_unprocessed_item"] is None
        assert row["watermark"] is None


def test_the_connectors_read_needs_a_session(anonymous, test_clock):
    assert_refused(anonymous.get(CONNECTORS_PATH), 401, "unauthenticated")
