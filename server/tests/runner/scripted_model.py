"""A scripted OpenAI-compatible chat model for the OpenCode permission tests (R10).

No model is ever called. A stdlib HTTP server on a free loopback port, in a thread of the test:

- `GET /v1/models` lists the one model. Every GET path is recorded in `gets`, so a fetch by
  OpenCode's webfetch tool (to any path on this server) shows up there.
- `POST /v1/chat/completions` streams server-sent events. A request that offers tools takes the
  next scripted step, `{"tool": name, "args": {...}}` or `{"text": "..."}`, and answers "done"
  when the steps run out. A request with no tools is OpenCode's title call and gets a short title.
  The usage chunk comes last, then `[DONE]`.
- Each request that offers tools is recorded in `offers` as the agent that sent it (read from the
  heading of its system prompt, `# Learner` gives "learner") and the sorted names of the tools it
  offers. Every tool result that a request carries back is kept in `tool_results`, by call id:
  that is what the agent got back from the call.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "muse-spark-1.3"
TITLE = "probe"
DONE = "done"
_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def _chunk(delta: dict[str, Any] | None, finish: str | None = None, **extra: Any) -> bytes:
    body: dict[str, Any] = {
        "id": "chatcmpl-scripted",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }
    return f"data: {json.dumps(body)}\n\n".encode()


def _text(content: Any) -> str:
    """A message's content as text: a string, or the text parts of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


def _agent(messages: list[Any]) -> str:
    """The agent named by the heading of the first system prompt, lower case; "" if none."""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            first = _text(message.get("content")).lstrip().split("\n", 1)[0]
            return first.removeprefix("# ").strip().lower() if first.startswith("# ") else ""
    return ""


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, *_: object) -> None:
        pass

    def _read_body(self) -> bytes:
        if "chunked" in self.headers.get("transfer-encoding", "").lower():
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(parts)
                parts.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def do_GET(self) -> None:
        self.server.model.record_get(self.path)
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            kind = "application/json"
        else:
            body, kind = "scripted model", "text/plain"
        data = body.encode()
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        try:
            request = json.loads(self._read_body() or b"{}")
        except ValueError:
            request = {}
        step, call_id = self.server.model.next_step(request if isinstance(request, dict) else {})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        if "tool" in step:
            call = {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))},
            }
            self.wfile.write(_chunk({"role": "assistant", "tool_calls": [call]}))
            self.wfile.write(_chunk({}, "tool_calls"))
        else:
            self.wfile.write(_chunk({"role": "assistant", "content": step["text"]}))
            self.wfile.write(_chunk({}, "stop"))
        self.wfile.write(_chunk(None, usage=_USAGE))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    model: "ScriptedModel"


class ScriptedModel:
    """Use as a context manager. `script(steps)` starts a fresh script and fresh records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: list[dict[str, Any]] = []
        self._cursor = 0
        self._calls = 0
        self.offers: list[tuple[str, list[str]]] = []
        self.tool_results: dict[str, str] = {}
        self.gets: list[str] = []
        self.title_requests = 0
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.model = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def gateway_url(self) -> str:
        """The OpenAI-compatible base URL, as GROVE_GATEWAY_URL takes it."""
        return f"{self.base_url}/v1"

    def script(self, steps: list[dict[str, Any]]) -> None:
        with self._lock:
            self._steps = [dict(step) for step in steps]
            self._cursor = 0
            self.offers = []
            self.tool_results = {}
            self.gets = []
            self.title_requests = 0

    def record_get(self, path: str) -> None:
        with self._lock:
            self.gets.append(path)

    def next_step(self, request: dict[str, Any]) -> tuple[dict[str, Any], str]:
        tools = request.get("tools") or []
        names = sorted(
            str((tool.get("function") or {}).get("name") or tool.get("name"))
            for tool in tools
            if isinstance(tool, dict)
        )
        messages = request.get("messages") or []
        results = {
            str(message.get("tool_call_id")): _text(message.get("content"))
            for message in messages
            if isinstance(message, dict) and message.get("role") == "tool"
        }
        with self._lock:
            self.tool_results.update(results)
            self._calls += 1
            call_id = f"call_{self._calls}"
            if not tools:
                self.title_requests += 1
                return {"text": TITLE}, call_id
            self.offers.append((_agent(messages), names))
            if self._cursor < len(self._steps):
                step = self._steps[self._cursor]
                self._cursor += 1
                return step, call_id
            return {"text": DONE}, call_id

    @property
    def steps_taken(self) -> int:
        with self._lock:
            return self._cursor

    def __enter__(self) -> "ScriptedModel":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
