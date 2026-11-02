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
    MAX_OUTPUT_TOKENS,
    MIN_OUTPUT_TOKENS,
    Catalog,
    ModelSettings,
    ModelSpec,
    load_catalog,
    load_model_settings,
)
from grove.models.egress import MODEL_LAYER, AsyncAllowlistTransport, EgressPolicy
from grove.models.errors import (
    BodyTooLarge,
    ModelError,
    ModelRefused,
    ProviderError,
    Unauthenticated,
)
from grove.models.gateway_auth import RunClaims, verify
from grove.models.http import (
    MAX_ATTEMPTS,
    MODAL_ATTEMPTS,
    NOT_SENT,
    asend,
    no_cookies,
    reset_after,
)
from grove.models.layer import default_cache_key, glimmer_effort_fields
from grove.models.meter import Meter
from grove.models.shape import (
    Usage,
    chat_answer,
    chat_usage,
    check_response_format,
    clean_messages,
    clean_tools,
    estimate_usage,
    may_have_run,
    redact,
    usage_or_estimate,
)
from grove.store.connection import Driver
from grove.writer.model_calls import ModelCall

_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
# Fields Meta refuses with HTTP 400 on Chat Completions (probes of 2026-11-02), or that would
# let a caller pick its own effort outside the model's list.
_META_REFUSED = ("stop", "logprobs", "top_logprobs")
_EFFORT_FIELDS = ("reasoning_effort", "chat_template_kwargs")
_GLIMMER_DROPPED = ("prompt_cache_key", "temperature", "top_p", "stop")
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
# After the client leaves, how long the pump reads on for the usage chunk.
DRAIN_SECONDS = 120.0
# A full Spark context is about 4 MB of text; images as data URLs take the rest. The box has 8 GB.
MAX_BODY_BYTES = 32 * 1024 * 1024


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


async def _read_json(request: Request) -> Any:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise BodyTooLarge(f"a body may hold at most {MAX_BODY_BYTES} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise BodyTooLarge(f"a body may hold at most {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise ModelRefused("the body is not JSON") from None


def upstream_body(
    body: dict, *, spec: ModelSpec, effort: str | None, job: str, world_id: str = ""
) -> dict:
    out = {
        key: value
        for key, value in body.items()
        if key in _KNOWN_FIELDS and key not in _EFFORT_FIELDS
    }
    out["messages"] = clean_messages(out.get("messages"))
    if "tools" in out:
        out["tools"] = clean_tools(out["tools"])
    check_response_format(out.get("response_format"))
    if not isinstance(out.get("stream", False), bool):
        # A provider could read the text "false" as false, and the meter would look for a stream.
        raise ModelRefused("stream must be true or false")
    if not isinstance(out.get("stream_options") or {}, dict):
        raise ModelRefused("stream_options must be an object")
    n = out.pop("n", 1)
    if n != 1:
        raise ModelRefused("n must be 1")
    wanted = [out[field] for field in _OUTPUT_FIELDS if isinstance(out.get(field), int)]
    limit = max(max(wanted) if wanted else DEFAULT_OUTPUT_TOKENS, MIN_OUTPUT_TOKENS)
    limit = min(limit, MAX_OUTPUT_TOKENS)
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
            key = default_cache_key(job, prefix if isinstance(prefix, str) else None, world_id)
            if key:
                out["prompt_cache_key"] = key
    else:
        # D15: Glimmer runs on its own sampling, never greedy, and never stops on `<|eom|>`. The
        # server applies the model's generation config.
        for field in _GLIMMER_DROPPED:
            out.pop(field, None)
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
        except (ValueError, RecursionError):
            return
        if not isinstance(event, dict):
            return
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        choices = event.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finished = True
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            self._count(delta.get("content"), output=True)
            self._count(delta.get("reasoning"), output=False)
            calls = delta.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                self.output = True
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    self._count(function.get("arguments"), output=True)

    def _count(self, text: object, *, output: bool) -> None:
        if isinstance(text, str) and text:
            self.chars += len(text)
            self.output = self.output or output


def _empty_completion(doc: dict) -> bool:
    text, calls, _ = chat_answer(doc)
    return not text.strip() and not calls


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
            cookies=no_cookies(),
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
                usage_estimated=usage.estimated,
            ),
        )

    def _admit(self, claims: RunClaims) -> None:
        # Every agent call stops at the ceiling (build plan section 4), whatever job its token
        # names. The jobs that continue past it are the ones code runs through the layer.
        job = self.catalog.job(claims.job)
        self.meter.gate(claims.world_id, job, every_job=True)
        if claims.token_cap is not None:
            self.meter.check_run_cap(claims.world_id, claims.run_id, claims.token_cap)

    async def chat_completions(self, request: Request) -> Response:
        try:
            claims = verify(self.secret, self.catalog, _bearer(request))
            body = await _read_json(request)
            if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                raise ModelRefused("the body must be an object with a model")
            spec = self.catalog.model(body["model"])
            if not spec.chat:
                raise ModelRefused("this model does not take chat")
            job = self.catalog.job(claims.job)
            effort = _effort(job, spec, self.catalog)
            url, headers = self._route(spec)
            try:
                out = upstream_body(
                    body, spec=spec, effort=effort, job=claims.job, world_id=claims.world_id
                )
                messages, tools = json.dumps(out["messages"]), json.dumps(out.get("tools") or [])
                # A trial build: NaN or a lone surrogate cannot be sent, and it is said here.
                self.client.build_request("POST", url, json=out)
            except (ValueError, TypeError, RecursionError):
                raise ModelRefused("the body cannot be sent as JSON") from None
            try:
                await run_in_threadpool(self._admit, claims)
            except ModelError:
                raise
            except Exception:
                # The store cannot answer: the gate fails closed, with a typed error.
                raise ProviderError("the budget could not be read") from None
        except ModelError as error:
            return _error(error)
        stream = bool(out.get("stream"))
        input_chars = len(messages) + len(tools)
        started = clock.monotonic()
        attempt = [started]

        def timed() -> httpx.Request:
            attempt[0] = clock.monotonic()
            # Identity, so a stream is relayed as it comes, with no compression buffer.
            return self.client.build_request(
                "POST", url, json=out, headers={**headers, "Accept-Encoding": "identity"}
            )

        record = self._record
        try:
            response = await asend(
                self.client,
                timed,
                stream=stream,
                sleep=self._sleep,
                attempts=MODAL_ATTEMPTS if spec.route == "modal" else MAX_ATTEMPTS,
            )
        except ModelError as error:
            # An egress refusal: nothing left, so nothing is recorded.
            return _error(error)
        except httpx.RequestError as exc:
            # No answer, or an answer whose body could not be read (a DecodingError). Past a
            # failed connect, the provider may have run the call, so the row carries an estimate.
            usage = Usage() if isinstance(exc, NOT_SENT) else estimate_usage(input_chars)
            await run_in_threadpool(
                record, claims, spec, effort, usage, "provider_error", started, attempt[0]
            )
            return _error(ProviderError("the model provider did not answer"))
        if response.status_code >= 300:
            try:
                await response.aread()
            except Exception:  # The status decides from here; an unreadable body changes nothing.
                pass
            finally:
                await response.aclose()
            outcome = "rate_limited" if response.status_code == 429 else "provider_error"
            ran = may_have_run(response.status_code)
            usage = estimate_usage(input_chars) if ran else Usage()
            await run_in_threadpool(
                record, claims, spec, effort, usage, outcome, started, attempt[0]
            )
            if response.status_code < 400:
                # Redirects are not followed: the provider must answer at the URL it was given.
                return _error(ProviderError("the model provider answered with a redirect"))
            return self._upstream_error(response)
        if not stream:
            try:
                doc = response.json()
            except (ValueError, RecursionError):
                doc = None
            if not isinstance(doc, dict):
                usage = estimate_usage(input_chars)
                await run_in_threadpool(
                    record, claims, spec, effort, usage, "provider_error", started, attempt[0]
                )
                return _error(ProviderError("the model provider's answer is not a JSON object"))
            text, _, _ = chat_answer(doc)
            usage = usage_or_estimate(chat_usage(doc.get("usage")), input_chars, len(text))
            # The answer passes on as it came; an empty one is recorded as such (D15).
            outcome = "invalid_output" if _empty_completion(doc) else "ok"
            await run_in_threadpool(
                record, claims, spec, effort, usage, outcome, started, attempt[0]
            )
            # The provider's bytes: its answer may hold what JSONResponse refuses to encode.
            return Response(response.content, media_type="application/json")
        return StreamingResponse(
            self._relay(response, claims, spec, effort, started, attempt[0], input_chars),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )

    def _redact(self, text: str) -> str:
        return redact(text, (self.settings.meta_key, self.settings.glimmer_key))

    def _upstream_error(self, response: httpx.Response) -> JSONResponse:
        """The provider's status with its message, in the B.3 shape; nothing else of its body."""
        try:
            doc = response.json()
            message = self._redact(str(doc["error"]["message"]))
        except (ValueError, KeyError, TypeError, RecursionError, httpx.StreamError):
            # httpx.StreamError: the body could not be read, so only the status is known.
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
        wait = reset_after(response)
        if wait is not None:
            headers["retry-after"] = str(math.ceil(wait))
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
            # Decoded bytes: the client gets no Content-Encoding header, and the meter reads text.
            async for chunk in response.aiter_bytes():
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
        except Exception:
            # Whatever broke the read, the call is metered below with what came.
            outcome = "client_closed" if gone.is_set() else "provider_error"
        finally:
            # The cap's cancel lands here too.
            if outcome is None:
                outcome = "client_closed" if gone.is_set() else "provider_error"
            try:
                usage = usage_or_estimate(chat_usage(watch.usage), input_chars, watch.chars)
                # In a thread: a slow store must not freeze every other stream of the loop.
                await asyncio.to_thread(
                    self._record, claims, spec, effort, usage, outcome, started, gpu_from
                )
            finally:
                # Whatever happened, the client's stream ends.
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

    async def settle(self, timeout: float | None = None) -> None:
        """Wait for the pumps still reading or recording, and for their caps."""
        pending = [task for task in self._pending if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=DRAIN_SECONDS + 5 if timeout is None else timeout)

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
