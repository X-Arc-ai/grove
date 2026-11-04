import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.ingest.conftest import (  # noqa: F401
    app,
    client_for,
    seeded_world,
    test_clock,
)


@pytest.fixture
def fake_google():
    state = SimpleNamespace(
        requests=[],
        answer=lambda path, query: (
            200,
            {
                "items": [],
                "nextSyncToken": "sync-one",
                "timeZone": "UTC",
            },
        ),
        token_answer={},
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            state.requests.append((parsed.path, query))
            code, body = state.answer(parsed.path, query)
            self.reply(code, body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            # Store only request type; never collect credential-bearing form data.
            state.requests.append((self.path, {"grant_type": form.get("grant_type")}))
            self.reply(200, state.token_answer)

        def reply(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()
