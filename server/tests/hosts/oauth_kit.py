"""The shared steps of the OAuth route tests; this module holds no test of its own.

An app on a small consent template (the two marks of web/consent.html), a browser per person with
the grove_session cookie, PKCE pairs, and the steps a host takes: authorize, approve, exchange.
Every name and address is invented. The store rows come from hosts_plant (tests/hosts/conftest.py),
which purges them, and a client that the API registers goes to hosts_plant.track_client.
"""

import html
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import WEB_DIST_ENV, ApiSettings
from grove.oauth.pkce import s256
from grove.views.session import SESSION_COOKIE
from tests.hosts.conftest import LOOPBACK_REDIRECT, Person

BASE = "https://grove.example"
MCP_URL = f"{BASE}/mcp"
STATE = "st-4417"
DEFAULT_SCOPE = "grove.read grove.write"
TEMPLATE = (
    "<!doctype html><html><head><title><!--grove:title--></title></head>"
    "<body><main><!--grove:consent--></main></body></html>"
)
_HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(48)
    return Pkce(verifier, s256(verifier))


def built_consent_page(folder: Path, monkeypatch, template: str = TEMPLATE) -> Path:
    """A web/dist stand-in that holds consent.html, named by GROVE_WEB_DIST."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "consent.html").write_text(template, encoding="utf-8")
    monkeypatch.setenv(WEB_DIST_ENV, str(folder))
    return folder


def make_app(driver, base: str = BASE, **options) -> FastAPI:
    return create_app(ApiSettings(public_base_url=base), driver=driver, **options)


def browser(app: FastAPI, person: Person | None = None, base: str = BASE) -> TestClient:
    """A client with its own cookie jar that follows no redirect; with a person, signed in."""
    client = TestClient(app, base_url=base, follow_redirects=False, raise_server_exceptions=False)
    if person is not None:
        client.cookies.set(SESSION_COOKIE, person.token, domain=urlsplit(base).hostname, path="/")
    return client


def hidden_fields(page: str) -> dict[str, str]:
    return {name: html.unescape(value) for name, value in _HIDDEN.findall(page)}


def location(response: httpx.Response) -> tuple[str, dict[str, str]]:
    """(the Location without its query, the query as a flat dict)."""
    target = response.headers["location"]
    parts = urlsplit(target)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    return target.split("?", 1)[0], query


def authorize_query(
    client_id: str,
    pkce: Pkce,
    *,
    redirect_uri: str | None = LOOPBACK_REDIRECT,
    state: str | None = STATE,
    scope: str | None = DEFAULT_SCOPE,
    resource: str | None = MCP_URL,
    **extra: str,
) -> dict[str, str]:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scope,
        "resource": resource,
        **extra,
    }
    return {key: value for key, value in query.items() if value is not None}


def consent(web: TestClient, query: dict[str, str]) -> httpx.Response:
    page = web.get("/authorize", params=query)
    assert page.status_code == 200, page.text
    return page


def decide(web: TestClient, query: dict[str, str], decision: str = "approve") -> httpx.Response:
    """Shows the consent page, then posts its form with the person's decision."""
    fields = hidden_fields(consent(web, query).text)
    fields["decision"] = decision
    return web.post("/authorize/decision", data=fields)


def new_code(web: TestClient, client_id: str, pkce: Pkce, **query: str | None) -> str:
    answer = decide(web, authorize_query(client_id, pkce, **query))
    assert answer.status_code == 302, answer.text
    return location(answer)[1]["code"]


def exchange(
    host: TestClient,
    client_id: str,
    code: str,
    verifier: str,
    *,
    redirect_uri: str | None = LOOPBACK_REDIRECT,
    **extra: str,
) -> httpx.Response:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        **extra,
    }
    return host.post("/token", data={key: value for key, value in form.items() if value})


def refresh(host: TestClient, client_id: str, refresh_token: str, **extra: str) -> httpx.Response:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        **extra,
    }
    return host.post("/token", data=form)


def oauth_error(response: httpx.Response) -> str:
    body = response.json()
    assert set(body) == {"error", "error_description"}, body
    return body["error"]


def document(client_id: str, **fields: object) -> bytes:
    body = {
        "client_id": client_id,
        "client_name": "Lumen Desk",
        "redirect_uris": [LOOPBACK_REDIRECT],
        **fields,
    }
    return json.dumps({key: value for key, value in body.items() if value is not None}).encode()
