"""The model-call layer (A.1, D15). Every direct model call of Grove goes through ModelLayer.

Each call: the job table gives the model and the effort, a contributor id is refused, the budget
gate runs, the request leaves through the egress allowlist with back-off, the answer is checked,
and the call is written to `model_calls` with its tokens and its cost, a failed call included.

Protocols, from the live probes of 2026-11-02 (the lane's evidence): Spark goes through the
Responses API at https://api.meta.ai/v1, which takes every effort from `minimal` to `max`,
`prompt_cache_key` (the second call of a 7,043-token prefix read 7,025 tokens from the cache) and
a JSON schema under `text.format`. The Anthropic-style Messages path at https://api.meta.ai refused
`prompt_cache_key` and the effort `minimal` with HTTP 400, so this layer does not use it. Glimmer
and the gateway use Chat Completions, which an OpenAI-compatible server speaks.
"""

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from grove import clock
from grove.models import audio
from grove.models.config import (
    DEFAULT_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MIN_OUTPUT_TOKENS,
    Catalog,
    Job,
    ModelSettings,
    ModelSpec,
    load_catalog,
    load_model_settings,
    valid_run_id,
)
from grove.models.egress import MODEL_LAYER, AllowlistTransport, EgressPolicy
from grove.models.errors import (
    InvalidOutput,
    ModelRefused,
    ProviderError,
    RateLimited,
)
from grove.models.http import MAX_ATTEMPTS, MODAL_ATTEMPTS, NOT_SENT, reset_after, send
from grove.models.meter import Meter
from grove.models.shape import (
    REF_KEYS,
    Usage,
    chat_answer,
    chat_usage,
    check_responses_input,
    clean_messages,
    clean_tools,
    estimate_usage,
    may_have_run,
    redact,
    responses_text,
    responses_usage,
    usage_or_estimate,
)
from grove.store.connection import Driver
from grove.writer.model_calls import BudgetState, ModelCall

MAX_SCHEMA_DEPTH = 10
MAX_SCHEMA_PROPERTIES = 5000
BACKGROUND_DEADLINE = 900.0
POLL_MISSES = 4
_TERMINAL = frozenset({"completed", "failed", "cancelled", "incomplete"})
_PROVIDER_MESSAGE = 300
_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


@dataclass(frozen=True)
class Result:
    text: str
    data: Any
    model: str
    effort: str | None
    usage: Usage
    cost: float
    model_call_id: str
    budget: BudgetState
    finish: str | None = None
    tool_calls: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Transcript:
    text: str
    audio_seconds: float
    cost: float
    model_call_ids: tuple[str, ...]


def check_schema(schema: dict) -> Draft202012Validator:
    """Meta takes a flat schema only: no references, at most 10 levels, at most 5,000 properties."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ModelRefused("a structured-output schema must be an object schema")
    count = 0

    def walk(node: object, depth: int) -> None:
        nonlocal count
        if isinstance(node, dict):
            # A reference is refused whole: Meta returns 400 for it, and the validator below
            # would open its own socket to fetch one that names a URL.
            if any(key in node for key in (*REF_KEYS, "$defs", "definitions")):
                raise ModelRefused("a schema with references is refused: Meta returns 400")
            properties = node.get("properties")
            if isinstance(properties, dict):
                count += len(properties)
            # Meta's limit counts nested schemas, not the JSON wrappers between them.
            if isinstance(properties, dict) or "items" in node:
                depth += 1
            if depth > MAX_SCHEMA_DEPTH:
                raise ModelRefused(f"a schema may nest at most {MAX_SCHEMA_DEPTH} levels")
            for value in node.values():
                walk(value, depth)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth)

    walk(schema, 0)
    if count > MAX_SCHEMA_PROPERTIES:
        raise ModelRefused(f"a schema may hold at most {MAX_SCHEMA_PROPERTIES} properties")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ModelRefused("the schema is not a valid JSON schema") from exc
    return Draft202012Validator(schema)


def parse_structured(text: str, validator: Draft202012Validator) -> Any:
    try:
        data = json.loads(text)
        errors = sorted(validator.iter_errors(data), key=lambda error: list(map(str, error.path)))
    except (ValueError, RecursionError) as exc:
        raise InvalidOutput("the answer is not JSON") from exc
    if errors:
        raise InvalidOutput(
            "the answer does not fit the schema",
            details={"paths": ["/".join(map(str, error.path)) for error in errors[:10]]},
        )
    return data


def _structured(
    text: str, validator: Draft202012Validator | None
) -> tuple[Any, InvalidOutput | None]:
    """The parsed answer, or the InvalidOutput that a non-JSON answer or a schema miss gives."""
    if validator is None:
        return None, None
    try:
        return parse_structured(text, validator), None
    except InvalidOutput as misfit:
        return None, misfit


def _audio_seconds(stated: object, sent: float) -> float:
    """The provider's duration in milliseconds as seconds, when it can be true of the audio sent.
    An infinite or absurd value would lose the row or close the day's budget."""
    known = isinstance(stated, int | float) and not isinstance(stated, bool)
    if known and math.isfinite(stated) and 0 < stated <= sent * 2000:
        return stated / 1000
    return sent


def default_cache_key(job: str, prefix: str | None, world_id: str = "") -> str | None:
    """One key per shared prefix (D15): the job and a digest of the stable instructions. The world
    goes into the digest, so a call cannot learn from `cached_tokens` what another world sent."""
    if not prefix:
        return None
    digest = hashlib.sha256(f"{world_id}\0{prefix}".encode()).hexdigest()[:24]
    return f"grove:{job}:{digest}"


def glimmer_effort_fields(effort: str | None) -> dict[str, Any]:
    """Glimmer's effort goes through the chat template on the serving engine (D15). Its template
    reads `reasoning_strength` (low, medium, high; the default is high). vLLM drops a top-level
    `reasoning_effort` and a `reasoning_effort` template field without an error, so the model
    would run at high."""
    return {"chat_template_kwargs": {"reasoning_strength": effort}} if effort else {}


def _provider_message(response: httpx.Response, secrets: tuple[str | None, ...]) -> str:
    """The provider's error message, cut short, with the keys this layer holds taken out."""
    try:
        doc = response.json()
    except ValueError:
        return ""
    error = doc.get("error") if isinstance(doc, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return ""
    return redact(message, secrets)[:_PROVIDER_MESSAGE]


class ModelLayer:
    def __init__(
        self,
        driver: Driver,
        *,
        settings: ModelSettings | None = None,
        catalog: Catalog | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or load_model_settings()
        self.catalog = catalog or load_catalog()
        self.meter = Meter(driver, self.catalog, self.settings)
        self.policy = EgressPolicy.for_settings(self.settings)
        self._sleep = sleep
        self._http = httpx.Client(
            transport=AllowlistTransport(self.policy, MODEL_LAYER, inner=transport),
            timeout=_TIMEOUT,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ModelLayer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # The steps every call shares.

    def _prepare(
        self,
        world_id: str,
        job_name: str,
        effort: str | None,
        run_id: str | None,
        family: str | None = None,
    ) -> tuple[Job, ModelSpec, str | None]:
        if run_id is not None and not valid_run_id(run_id):
            # Checked here: the writer would refuse it only after the call had run and billed.
            raise ModelRefused("run_id must be a short ASCII id")
        job = self.catalog.job(job_name)
        spec = self.catalog.model_for(job)
        if family is not None and spec.family != family:
            raise ModelRefused("this call does not serve that job's model")
        chosen = self.catalog.effort_for(job, spec, effort)
        self.meter.gate(world_id, job)
        return job, spec, chosen

    def _record(
        self,
        world_id: str,
        *,
        job: Job,
        spec: ModelSpec,
        effort: str | None,
        run_id: str | None,
        usage: Usage,
        outcome: str,
        started: float,
        audio_seconds: float | None = None,
        gpu_from: float | None = None,
    ) -> tuple[str, BudgetState, float]:
        """`started` is the first try; `gpu_from` the start of the attempt that answered. The GPU
        bills from the second: the 503s of a cold start ran on no GPU of ours."""
        now = clock.monotonic()
        seconds = max(now - started, 0.0)
        gpu_seconds = max(now - (started if gpu_from is None else gpu_from), 0.0)
        cost = self.catalog.cost(
            spec.id,
            input_tokens=usage.input_tokens,
            cached_tokens=usage.cached_tokens,
            output_tokens=usage.output_tokens,
            audio_seconds=audio_seconds or 0.0,
            gpu_seconds=gpu_seconds if spec.route == "modal" else 0.0,
        )
        call = ModelCall(
            job=job.name,
            model=spec.id,
            route=spec.route,
            outcome=outcome,
            at=self.meter.now(world_id),
            effort=effort,
            run_id=run_id,
            input_tokens=usage.input_tokens,
            cached_tokens=min(usage.cached_tokens, usage.input_tokens),
            output_tokens=usage.output_tokens,
            reasoning_tokens=min(usage.reasoning_tokens, usage.output_tokens),
            cost=cost,
            audio_seconds=audio_seconds,
            latency_ms=int(seconds * 1000),
        )
        call_id, budget = self.meter.record(world_id, call)
        return call_id, budget, cost

    def _failed(
        self,
        world_id: str,
        response: httpx.Response,
        *,
        job: Job,
        spec: ModelSpec,
        effort: str | None,
        run_id: str | None,
        started: float,
        gpu_from: float | None = None,
        usage: Usage | None = None,
        audio_seconds: float | None = None,
    ) -> None:
        """Record a refused call, then raise its typed error."""
        if response.status_code == 429:
            self._record(
                world_id,
                job=job,
                spec=spec,
                effort=effort,
                run_id=run_id,
                usage=Usage(),
                outcome="rate_limited",
                started=started,
                gpu_from=gpu_from,
            )
            raise RateLimited(
                "the provider's rate limit held after the retries",
                details={"reset_after_seconds": reset_after(response)},
            )
        self._record(
            world_id,
            job=job,
            spec=spec,
            effort=effort,
            run_id=run_id,
            usage=usage or Usage(),
            outcome="provider_error",
            started=started,
            gpu_from=gpu_from,
            audio_seconds=audio_seconds,
        )
        error = ProviderError(
            "the provider refused the call",
            details={
                "status": response.status_code,
                "provider_message": _provider_message(
                    response, (self.settings.meta_key, self.settings.glimmer_key)
                ),
            },
        )
        error.retryable = response.status_code >= 500
        raise error

    def _send(
        self,
        world_id: str,
        build: Callable[[], httpx.Request],
        *,
        job: Job,
        spec: ModelSpec,
        effort: str | None,
        run_id: str | None,
        started: float,
        request_chars: int = 0,
        audio_seconds: float | None = None,
    ) -> tuple[dict, float]:
        """Send with back-off, and return the JSON answer with the start of the attempt that
        gave it. A failure is recorded and raised typed: no answer, an error status, or an answer
        that is not a JSON object. Its row has no tokens when the provider did no work, and an
        estimate of the input when it may have run the call."""
        attempts = MODAL_ATTEMPTS if spec.route == "modal" else MAX_ATTEMPTS
        attempt = [started]

        def timed() -> httpx.Request:
            attempt[0] = clock.monotonic()
            return build()

        try:
            response = send(self._http, timed, sleep=self._sleep, attempts=attempts)
        except httpx.RequestError as exc:
            # No answer, or an answer whose body could not be read (a DecodingError).
            not_sent = isinstance(exc, NOT_SENT)
            self._record(
                world_id,
                job=job,
                spec=spec,
                effort=effort,
                run_id=run_id,
                usage=Usage() if not_sent else estimate_usage(request_chars),
                outcome="provider_error",
                started=started,
                gpu_from=attempt[0],
                audio_seconds=None if not_sent else audio_seconds,
            )
            # From None: the cause's text can quote a request header, and a header holds a key.
            raise ProviderError("the model provider did not answer") from None
        if response.status_code >= 300:
            # Redirects are not followed, so a 3xx is a failure too.
            ran = may_have_run(response.status_code)
            self._failed(
                world_id,
                response,
                job=job,
                spec=spec,
                effort=effort,
                run_id=run_id,
                started=started,
                gpu_from=attempt[0],
                usage=estimate_usage(request_chars) if ran else Usage(),
                audio_seconds=audio_seconds if ran else None,
            )
        try:
            doc = response.json()
        except (ValueError, RecursionError):
            doc = None
        if not isinstance(doc, dict):
            self._record(
                world_id,
                job=job,
                spec=spec,
                effort=effort,
                run_id=run_id,
                usage=estimate_usage(request_chars),
                outcome="provider_error",
                started=started,
                gpu_from=attempt[0],
                audio_seconds=audio_seconds,
            )
            raise ProviderError("the model provider's answer is not a JSON object")
        return doc, attempt[0]

    def _meta_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.require_meta_key()}"}

    # Spark through the Responses API.

    def respond(
        self,
        world_id: str,
        *,
        job: str,
        input: str | list[dict],
        instructions: str | None = None,
        schema: dict | None = None,
        schema_name: str = "output",
        effort: str | None = None,
        run_id: str | None = None,
        cache_key: str | None = None,
        max_output_tokens: int = DEFAULT_OUTPUT_TOKENS,
        background: bool = False,
    ) -> Result:
        """One Spark answer. `instructions` is the stable prefix, `input` the event (D15)."""
        if not MIN_OUTPUT_TOKENS <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ModelRefused(
                f"max_output_tokens must be from {MIN_OUTPUT_TOKENS} to {MAX_OUTPUT_TOKENS}"
            )
        check_responses_input(input)
        validator = check_schema(schema) if schema is not None else None
        job_row, spec, chosen = self._prepare(world_id, job, effort, run_id, family="spark")
        body: dict[str, Any] = {
            "model": spec.id,
            "input": input,
            "reasoning": {"effort": chosen},
            "max_output_tokens": max_output_tokens,
            # Background mode requires stored responses; a direct call stores nothing.
            "store": background,
        }
        if instructions:
            body["instructions"] = instructions
        key = cache_key or default_cache_key(job, instructions, world_id)
        if key:
            body["prompt_cache_key"] = key
        if background:
            body["background"] = True
        if validator is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            }
        base = self.settings.meta_base_url
        headers = self._meta_headers()
        started = clock.monotonic()
        request_chars = len(json.dumps(body))
        doc, _ = self._send(
            world_id,
            lambda: self._http.build_request(
                "POST", f"{base}/responses", json=body, headers=headers
            ),
            job=job_row,
            spec=spec,
            effort=chosen,
            run_id=run_id,
            started=started,
            request_chars=request_chars,
        )
        if background:
            try:
                doc = self._wait(doc, base, headers)
            except (ProviderError, httpx.RequestError) as exc:
                self._record(
                    world_id,
                    job=job_row,
                    spec=spec,
                    effort=chosen,
                    run_id=run_id,
                    # It was accepted and may have run before the poll failed.
                    usage=estimate_usage(request_chars),
                    outcome="provider_error",
                    started=started,
                )
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError("polling a background response failed") from None
        text = responses_text(doc)
        usage = usage_or_estimate(responses_usage(doc), request_chars, len(text))
        status = doc.get("status")
        ok = status == "completed" and bool(text.strip())
        # The answer is checked before the call is written, so the row says what it was.
        data, misfit = _structured(text, validator) if ok else (None, None)
        call_id, budget, cost = self._record(
            world_id,
            job=job_row,
            spec=spec,
            effort=chosen,
            run_id=run_id,
            usage=usage,
            outcome="ok" if ok and misfit is None else "invalid_output",
            started=started,
        )
        if not ok:
            raise InvalidOutput(
                "the model gave no complete answer",
                details={
                    "status": status,
                    "incomplete": doc.get("incomplete_details"),
                    "model_call_id": call_id,
                },
            )
        if misfit is not None:
            misfit.details["model_call_id"] = call_id
            raise misfit
        return Result(
            text=text,
            data=data,
            model=spec.id,
            effort=chosen,
            usage=usage,
            cost=cost,
            model_call_id=call_id,
            budget=budget,
            finish=status,
        )

    def _wait(self, doc: dict, base: str, headers: dict[str, str]) -> dict:
        response_id = doc.get("id")
        if not isinstance(response_id, str):
            raise ProviderError("a background response came back with no id")
        deadline = clock.monotonic() + BACKGROUND_DEADLINE
        pause = 1.0
        misses = 0
        try:
            while doc.get("status") not in _TERMINAL:
                if clock.monotonic() > deadline:
                    raise ProviderError("a background response did not finish in time")
                self._sleep(pause)
                pause = min(pause * 2, 5.0)
                polled = self._poll(base, response_id, headers)
                if polled is not None:
                    doc, misses = polled, 0
                    continue
                # A GET is safe to repeat, so one passing failure does not end a paid run.
                misses += 1
                if misses >= POLL_MISSES:
                    raise ProviderError("polling a background response failed")
        except (ProviderError, httpx.RequestError):
            # A response left running bills at Meta with no row to show it: stop it.
            self._cancel(base, response_id, headers)
            raise
        return doc

    def _poll(self, base: str, response_id: str, headers: dict[str, str]) -> dict | None:
        """The polled document, or None for a failure that may pass. A 4xx will not pass."""
        try:
            polled = send(
                self._http,
                lambda: self._http.build_request(
                    "GET", f"{base}/responses/{response_id}", headers=headers
                ),
                sleep=self._sleep,
            )
        except httpx.RequestError:
            return None
        if 300 <= polled.status_code < 500 and polled.status_code != 429:
            raise ProviderError(
                "polling a background response failed", details={"status": polled.status_code}
            )
        if polled.status_code >= 300:
            return None
        try:
            doc = polled.json()
        except (ValueError, RecursionError):
            return None
        return doc if isinstance(doc, dict) else None

    def _cancel(self, base: str, response_id: str, headers: dict[str, str]) -> None:
        try:
            self._http.post(f"{base}/responses/{response_id}/cancel", headers=headers)
        except httpx.RequestError:
            pass

    # Chat Completions: Glimmer on Modal, or Spark on Meta.

    def chat(
        self,
        world_id: str,
        *,
        job: str,
        messages: list[dict],
        schema: dict | None = None,
        schema_name: str = "output",
        effort: str | None = None,
        run_id: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = DEFAULT_OUTPUT_TOKENS,
        cache_key: str | None = None,
    ) -> Result:
        if not MIN_OUTPUT_TOKENS <= max_tokens <= MAX_OUTPUT_TOKENS:
            raise ModelRefused(
                f"max_tokens must be from {MIN_OUTPUT_TOKENS} to {MAX_OUTPUT_TOKENS}"
            )
        messages = clean_messages(messages)
        validator = check_schema(schema) if schema is not None else None
        job_row, spec, chosen = self._prepare(world_id, job, effort, run_id)
        if not spec.chat:
            raise ModelRefused("this job's model does not take chat")
        while True:
            try:
                return self._chat_once(
                    world_id,
                    job_row,
                    spec,
                    chosen,
                    messages,
                    validator,
                    schema,
                    schema_name,
                    run_id,
                    tools,
                    max_tokens,
                    cache_key,
                )
            except InvalidOutput as error:
                lower = self._lower_effort(spec, chosen)
                # An empty answer at the reasoning ceiling is retried at a lower effort (D15).
                if not error.details.get("empty") or lower is None:
                    raise
                # A retry is a new call: it passes the budget gate again.
                self.meter.gate(world_id, job_row)
                chosen = lower

    @staticmethod
    def _lower_effort(spec: ModelSpec, effort: str | None) -> str | None:
        if effort is None or effort not in spec.efforts:
            return None
        index = spec.efforts.index(effort)
        return spec.efforts[index - 1] if index > 0 else None

    def _chat_once(
        self,
        world_id: str,
        job: Job,
        spec: ModelSpec,
        effort: str | None,
        messages: list[dict],
        validator: Draft202012Validator | None,
        schema: dict | None,
        schema_name: str,
        run_id: str | None,
        tools: list[dict] | None,
        max_tokens: int,
        cache_key: str | None,
    ) -> Result:
        body: dict[str, Any] = {"model": spec.id, "messages": messages, "max_tokens": max_tokens}
        if spec.route == "meta":
            base, headers = self.settings.meta_base_url, self._meta_headers()
            body["reasoning_effort"] = effort
            head = messages[0] if messages else {}
            prefix = head.get("content") if head.get("role") == "system" else None
            prefix = prefix if isinstance(prefix, str) else None
            key = cache_key or default_cache_key(job.name, prefix, world_id)
            if key:
                body["prompt_cache_key"] = key
        else:
            base, token = self.settings.require_glimmer()
            headers = {"Authorization": f"Bearer {token}"}
            body.update(glimmer_effort_fields(effort))
        if tools:
            body["tools"] = clean_tools(tools)
            body["tool_choice"] = "auto"
        if validator is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        started = clock.monotonic()
        request_chars = len(json.dumps(body))
        doc, attempt = self._send(
            world_id,
            lambda: self._http.build_request(
                "POST", f"{base}/chat/completions", json=body, headers=headers
            ),
            job=job,
            spec=spec,
            effort=effort,
            run_id=run_id,
            started=started,
            request_chars=request_chars,
        )
        text, tool_calls, finish = chat_answer(doc)
        usage = usage_or_estimate(chat_usage(doc.get("usage")), request_chars, len(text))
        empty = not text.strip() and not tool_calls
        data, misfit = (None, None) if empty or not text else _structured(text, validator)
        call_id, budget, cost = self._record(
            world_id,
            job=job,
            spec=spec,
            effort=effort,
            run_id=run_id,
            usage=usage,
            outcome="invalid_output" if empty or misfit is not None else "ok",
            started=started,
            gpu_from=attempt,
        )
        if empty:
            raise InvalidOutput(
                "the model gave an empty answer",
                details={"empty": True, "finish": finish, "model_call_id": call_id},
            )
        if misfit is not None:
            misfit.details["model_call_id"] = call_id
            raise misfit
        return Result(
            text=text,
            data=data,
            model=spec.id,
            effort=effort,
            usage=usage,
            cost=cost,
            model_call_id=call_id,
            budget=budget,
            finish=finish,
            tool_calls=tool_calls,
        )

    # Voice Transcribe.

    def transcribe(
        self,
        world_id: str,
        *,
        audio_wav: bytes,
        job: str = "transcription",
        run_id: str | None = None,
    ) -> Transcript:
        """Text of a WAV file. Audio over the API's 10 minutes goes as pieces, one call each."""
        job_row, spec, _ = self._prepare(world_id, job, None, run_id, family="voice")
        pieces = audio.split_wav(audio_wav)
        base, headers = self.settings.meta_base_url, self._meta_headers()
        request = json.dumps({"model": spec.id, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"})
        texts, ids, total_cost, total_seconds = [], [], 0.0, 0.0
        for index, piece in enumerate(pieces):
            if index:
                # Each piece passes the gate: a long file must not run past a reached budget.
                self.meter.gate(world_id, job_row)
            started = clock.monotonic()
            doc, _ = self._send(
                world_id,
                lambda piece=piece: self._http.build_request(
                    "POST",
                    f"{base}/asr/transcribe",
                    headers=headers,
                    files={
                        "request": (None, request, "application/json"),
                        "audio": ("audio.wav", piece.data, "audio/wav"),
                    },
                ),
                job=job_row,
                spec=spec,
                effort=None,
                run_id=run_id,
                started=started,
                audio_seconds=piece.seconds,
            )
            stated = doc.get("audioDurationMs")
            seconds = _audio_seconds(stated, piece.seconds)
            text = doc.get("transcript")
            ok = isinstance(text, str)
            call_id, _, cost = self._record(
                world_id,
                job=job_row,
                spec=spec,
                effort=None,
                run_id=run_id,
                usage=Usage(),
                outcome="ok" if ok else "invalid_output",
                started=started,
                audio_seconds=seconds,
            )
            if not ok:
                raise InvalidOutput("the transcript is missing", details={"model_call_id": call_id})
            texts.append(text.strip())
            ids.append(call_id)
            total_cost += cost
            total_seconds += seconds
        return Transcript(
            text=" ".join(part for part in texts if part),
            audio_seconds=total_seconds,
            cost=total_cost,
            model_call_ids=tuple(ids),
        )
