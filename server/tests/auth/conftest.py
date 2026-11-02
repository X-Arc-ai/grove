"""Sign-in against the real store: a capture mailer, a fake Google behind MockTransport, no network.

Addresses are invented (example.test). Every account and world a test makes is purged at its end.
"""

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.mail import MailFailed
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer.accounts import email_hash
from grove.writer.testing import purge_test_account, purge_test_world
from tests.conftest import slice_id

BASE = "https://grove.example"
LOOPBACK = "http://127.0.0.1:8000"
CLIENT_ID = "client-id.example.test"
CLIENT_SECRET = "client-secret-for-tests"
AUTH_URI = "https://accounts.example.test/auth"
TOKEN_URI = "https://accounts.example.test/token"
START = "/v1/auth/email/start"
VERIFY = "/v1/auth/email/verify"
GOOGLE_START = "/v1/auth/google/start"
GOOGLE_CALLBACK = "/v1/auth/google/callback"
LOGOUT = "/v1/auth/logout"
SESSION_VIEW = "/v1/views/session"
SIGNIN_ERROR = "/signin?signin_error=google"
_WORLDS = UUID("9c3e7f02-5b1a-4d6e-8f20-1a2b3c4d5e6f")


class CaptureMailer:
    """Keeps each code in place of sending it. `fail` makes the next sends refuse."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def send_code(self, to: str, code: str) -> None:
        if self.fail:
            raise MailFailed(500)
        self.sent.append((to, code))

    def last_code(self, to: str) -> str:
        return next(code for sent_to, code in reversed(self.sent) if sent_to == to)


@dataclass
class Accounts:
    """Makes invented addresses and test worlds, and remembers them for the purge."""

    hashes: set[str] = field(default_factory=set)
    worlds: set[str] = field(default_factory=set)

    def address(self) -> str:
        address = f"{uuid4().hex}@example.test"
        self.hashes.add(email_hash(address))
        return address

    def world_id_for(self, user_id: str) -> str:
        """One id per user, as the real default gives, so racers make one world."""
        world_id = f"t_{slice_id()}_{uuid5(_WORLDS, user_id)}"
        self.worlds.add(world_id)
        return world_id


@pytest.fixture
def accounts(driver, migrated):
    made = Accounts()
    yield made
    for world_id in made.worlds:
        purge_test_world(driver, world_id)
    for hashed in made.hashes:
        purge_test_account(driver, hashed)


@pytest.fixture
def mailer():
    return CaptureMailer()


def id_token(claims: dict) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}."


@dataclass
class FakeGoogle:
    """The token endpoint. `answer` overrides the whole answer; `claims` overrides one claim."""

    claims: dict = field(default_factory=dict)
    answer: httpx.Response | None = None
    requests: list[dict] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        self.requests.append({"url": str(request.url), "form": form})
        if self.answer is not None:
            return self.answer
        return httpx.Response(200, json={"id_token": id_token(self.claims)})

    def good_claims(self, address: str, nonce: str, name: str = "Maya Chen") -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "exp": clock.now().timestamp() + 3600,
            "nonce": nonce,
            "sub": "1234567890",
            "email": address,
            "email_verified": True,
            "name": name,
        }


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def client_file(tmp_path):
    path = tmp_path / "google-oauth.json"
    path.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "auth_uri": AUTH_URI,
                    "token_uri": TOKEN_URI,
                }
            }
        )
    )
    return str(path)


@pytest.fixture
def make_app(driver, accounts, mailer, google, client_file):
    def make(base: str = BASE, *, oauth_file: str | None = client_file, **options):
        settings = ApiSettings(public_base_url=base, google_oauth_file=oauth_file)
        http = httpx.Client(transport=httpx.MockTransport(google.handle))
        return create_app(
            settings,
            driver=driver,
            mailer=options.pop("mailer", mailer),
            google_http=http,
            world_id_for=accounts.world_id_for,
            **options,
        )

    return make


def browser(app, base: str = BASE) -> TestClient:
    """A client with its own cookie jar, which follows no redirect and shows a 500 as a 500."""
    return TestClient(app, base_url=base, follow_redirects=False, raise_server_exceptions=False)


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return browser(app)


def email_sign_in(client: TestClient, mailer: CaptureMailer, address: str):
    assert client.post(START, json={"email": address}).status_code == 200
    return client.post(VERIFY, json={"email": address, "code": mailer.last_code(address)})


def google_start(client: TestClient, next_path: str | None = None) -> dict[str, str]:
    """Asks for the redirect and returns its query as a flat dict."""
    params = {"next": next_path} if next_path is not None else {}
    response = client.get(GOOGLE_START, params=params)
    assert response.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


def google_sign_in(client: TestClient, google: FakeGoogle, address: str, **claims):
    query = google_start(client)
    google.claims = {**google.good_claims(address, query["nonce"]), **claims}
    return client.get(GOOGLE_CALLBACK, params={"code": "a-code", "state": query["state"]})


def cookie_flags(response, name: str) -> tuple[str, set[str]]:
    """(the value, the lowercase attributes) of the Set-Cookie header for that name."""
    for header in response.headers.get_list("set-cookie"):
        parts = [part.strip() for part in header.split(";")]
        key, _, value = parts[0].partition("=")
        if key == name:
            return value, {part.lower() for part in parts[1:]}
    raise AssertionError(f"no Set-Cookie for {name}")


def user_of(driver, address: str) -> dict | None:
    rows = PlatformQuery(driver).read(
        "MATCH (u:User {email_hash: $h}) RETURN u.id AS id, u.created_at AS created_at",
        h=email_hash(address),
    )
    return rows[0] if rows else None


def worlds_of(driver, user_id: str) -> list[dict]:
    return PlatformQuery(driver).read(
        "MATCH (w:World {owner_user_id: $id}) "
        "RETURN w.id AS id, w.self_item_id AS self_item_id, w.seed_version AS seed_version",
        id=user_id,
    )


def sessions_of(driver, user_id: str) -> list[dict]:
    return PlatformQuery(driver).read(
        "MATCH (s:Session {user_id: $id}) "
        "RETURN s.token_hash AS token_hash, s.method AS method, s.revoked_at AS revoked_at, "
        "s.expires_at AS expires_at",
        id=user_id,
    )


def codes_of(driver, address: str) -> list[dict]:
    return PlatformQuery(driver).read(
        "MATCH (c:EmailCode {email_hash: $h}) RETURN c ORDER BY c.created_at",
        h=email_hash(address),
    )


# What a logout must leave alone. The seed's rows are here too: a world keeps all of them.
WORLD_LABELS_COUNTED = (
    "Item",
    "Evidence",
    "ChangeEntry",
    "HistoryValue",
    "Kind",
    "Meaning",
    "Alias",
    "Connector",
    "Source",
    "Episode",
)


def world_counts(driver, world_id: str) -> dict[str, int]:
    query = WorldQuery(driver, world_id)
    return {
        label: query.read(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
        for label in WORLD_LABELS_COUNTED
    }


def items_of(driver, world_id: str) -> list[dict]:
    return WorldQuery(driver, world_id).read(
        "MATCH (i:Item) RETURN i.id AS id, i.title AS title, i.kind AS kind, i.version AS version"
    )
