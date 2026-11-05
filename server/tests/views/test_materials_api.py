"""All material HTTP mutations use the real native writer in synthetic worlds."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.views.session import SESSION_COOKIE
from tests.views.test_read_only import _READ_ENTRY as READ_ENTRY
from tests.views.test_read_only import ReadOnlyDriver
from tests.writer import test_materials

material_world = test_materials.material_world


@pytest.fixture
def material_client(driver, material_world):
    app = create_app(driver=driver)
    client = TestClient(
        app, raise_server_exceptions=True, cookies={SESSION_COOKIE: material_world[1]}
    )
    client.headers.update(
        {
            "Origin": app.state.settings.public_base_url,
            "X-Grove-Action": "materials",
            "Idempotency-Key": str(uuid4()),
        }
    )
    return client


def test_note_create_reload_edit_and_delete(driver, material_world, material_client):
    client = material_client
    response = client.post(
        "/v1/materials/notes", json={"title": "Plan", "body": "Mira plans the launch."}
    )
    assert response.status_code == 201, response.text
    item = response.json()["material"]
    assert item["request"]["status"] == "queued"
    assert client.get("/v1/materials").json()["materials"][0]["id"] == item["id"]
    assert (
        client.get("/v1/materials/" + item["id"]).json()["material"]["parts"][0]["text"]
        == "Mira plans the launch."
    )
    client.headers["Idempotency-Key"] = str(uuid4())
    edited = client.put(
        "/v1/materials/notes/" + item["id"],
        json={"title": "Updated", "body": "Mira moved the launch.", "expected_revision": 1},
    )
    assert edited.status_code == 201, edited.text
    path = "/v1/materials/" + item["id"]
    assert client.get(path + "/delete-preview").json()["revisions"] == 2
    approval = client.post(
        path + "/delete-confirmations", json={"confirm": True, "expected_revision": 2}
    )
    assert approval.status_code == 200, approval.text
    result = client.post(
        path + "/delete",
        json={"approval_token": approval.json()["approval_token"], "expected_revision": 2},
    )
    assert result.status_code == 200, result.text
    assert result.json()["deleted"] is True
    assert client.get(path).status_code == 404
    assert client.get("/v1/materials").json()["materials"] == []


def test_the_reads_of_a_real_note_run_in_read_mode_only(driver, material_world, material_client):
    """An empty list never reaches summary or receipt_of, so this one holds a note."""
    created = material_client.post(
        "/v1/materials/notes", json={"title": "Plan", "body": "Mira plans the launch."}
    )
    assert created.status_code == 201, created.text
    item = created.json()["material"]
    guarded = ReadOnlyDriver(driver)
    reader = TestClient(
        create_app(driver=guarded),
        raise_server_exceptions=False,
        cookies={SESSION_COOKIE: material_world[1]},
    )
    for path in (
        "/v1/materials",
        "/v1/materials/" + item["id"],
        "/v1/materials/" + item["id"] + "/delete-preview",
    ):
        answer = reader.get(path)
        assert answer.status_code == 200, (path, answer.text)
    assert reader.get("/v1/materials").json()["materials"][0]["id"] == item["id"]
    assert guarded.calls and set(guarded.calls) == {READ_ENTRY}, set(guarded.calls)


def test_file_has_retained_text_and_no_request(material_client):
    response = material_client.post(
        "/v1/materials/files",
        content=b"A cedar plan.",
        headers={"Content-Type": "text/plain", "X-Grove-Filename": "plan.txt"},
    )
    assert response.status_code == 201, response.text
    material = response.json()["material"]
    assert material["graph_state"] == "held_provenance" and material["request"] is None
    assert material["parts"][0]["text"] == "A cedar plan."


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer synthetic"},
        {"Origin": "https://other.example"},
        {"X-Grove-Action": "wrong"},
    ],
)
def test_material_mutation_boundary(material_client, headers):
    response = material_client.post(
        "/v1/materials/notes", json={"title": "Title", "body": "Body"}, headers=headers
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


def test_anonymous_and_other_world_cannot_read_material(driver, material_client, make_world):
    from grove.writer.testing import plant_session

    response = material_client.post(
        "/v1/materials/notes", json={"title": "Private", "body": "A private plan."}
    )
    path = "/v1/materials/" + response.json()["material"]["id"]
    other = TestClient(
        material_client.app, cookies={SESSION_COOKIE: plant_session(driver, make_world())}
    )
    assert other.get(path).status_code == 404
    assert TestClient(material_client.app).get(path).status_code == 401


def test_note_rejects_spoofed_metadata_and_missing_key(material_client):
    response = material_client.post(
        "/v1/materials/notes", json={"title": "Title", "body": "Text", "at": "2000-01-01"}
    )
    assert response.status_code == 422
    del material_client.headers["Idempotency-Key"]
    assert (
        material_client.post(
            "/v1/materials/notes", json={"title": "Title", "body": "Text"}
        ).status_code
        == 422
    )


def test_unsupported_file_is_rejected_before_body_parse(material_client):
    response = material_client.post(
        "/v1/materials/files",
        content=b"not an image",
        headers={"Content-Type": "image/png", "X-Grove-Filename": "a.png"},
    )
    assert response.status_code == 415


def test_persisted_demo_refuses_before_material_body_read(driver, migrated, monkeypatch):
    from datetime import timedelta

    from grove import clock
    from grove.api.config import ApiSettings
    from grove.views.session import token_hash
    from grove.writer.accounts import create_session, email_hash, ensure_demo_account
    from grove.writer.testing import purge_test_account, purge_test_world
    from tests.auth.conftest import Accounts

    made = Accounts()
    hashed = email_hash(made.address())
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", hashed)
    try:
        account = ensure_demo_account(driver, hashed, world_id_for=made.world_id_for)
        token = "material-test-" + uuid4().hex
        create_session(
            driver,
            account.user_id,
            token_hash(token),
            method="email",
            expires_at=clock.now() + timedelta(minutes=5),
        )
        monkeypatch.delenv("GROVE_DEMO_EMAIL_HASH")
        app = create_app(ApiSettings("https://grove.example"), driver=driver)
        client = TestClient(app, cookies={SESSION_COOKIE: token})

        def body():
            raise AssertionError("the restricted request body was consumed")
            yield b""

        response = client.post(
            "/v1/materials/files",
            content=body(),
            headers={
                "Origin": "https://grove.example",
                "X-Grove-Action": "materials",
                "Content-Type": "text/plain",
                "Idempotency-Key": str(uuid4()),
                "X-Grove-Filename": "private.txt",
            },
        )
        assert response.status_code == 403
    finally:
        for world in made.worlds:
            purge_test_world(driver, world)
        for value in made.hashes:
            purge_test_account(driver, value)


def test_material_screening_counts_are_real_and_value_free(material_client):
    response = material_client.post(
        "/v1/materials/notes",
        json={
            "title": "Contact mira@cedar.example",
            "body": "Email mira@cedar.example for the plan.",
        },
    )
    assert response.status_code == 201, response.text
    material = response.json()["material"]
    assert material["screening_counts"]["contact_email"] == 2
    assert "mira@cedar.example" not in response.text
