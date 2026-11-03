"""Shared pieces of the runner tests: a synthetic `store` request, served apps, and tokens."""

import hashlib
import secrets
import socket
import threading
from datetime import UTC, datetime

import httpx
import uvicorn

from grove.ids import new_id
from grove.runner.passes import PassSettings, RunnerAuth
from grove.runner.tokens import runner_token_hash
from grove.screening import Message, Screener, ScreeningConfig
from grove.writer import Caller, enqueue_request, record_connector
from grove.writer.sources import write_screened_source
from grove.writer.worlds import create_world

HOST = Caller(kind="ai_host", scopes=frozenset({"grove.read", "grove.write"}), actor="host:test")
# Invented people, places and plans only (R3).
WORDS = (
    "Remember this: Lina Okafor is organising the spring garden fair at the Elm Street "
    "community hall."
)


def seeded_world(driver, world_id: str) -> str:
    create_world(driver, world_id, owner_user_id=f"u_{world_id[-12:]}")
    return world_id


def queue_store(
    driver,
    world_id: str,
    text: str = WORDS,
    *,
    request_id: str | None = None,
    at: datetime = datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
) -> str:
    """A `store` as hosts-b will queue it: an ai_host source, screened, with its request row."""
    connector = record_connector(driver, world_id, "ai_host")
    screened = Screener(ScreeningConfig()).screen_source(
        "", [Message(key="m1", sender=None, text=text, at=at)]
    )
    key = hashlib.sha256(screened.content().encode("utf-8")).hexdigest()

    def write_source(tx) -> str:
        source_id = new_id(tx.world_id)
        write_screened_source(
            tx, screened, source_id=source_id, connector_id=connector, parser_version="test-1"
        )
        return source_id

    request_id = request_id or f"store-{secrets.token_hex(6)}"
    enqueue_request(
        driver,
        world_id,
        HOST,
        request_id,
        write_source=write_source,
        content_key=key,
        verbatim=True,
    )
    return request_id


def new_auth() -> tuple[RunnerAuth, str]:
    """A fresh gateway secret and runner token, and the API's half of them."""
    token = secrets.token_urlsafe(32)
    secret = secrets.token_bytes(32)
    return RunnerAuth(gateway_secret=secret, runner_token_sha256=runner_token_hash(token)), token


class Served:
    """An ASGI app under uvicorn on a free loopback port, in a thread."""

    def __init__(self, app, health: str = "/healthz") -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.health = health
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "Served":
        self.thread.start()
        with httpx.Client(trust_env=False) as client:
            for _ in range(400):
                try:
                    client.get(self.url + self.health, timeout=1)
                    return self
                except httpx.TransportError:
                    threading.Event().wait(0.05)
        raise RuntimeError("the app did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


FAST = PassSettings(time_cap_seconds=120, lease_margin_seconds=30, token_cap=400_000)
