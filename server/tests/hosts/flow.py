"""The synthetic connector's flow test, ported: its 14 checks against the real app over real HTTP.

The original drove a synthetic server with urllib and followed no redirect; this port does the
same against `create_app(...)` served by uvicorn on a free loopback port, in a thread. The steps
and the check names are the original's. Where the original pinned the synthetic server's own
shape, the port pins this server's: check 4 wants a UUIDv7 client id, and the consent GET and the
decision POST carry the session cookie of a signed-in person, as a browser would.

`run_checks` runs the 14 steps once, in order, and records each one as a `Check`: whether the
original's condition held, a short detail, the replies it saw and the values later steps use. A
step that raises is recorded as failed with the error, and the run goes on, so each test of
`test_flow_checks.py` fails on its own step. Nothing here prints a token or a cookie.
"""

import base64
import hashlib
import html
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import Message
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI

from grove.views.session import SESSION_COOKIE

HOST = "127.0.0.1"
TIMEOUT_SECONDS = 20
POLL_SECONDS = 0.1
POLLS = 300
ACCEPT = "application/json, text/event-stream"
CLIENT_NAME = "flow-test"
REDIRECT_URI = "http://127.0.0.1:9/cb"
STATE = "st-123"
# The original asks for OpenID scopes only: Grove drops them and grants its default.
FOREIGN_SCOPE = "openid profile email offline_access"
PROTOCOL_VERSION = "2025-06-18"

# The 14 checks, named as the original names them, in its order.
UNAUTHENTICATED = "unauthenticated /mcp returns 401 with resource_metadata"
RESOURCE_METADATA = "protected resource metadata"
SERVER_METADATA = "authorization server metadata (S256, registration)"
REGISTRATION = "dynamic client registration (public client, foreign scopes)"
CONSENT = "consent page renders"
APPROVE = "approve redirects with code, state, iss"
WRONG_VERIFIER = "wrong PKCE verifier is rejected"
EXCHANGE = "token exchange with PKCE"
INITIALIZE = "MCP initialize"
INITIALIZED = "initialized notification -> 202"
TOOLS_LIST = "tools/list"
AS_OF = "grove_as_of 2026-10-13: true=55 USD, Grove did not know yet"
RETAIN = "grove_retain closes the old launch belief"
REFRESH = "refresh token grant"
CHECKS = (
    UNAUTHENTICATED,
    RESOURCE_METADATA,
    SERVER_METADATA,
    REGISTRATION,
    CONSENT,
    APPROVE,
    WRONG_VERIFIER,
    EXCHANGE,
    INITIALIZE,
    INITIALIZED,
    TOOLS_LIST,
    AS_OF,
    RETAIN,
    REFRESH,
)

# The original's form reader: every name="..." value="..." pair of the page. The buttons match
# too, and the decision is set after.
_PAIRS = re.compile(r'name="([^"]+)" value="([^"]*)"')
_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')


# HTTP -------------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# No proxy from the environment may stand between the test and the loopback server.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


@dataclass(frozen=True)
class Reply:
    """One HTTP answer: its status, its headers (names match with no case) and its text."""

    method: str
    path: str
    status: int
    # Neither prints: a body or a header may hold a token.
    headers: Message = field(repr=False)
    body: str = field(repr=False)

    def header(self, name: str) -> str:
        return self.headers.get(name) or ""

    def json(self) -> Any:
        return json.loads(self.body)


def _brief(reply: Reply) -> str:
    """The status and an OAuth or typed error code: never the body, which may hold a token."""
    try:
        error = reply.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if isinstance(error, dict):
        error = error.get("code")
    return f"{reply.status} {error}" if isinstance(error, str) else str(reply.status)


def call(
    method: str,
    url: str,
    data: Any = None,
    headers: dict[str, str] | None = None,
    *,
    form: bool = False,
) -> Reply:
    """One request with urllib, as the original sends it: JSON, or a form with form=True."""
    sent = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            sent["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=sent, method=method)
    path = urllib.parse.urlsplit(url).path
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as answer:
            return Reply(method, path, answer.status, answer.headers, answer.read().decode())
    except urllib.error.HTTPError as refused:
        with refused:
            return Reply(method, path, refused.code, refused.headers, refused.read().decode())


def session_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": ACCEPT}


def query_of(location: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))


def form_fields(page: str) -> dict[str, str]:
    return {name: html.unescape(value) for name, value in _PAIRS.findall(page)}


def is_uuid7(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 7 and str(parsed) == value


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return Pkce(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())


def authorize_query(client_id: str, pkce: Pkce, resource: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": STATE,
        "scope": FOREIGN_SCOPE,
        "resource": resource,
    }


def consent_page(authorize_url: str, query: dict[str, str], session_token: str) -> Reply:
    return call(
        "GET",
        f"{authorize_url}?{urllib.parse.urlencode(query)}",
        None,
        session_cookie(session_token),
    )


def approve(base: str, page: Reply, session_token: str) -> Reply:
    fields = form_fields(page.body)
    fields["decision"] = "approve"
    return call(
        "POST", f"{base}/authorize/decision", fields, session_cookie(session_token), form=True
    )


def exchange(token_url: str, client_id: str, code: str, verifier: str) -> Reply:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    return call("POST", token_url, form, form=True)


def connect(base: str, session_token: str, client_id: str) -> dict[str, Any]:
    """The whole sign-in of a host for one person, over HTTP: the consent page, Approve and the
    exchange. Returns the token answer; fails loudly on any other answer."""
    pkce = new_pkce()
    page = consent_page(
        f"{base}/authorize", authorize_query(client_id, pkce, f"{base}/mcp"), session_token
    )
    if page.status != 200:
        raise AssertionError(f"the consent page answered {page.status}")
    decided = approve(base, page, session_token)
    code = query_of(decided.header("Location")).get("code")
    if decided.status != 302 or not code:
        raise AssertionError(f"the decision answered {decided.status} with no code")
    tokens = exchange(f"{base}/token", client_id, code, pkce.verifier)
    if tokens.status != 200:
        raise AssertionError(f"the exchange answered {tokens.status}")
    return tokens.json()


def rpc(base: str, access_token: str, message: dict[str, Any]) -> Reply:
    return call("POST", f"{base}/mcp", message, bearer(access_token))


def tool_call(base: str, access_token: str, call_id: int, name: str, arguments: dict) -> Reply:
    params = {"name": name, "arguments": arguments}
    message = {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": params}
    return rpc(base, access_token, message)


# The server -------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


@contextmanager
def serving(app: FastAPI, port: int) -> Iterator[str]:
    """The app under uvicorn on HOST:port in a thread, until the block ends. Yields the origin."""
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="grove-hosts-flow", daemon=True)
    thread.start()
    try:
        for _ in range(POLLS):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("the app stopped while it started")
            time.sleep(POLL_SECONDS)
        else:
            raise RuntimeError("the app did not start")
        yield f"http://{HOST}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


# The checks -------------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool = False
    detail: str = ""
    replies: list[Reply] = field(default_factory=list, repr=False)

    def reply(self, index: int = -1) -> Reply:
        return self.replies[index]


@dataclass
class FlowResult:
    base: str
    checks: dict[str, Check] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict, repr=False)

    def check(self, name: str) -> Check:
        if name not in self.checks:
            raise AssertionError(f"the flow did not reach the check {name!r}")
        return self.checks[name]


class _Run:
    """The original's steps, one method per check, sharing what they learn in `values`."""

    def __init__(self, base: str, session_token: str, track_client: Callable[[str], object]):
        self.base = base
        self.session_token = session_token
        self.track_client = track_client
        self.result = FlowResult(base)
        self.values = self.result.values

    def _record(self, name: str, passed: object, detail: str, *replies: Reply) -> None:
        self.result.checks[name] = Check(name, bool(passed), detail, list(replies))

    def unauthenticated(self) -> None:
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        reply = call("POST", f"{self.base}/mcp", message, {"Accept": ACCEPT})
        challenge = reply.header("WWW-Authenticate")
        found = _RESOURCE_METADATA.search(challenge)
        if found:
            self.values["prm_url"] = found.group(1)
        passed = reply.status == 401 and "resource_metadata=" in challenge
        self._record(UNAUTHENTICATED, passed, f"{reply.status} {challenge[:120]}", reply)

    def resource_metadata(self) -> None:
        reply = call("GET", self.values["prm_url"])
        prm = reply.json()
        self.values["prm"] = prm
        passed = reply.status == 200 and prm["resource"].endswith("/mcp")
        self._record(RESOURCE_METADATA, passed, str(prm.get("resource")), reply)

    def server_metadata(self) -> None:
        issuer = self.values["prm"]["authorization_servers"][0]
        reply = call("GET", f"{issuer}/.well-known/oauth-authorization-server")
        asm = reply.json()
        self.values["asm"] = asm
        passed = (
            reply.status == 200
            and "S256" in asm["code_challenge_methods_supported"]
            and asm.get("registration_endpoint")
        )
        self._record(SERVER_METADATA, passed, str(asm.get("registration_endpoint")), reply)

    def registration(self) -> None:
        asked = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "scope": FOREIGN_SCOPE,
        }
        reply = call("POST", self.values["asm"]["registration_endpoint"], asked)
        client = reply.json()
        client_id = client.get("client_id")
        if isinstance(client_id, str):
            # Purged with the run's worlds, whatever the checks find.
            self.track_client(client_id)
            self.values["client_id"] = client_id
        passed = reply.status == 201 and is_uuid7(client_id)
        self._record(REGISTRATION, passed, f"{reply.status} {client_id}", reply)

    def _consent(self, pkce: Pkce) -> Reply:
        query = authorize_query(self.values["client_id"], pkce, self.values["prm"]["resource"])
        return consent_page(self.values["asm"]["authorization_endpoint"], query, self.session_token)

    def consent(self) -> None:
        pkce = new_pkce()
        self.values["pkce"] = pkce
        reply = self._consent(pkce)
        self.values["consent_page"] = reply
        passed = reply.status == 200 and "Approve" in reply.body
        self._record(CONSENT, passed, str(reply.status), reply)

    def approve(self) -> None:
        reply = approve(self.base, self.values["consent_page"], self.session_token)
        back = query_of(reply.header("Location"))
        self.values["code"] = back.get("code")
        passed = (
            reply.status == 302
            and back.get("code")
            and back.get("state") == STATE
            and back.get("iss") == self.base
        )
        self._record(APPROVE, passed, f"{reply.status} state={back.get('state')}", reply)

    def wrong_verifier(self) -> None:
        token_url = self.values["asm"]["token_endpoint"]
        wrong = "wrong" + self.values["pkce"].verifier
        reply = exchange(token_url, self.values["client_id"], self.values["code"], wrong)
        passed = reply.status == 400 and "invalid_grant" in reply.body
        self._record(WRONG_VERIFIER, passed, _brief(reply), reply)

    def exchange(self) -> None:
        # The failed attempt spent the code: a fresh consent and Approve give a new one.
        pkce = new_pkce()
        page = self._consent(pkce)
        decided = approve(self.base, page, self.session_token)
        code = query_of(decided.header("Location"))["code"]
        reply = exchange(
            self.values["asm"]["token_endpoint"], self.values["client_id"], code, pkce.verifier
        )
        tokens = reply.json()
        self.values["tokens"] = tokens
        passed = (
            reply.status == 200
            and tokens.get("token_type") == "Bearer"
            and tokens.get("refresh_token")
        )
        self._record(EXCHANGE, passed, str(reply.status), page, decided, reply)

    def _rpc(self, message: dict[str, Any]) -> Reply:
        return rpc(self.base, self.values["tokens"]["access_token"], message)

    def initialize(self) -> None:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "0"},
        }
        reply = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        answer = reply.json()
        passed = (
            reply.status == 200
            and answer["result"]["protocolVersion"] == PROTOCOL_VERSION
            and reply.header("Mcp-Session-Id")
        )
        self._record(INITIALIZE, passed, str(reply.status), reply)

    def initialized(self) -> None:
        reply = self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._record(INITIALIZED, reply.status == 202, str(reply.status), reply)

    def tools_list(self) -> None:
        reply = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in reply.json()["result"]["tools"]]
        self.values["tool_names"] = names
        passed = names == ["grove_recall", "grove_as_of", "grove_retain"]
        self._record(TOOLS_LIST, passed, ",".join(names), reply)

    def as_of(self) -> None:
        arguments = {"topic": "pricing", "date": "2026-10-13"}
        reply = tool_call(
            self.base, self.values["tokens"]["access_token"], 3, "grove_as_of", arguments
        )
        found = reply.json()["result"]["structuredContent"]
        true_ids = [belief["id"] for belief in found["true_on_date"]]
        passed = true_ids == ["b2"] and found["known_by_grove_on_date"] == []
        self._record(AS_OF, passed, f"true={true_ids}", reply)

    def retain(self) -> None:
        arguments = {
            "topic": "launch",
            "statement": "Kestrel Teams launch moved to 2026-12-01.",
            "valid_from": "2026-10-26",
        }
        reply = tool_call(
            self.base, self.values["tokens"]["access_token"], 4, "grove_retain", arguments
        )
        found = reply.json()["result"]["structuredContent"]
        self._record(RETAIN, found["closed"] == ["b3"], f"closed={found['closed']}", reply)

    def refresh(self) -> None:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.values["tokens"]["refresh_token"],
            "client_id": self.values["client_id"],
        }
        reply = call("POST", self.values["asm"]["token_endpoint"], form, form=True)
        refreshed = reply.json()
        self.values["refreshed"] = refreshed
        passed = reply.status == 200 and refreshed.get("access_token")
        replies = [reply]
        if passed:
            # Beyond the original: the new access token reaches /mcp.
            ping = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
            replies.append(rpc(self.base, refreshed["access_token"], ping))
        self._record(REFRESH, passed, str(reply.status), *replies)

    def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
        return (
            (UNAUTHENTICATED, self.unauthenticated),
            (RESOURCE_METADATA, self.resource_metadata),
            (SERVER_METADATA, self.server_metadata),
            (REGISTRATION, self.registration),
            (CONSENT, self.consent),
            (APPROVE, self.approve),
            (WRONG_VERIFIER, self.wrong_verifier),
            (EXCHANGE, self.exchange),
            (INITIALIZE, self.initialize),
            (INITIALIZED, self.initialized),
            (TOOLS_LIST, self.tools_list),
            (AS_OF, self.as_of),
            (RETAIN, self.retain),
            (REFRESH, self.refresh),
        )


def run_checks(
    base: str, session_token: str, *, track_client: Callable[[str], object]
) -> FlowResult:
    """The 14 steps once, in order, against the app at `base`, for the person whose session
    token is `session_token`. A client the run registers goes to track_client first."""
    run = _Run(base, session_token, track_client)
    for name, step in run.steps():
        try:
            step()
        except Exception as failed:  # one step's failure is that check's result alone
            run.result.checks[name] = Check(name, False, f"{type(failed).__name__}: {failed}")
    return run.result
