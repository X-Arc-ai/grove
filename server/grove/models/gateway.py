"""The internal model gateway: OpenAI-compatible Chat Completions on 127.0.0.1 for OpenCode.

OpenCode's only provider is this gateway (section 4). Its provider key is a run token, which
binds one world, one run and one job. For every call the gateway:

1. checks the run token, the model (a contributor id is refused) and the job's budget gate, and
   the run's token cap when the token carries one;
2. keeps only the fields and message parts it knows (text, and images as data URLs: a provider
   never fetches a URL for an agent), sets the job's effort, raises a small output limit to the
   floor, adds a cache key per shared prefix, and asks for the usage chunk on a stream;
3. sends the call to Meta (Spark) or to the Glimmer endpoint on Modal, through the egress
   allowlist, with back-off on 429;
4. writes the call to `model_calls` with its tokens and cost, also when the call failed or the
   client closed the stream. A stream belongs to a pump task, not to the client: when the client
   leaves, the pump reads on for up to DRAIN_SECONDS to get the usage chunk, which Meta sends
   only at the end, and records an estimate when that chunk never comes.

The gateway holds the Meta key and the Glimmer key; the runner holds neither.
"""

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.models.config import (
    DEFAULT_OUTPUT_TOKENS,
    MIN_OUTPUT_TOKENS,
    Catalog,
    ModelSettings,
    ModelSpec,
    load_catalog,
    load_model_settings,
)
from grove.models.egress import MODEL_LAYER, AsyncAllowlistTransport, EgressPolicy
from grove.models.errors import ModelError, ModelRefused, ProviderError, Unauthenticated
from grove.models.gateway_auth import RunClaims, verify
from grove.models.http import MAX_ATTEMPTS, MODAL_ATTEMPTS, asend
from grove.models.layer import Usage, chat_usage, default_cache_key, glimmer_effort_fields
from grove.models.meter import Meter
from grove.store.connection import Driver
from grove.writer.model_calls import ModelCall

_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
# Fields Meta refuses with HTTP 400 on Chat Completions (probes of 2026-11-02), or that would
# let a caller pick its own effort outside the model's list.
_META_REFUSED = ("stop", "logprobs", "top_logprobs")
_EFFORT_FIELDS = ("reasoning_effort", "chat_template_kwargs")
_OUTPUT_FIELDS = ("max_tokens", "max_completion_tokens")
# Everything else an agent sends is dropped, so a field such as `store`, `ignore_eos` or
# `chat_template` never reaches a provider.
_KNOWN_FIELDS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "parallel_tool_calls",
        "response_format",
        "prompt_cache_key",
        "n",
        *_META_REFUSED,
        *_EFFORT_FIELDS,
        *_OUTPUT_FIELDS,
    }
)
_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_MESSAGE_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")
_DATA_IMAGE = "data:image/"
# After the client leaves, how long the pump reads on for the usage chunk.
DRAIN_SECONDS = 120.0
# Four characters a token: the estimate for a stream that ended before its usage chunk.
_CHARS_PER_TOKEN = 4
_BEARER = re.compile(r"(?i)bearer\s+\S+")


def _error(error: ModelError) -> JSONResponse:
    headers = {}
    wait = error.details.get("retry_after_seconds")
    if isinstance(wait, int) and not isinstance(wait, bool):
        # An OpenAI client reads the wait from the header, not from the body.
        headers["retry-after"] = str(wait)
    return JSONResponse(error.as_body(), status_code=error.status, headers=headers)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("a bearer run token is required")
    return token.strip()


def _effort(claims_job: Any, spec: ModelSpec, catalog: Catalog) -> str | None:
    """The job's effort when the model is the job's, else the model's lowest. Never none: every
    call carries its effort, and the job table sets it (D15). An effort the agent sends is
    dropped, so an agent cannot raise its own spend."""
    if not spec.efforts:
        return None
    job_model = catalog.model_for(claims_job)
    if job_model.id == spec.id and claims_job.effort in spec.efforts:
        return claims_job.effort
    return spec.efforts[0]


def _part(part: object) -> dict:
    if not isinstance(part, dict):
        raise ModelRefused("a content part must be an object")
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        return {"type": "text", "text": part["text"]}
    if part.get("type") == "image_url":
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url.startswith(_DATA_IMAGE):
            return {"type": "image_url", "image_url": {"url": url}}
    raise ModelRefused(
        "only text parts and images as data URLs are taken: a provider never fetches a URL"
    )


def _message(message: object) -> dict:
    if not isinstance(message, dict) or message.get("role") not in _ROLES:
        raise ModelRefused("a message must be an object with a known role")
    out = {key: message[key] for key in _MESSAGE_KEYS if key in message}
    content = out.get("content")
    if isinstance(content, list):
        out["content"] = [_part(part) for part in content]
    elif content is not None and not isinstance(content, str):
        raise ModelRefused("message content must be text or a list of parts")
    return out


def upstream_body(body: dict, *, spec: ModelSpec, effort: str | None, job: str) -> dict:
    out = {
        key: value
        for key, value in body.items()
        if key in _KNOWN_FIELDS and key not in _EFFORT_FIELDS
    }
    messages = out.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ModelRefused("messages must be a list with at least one message")
    out["messages"] = [_message(message) for message in messages]
    n = out.pop("n", 1)
    if n != 1:
        raise ModelRefused("n must be 1")
    wanted = [out[field] for field in _OUTPUT_FIELDS if isinstance(out.get(field), int)]
    limit = max(max(wanted) if wanted else DEFAULT_OUTPUT_TOKENS, MIN_OUTPUT_TOKENS)
    for field in _OUTPUT_FIELDS:
        out.pop(field, None)
    out["max_tokens"] = limit
    if out.get("stream"):
        out["stream_options"] = {**(out.get("stream_options") or {}), "include_usage": True}
    else:
        out.pop("stream_options", None)
    if spec.route == "meta":
        for field in _META_REFUSED:
            out.pop(field, None)
        if out.get("tool_choice") not in (None, "auto"):
            # Meta takes only "auto"; "none" means no tools at all.
            if out["tool_choice"] == "none":
                out.pop("tools", None)
            out["tool_choice"] = "auto"
            if "tools" not in out:
                out.pop("tool_choice")
        out["reasoning_effort"] = effort
        if not out.get("prompt_cache_key"):
            messages = out.get("messages") or []
            first = messages[0] if messages and isinstance(messages[0], dict) else {}
            prefix = first.get("content") if first.get("role") == "system" else None
            key = default_cache_key(job, prefix if isinstance(prefix, str) else None)
            if key:
                out["prompt_cache_key"] = key
    else:
        out.pop("prompt_cache_key", None)
        out.update(glimmer_effort_fields(effort))
    return out


class _StreamMeter:
    """Reads the SSE bytes as they pass, to keep the usage chunk and the finish reason."""

    def __init__(self) -> None:
        self._buffer = b""
        self.usage: dict | None = None
        self.finished = False
        self.done = False
        self.output = False
        self.chars = 0

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            self._line(line.strip())

    def close(self) -> None:
        if self._buffer.strip():
            self._line(self._buffer.strip())
        self._buffer = b""

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            self.done = True
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finished = True
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            self._count(delta.get("content"), output=True)
            self._count(delta.get("reasoning"), output=False)
            for call in delta.get("tool_calls") or []:
                self.output = True
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    self._count(function.get("arguments"), output=True)

    def _count(self, text: object, *, output: bool) -> None:
        if isinstance(text, str) and text:
            self.chars += len(text)
            self.output = self.output or output


def _estimate(input_chars: int, output_chars: int) -> Usage:
    """Tokens for a stream whose usage chunk never came: four characters a token."""
    return Usage(
        input_tokens=math.ceil(input_chars / _CHARS_PER_TOKEN),
        output_tokens=math.ceil(output_chars / _CHARS_PER_TOKEN),
    )


def _empty_completion(doc: dict) -> bool:
    choices = doc.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        return True
    content = message.get("content")
    return not (isinstance(content, str) and content.strip()) and not message.get("tool_calls")


class Gateway:
    def __init__(
        self,
        driver: Driver,
        *,
        settings: ModelSettings | None = None,
        catalog: Catalog | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._pending: set[asyncio.Task] = set()
        self.settings = settings or load_model_settings()
        self.catalog = catalog or load_catalog()
        self.secret = self.settings.require_gateway_secret()
        self.meter = Meter(driver, self.catalog, self.settings)
        self.policy = EgressPolicy.for_settings(self.settings)
        self.client = httpx.AsyncClient(
            transport=AsyncAllowlistTransport(self.policy, MODEL_LAYER, inner=transport),
            timeout=_TIMEOUT,
            follow_redirects=False,
        )

    def _route(self, spec: ModelSpec) -> tuple[str, dict[str, str]]:
        if spec.route == "meta":
            return (
                f"{self.settings.meta_base_url}/chat/completions",
                {"Authorization": f"Bearer {self.settings.require_meta_key()}"},
            )
        base, key = self.settings.require_glimmer()
        return f"{base}/chat/completions", {"Authorization": f"Bearer {key}"}

    def _record(
        self,
        claims: RunClaims,
        spec: ModelSpec,
        effort: str | None,
        usage: Usage,
        outcome: str,
        started: float,
        gpu_from: float | None = None,
    ) -> None:
        """`gpu_from` is the start of the attempt that answered: a cold start's 503s ran on no
        GPU of ours, so they are left out of a Glimmer call's GPU seconds."""
        now = clock.monotonic()
        seconds = max(now - started, 0.0)
        gpu_seconds = max(now - (started if gpu_from is None else gpu_from), 0.0)
        cost = self.catalog.cost(
            spec.id,
            input_tokens=usage.input_tokens,
            cached_tokens=usage.cached_tokens,
            output_tokens=usage.output_tokens,
            gpu_seconds=gpu_seconds if spec.route == "modal" else 0.0,
        )
        self.meter.record(
            claims.world_id,
            ModelCall(
                job=claims.job,
                model=spec.id,
                route=spec.route,
                outcome=outcome,
                at=self.meter.now(claims.world_id),
                effort=effort,
                run_id=claims.run_id,
                input_tokens=usage.input_tokens,
                cached_tokens=min(usage.cached_tokens, usage.input_tokens),
                output_tokens=usage.output_tokens,
                reasoning_tokens=min(usage.reasoning_tokens, usage.output_tokens),
                cost=cost,
                latency_ms=int(seconds * 1000),
            ),
        )

    def _admit(self, claims: RunClaims) -> None:
        job = self.catalog.job(claims.job)
        self.meter.gate(claims.world_id, job)
        if claims.token_cap is not None:
            self.meter.check_run_cap(claims.world_id, claims.run_id, claims.token_cap)

    async def chat_completions(self, request: Request) -> Response:
        try:
            claims = verify(self.secret, self.catalog, _bearer(request))
            try:
                body = await request.json()
            except ValueError as exc:
                raise ModelRefused("the body is not JSON") from exc
            if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                raise ModelRefused("the body must be an object with a model")
            spec = self.catalog.model(body["model"])
            if not spec.chat:
                raise ModelRefused("this model does not take chat")
            job = self.catalog.job(claims.job)
            effort = _effort(job, spec, self.catalog)
            out = upstream_body(body, spec=spec, effort=effort, job=claims.job)
            url, headers = self._route(spec)
            await run_in_threadpool(self._admit, claims)
        except ModelError as error:
            return _error(error)
        stream = bool(out.get("stream"))
        input_chars = len(json.dumps(out.get("messages"))) + len(json.dumps(out.get("tools") or []))
        started = clock.monotonic()
        attempt = [started]

        def timed() -> httpx.Request:
            attempt[0] = clock.monotonic()
            return self.client.build_request("POST", url, json=out, headers=headers)

        record = self._record
        try:
            response = await asend(
                self.client,
                timed,
                stream=stream,
                sleep=self._sleep,
                attempts=MODAL_ATTEMPTS if spec.route == "modal" else MAX_ATTEMPTS,
            )
        except httpx.RequestError:
            # No answer, or an answer whose body could not be read (a DecodingError).
            await run_in_threadpool(
                record, claims, spec, effort, Usage(), "provider_error", started, attempt[0]
            )
            return _error(ProviderError("the model provider did not answer"))
        if response.status_code >= 300:
            await response.aread()
            await response.aclose()
            outcome = "rate_limited" if response.status_code == 429 else "provider_error"
            await run_in_threadpool(
                record, claims, spec, effort, Usage(), outcome, started, attempt[0]
            )
            if response.status_code < 400:
                # Redirects are not followed: the provider must answer at the URL it was given.
                return _error(ProviderError("the model provider answered with a redirect"))
            return self._upstream_error(response)
        if not stream:
            try:
                doc = response.json()
            except ValueError:
                doc = None
            if not isinstance(doc, dict):
                await run_in_threadpool(
                    record, claims, spec, effort, Usage(), "provider_error", started, attempt[0]
                )
                return _error(ProviderError("the model provider's answer is not a JSON object"))
            usage = chat_usage(doc.get("usage"))
            # The answer passes on as it came; an empty one is recorded as such (D15).
            outcome = "invalid_output" if _empty_completion(doc) else "ok"
            await run_in_threadpool(
                record, claims, spec, effort, usage, outcome, started, attempt[0]
            )
            return JSONResponse(doc)
        return StreamingResponse(
            self._relay(response, claims, spec, effort, started, attempt[0], input_chars),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )

    def _redact(self, text: str) -> str:
        """A provider's message with the keys this gateway holds, and any bearer value, removed."""
        for secret in (self.settings.meta_key, self.settings.glimmer_key):
            if secret:
                text = text.replace(secret, "<redacted>")
        return _BEARER.sub("Bearer <redacted>", text)

    def _upstream_error(self, response: httpx.Response) -> JSONResponse:
        """The provider's status with its message, in the B.3 shape; nothing else of its body."""
        try:
            doc = response.json()
            message = self._redact(str(doc["error"]["message"]))
        except (ValueError, KeyError, TypeError):
            message = ""
        code = "rate_limited" if response.status_code == 429 else "provider_error"
        body = {
            "error": {
                "code": code,
                "type": code,
                "message": str(message)[:300] or "the model provider refused the call",
                "details": {"status": response.status_code},
                "retryable": response.status_code == 429 or response.status_code >= 500,
            }
        }
        headers = {}
        if "retry-after" in response.headers:
            headers["retry-after"] = response.headers["retry-after"]
        return JSONResponse(body, status_code=response.status_code, headers=headers)

    async def _relay(
        self,
        response: httpx.Response,
        claims: RunClaims,
        spec: ModelSpec,
        effort: str | None,
        started: float,
        gpu_from: float | None = None,
        input_chars: int = 0,
    ) -> AsyncIterator[bytes]:
        """The client's side of a stream: chunks from the pump's queue. The pump owns the upstream
        response and the record, so a client that leaves, or a cancel of this generator, can
        neither skip the record nor cut the read that brings the usage chunk."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        gone = asyncio.Event()
        pump = self._spawn(
            self._pump(response, queue, gone, claims, spec, effort, started, gpu_from, input_chars)
        )
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            # No await here: once the client is gone, an await in this block can be cancelled.
            if not pump.done():
                gone.set()
                cap = self._spawn(self._cap(pump))
                pump.add_done_callback(lambda _: cap.cancel())

    def _spawn(self, work: Any) -> asyncio.Task:
        task = asyncio.ensure_future(work)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    @staticmethod
    async def _cap(pump: asyncio.Task) -> None:
        await asyncio.sleep(DRAIN_SECONDS)
        pump.cancel()

    async def _pump(
        self,
        response: httpx.Response,
        queue: asyncio.Queue,
        gone: asyncio.Event,
        claims: RunClaims,
        spec: ModelSpec,
        effort: str | None,
        started: float,
        gpu_from: float | None,
        input_chars: int,
    ) -> None:
        watch = _StreamMeter()
        outcome: str | None = None
        try:
            async for chunk in response.aiter_raw():
                watch.feed(chunk)
                if not gone.is_set():
                    queue.put_nowait(chunk)
            watch.close()
            if gone.is_set():
                outcome = "client_closed"
            elif watch.finished or watch.done:
                outcome = "ok" if watch.output else "invalid_output"
            else:
                outcome = "provider_error"
        except httpx.HTTPError:
            outcome = "client_closed" if gone.is_set() else "provider_error"
        finally:
            # The cap's cancel lands here too: the record runs first, with no await before it.
            if outcome is None:
                outcome = "client_closed" if gone.is_set() else "provider_error"
            usage = chat_usage(watch.usage) if watch.usage else _estimate(input_chars, watch.chars)
            try:
                self._record(claims, spec, effort, usage, outcome, started, gpu_from)
            finally:
                queue.put_nowait(None)
                try:
                    await response.aclose()
                except (httpx.HTTPError, asyncio.CancelledError):
                    pass

    async def models(self, request: Request) -> Response:
        try:
            verify(self.secret, self.catalog, _bearer(request))
        except ModelError as error:
            return _error(error)
        data = [
            {"id": spec.id, "object": "model", "owned_by": "grove"}
            for spec in self.catalog.models.values()
            if spec.chat
        ]
        return JSONResponse({"object": "list", "data": data})

    async def settle(self, timeout: float = DRAIN_SECONDS + 5) -> None:
        """Wait for the pumps still reading or recording, and for their caps."""
        pending = [task for task in self._pending if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def aclose(self) -> None:
        await self.settle()
        for task in list(self._pending):
            task.cancel()
        await self.client.aclose()


def create_app(gateway: Gateway) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await gateway.aclose()

    app = FastAPI(
        title="Grove model gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_api_route("/v1/chat/completions", gateway.chat_completions, methods=["POST"])
    app.add_api_route("/v1/models", gateway.models, methods=["GET"])

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


def serve() -> None:
    """Run the gateway on GROVE_GATEWAY_HOST and GROVE_GATEWAY_PORT (127.0.0.1:18300)."""
    import uvicorn

    from grove.store.connection import open_driver

    settings = load_model_settings()
    driver = open_driver()
    try:
        app = create_app(Gateway(driver, settings=settings))
        uvicorn.run(app, host=settings.gateway_host, port=settings.gateway_port, access_log=False)
    finally:
        driver.close()


if __name__ == "__main__":
    serve()
