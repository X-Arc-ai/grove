# The model-call layer

Every model call of Grove goes through `server/grove/models/`. The layer picks the model and the effort from the job, refuses a contributor model id, applies the world's daily budget, sends the request through the egress allowlist with back-off, checks the answer, and writes the call to `model_calls` with its tokens and its cost. A failed call is written too. Agents reach a model only through the internal gateway, which does the same for each of their calls.

## Who calls what

| Caller | Entry | Protocol | Models |
|---|---|---|---|
| Code that needs one answer (extraction, verification, judgments, explanations) | `ModelLayer.respond` | Responses API, `POST https://api.meta.ai/v1/responses` | Spark 1.3 |
| Code that needs a chat turn, Glimmer's jobs above all | `ModelLayer.chat` | Chat Completions | Glimmer on Modal, or Spark 1.3 |
| Voice notes | `ModelLayer.transcribe` | `POST https://api.meta.ai/v1/asr/transcribe` (multipart) | Voice Transcribe 1.0 |
| OpenCode's four agents | the gateway, `POST http://127.0.0.1:18300/v1/chat/completions` | Chat Completions with tools and streaming | Spark 1.3, Glimmer |

Model ids are configuration (`server/grove/models/jobs.json`), never code. Prices are in `server/grove/models/prices.json` (S24).

## Facts the protocols rest on

Probed live on 2026-11-02 with synthetic text.

| Path | What holds |
|---|---|
| Responses API | `reasoning.effort` takes `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. `prompt_cache_key` works: the second call of a 7,043-token prefix read 7,025 tokens from the cache. `text.format` with a JSON schema returns JSON that fits it. `background: true` needs `store` to be true, and the answer is polled at `GET /v1/responses/{id}`. Usage: `input_tokens` (cached ones included), `input_tokens_details.cached_tokens`, `output_tokens` (reasoning included), `output_tokens_details.reasoning_tokens`. |
| Chat Completions | Same efforts as `reasoning_effort`, the same cache key, and `response_format` with a strict JSON schema. Refused with HTTP 400: `stop`, `n` other than 1, `logprobs`, and every `tool_choice` except `"auto"`. A stream carries the usage on its finish chunk when `stream_options.include_usage` is true. Tool parameters with a `$schema` key and an assistant turn whose content is `""` beside its tool calls are accepted. |
| The Messages path at `https://api.meta.ai` | Refuses `prompt_cache_key` and the effort `minimal` with HTTP 400. The layer does not use it. |
| Cache keys | A key turns the cache on and does not partition it. Meta matches on content: one prefix under a second key read 6,513 of 6,522 tokens from the cache, and the same prefix with no key read none. So `cached_tokens` can tell a caller that the same text was sent recently under this API key, if that caller already holds the exact text of a long prefix. All worlds also share vLLM's prefix cache on the Modal server. Accepted for this build. |
| Background cancel | `POST /v1/responses/{id}/cancel` answers 200, and the state still read `in_progress` two seconds later. The layer does not wait for it. |
| Not set | `prompt_cache_retention` (a hint, per D15), and streaming on a background call: the layer polls instead. |
| Voice Transcribe | The request part `{"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}` and a WAV part. The answer has `transcript` and `audioDurationMs`. Files over 10 minutes are split into pieces of at most 590 seconds, one call each. The fields `keywords` and `languageBias` in the form `["..."]` and `"en"` were refused as a malformed request. |
| Reasoning tokens | Every Muse language model reasons, and the tokens bill as output. A one-word answer used up to 340 of them, so no call asks for fewer than 1,024 output tokens. No call asks for more than 65,536, about 0.28 USD of output on Spark. |

## Jobs and the budget

`jobs.json` gives each job its model family, its effort and what it does at the ceiling. Every call carries an effort: the caller's, when the model takes it, or the job's.

| Job | Model, effort | At the ceiling |
|---|---|---|
| `triage` | Glimmer, low | continues |
| `triage_fallback` | Spark, minimal | continues |
| `extraction` | Spark, minimal | continues |
| `transcription` | Voice Transcribe | continues |
| `verification` | Spark, low | waits |
| `second_opinion` | Glimmer, medium | waits |
| `consolidation` | Spark, high | waits |
| `explain_break` | Spark, low | waits |
| `update_pass` (the Learner's gateway calls) | Spark, low | waits |
| `cleanup_pass` (the Curator's gateway calls) | Spark, low | waits |
| `consolidation_pass` (the Connector's gateway calls) | Spark, high | waits |
| `smoke` | Spark, minimal | waits |

The backfill's calls use `backfill_triage` and `backfill_extraction`, which wait at the ceiling, because the build plan's backfill stops there.

Each world has one `budgets` row per day (UTC, from the clock provider). `record_model_call` writes the call and adds its cost to `spent` in one transaction; the call that brings `spent` to the ceiling turns the state to `reached`. From then on, a job that waits is refused with `budget_reached` (HTTP 429, retryable, with `resets_at`, the next midnight UTC) before any request leaves. A job that continues still runs: triage, extraction and transcription keep memory current, run in code, and are the cheap part. Every call through the gateway stops at the ceiling, whatever job its token names, so a runaway agent loop stops there, and a Learner pass that waits leaves its requests queued for the reset. A new day starts a new row with the ceiling of `GROVE_DAILY_BUDGET_USD`. The operator changes a day's ceiling with `set_budget_ceiling`; `None` removes it. Calls that pass the gate at the same moment can end the day above the ceiling by the cost of those calls. Ruling S39 in `docs/store-map.md` has the store side.

The gateway also takes a token cap per run: a run token with `token_cap` is refused with `run_cap_reached` once the run's recorded input and output tokens reach it. The cap is checked before each call, and a stream is recorded when it ends. So one call can pass the cap by its own size, and calls of one run in flight at the same moment can pass it together, as with the daily ceiling.

## Cost

| Model | Basis | Price |
|---|---|---|
| Spark 1.3 | tokens | 1.25 USD per 1M input, 0.15 cached, 4.25 output |
| Voice Transcribe 1.0 | audio | 0.18 USD per hour |
| Glimmer on Modal | GPU seconds | 0.001097 USD a second (one H100, the dearer of the two GPUs the app asks for), times the call's wall time |

Glimmer's GPU seconds run from the start of the attempt that answered, so the 503s of a cold start are left out. The figure is an upper bound per call, because one container serves calls in parallel. The cold starts and the idle minutes before scale-down bill on Modal, not in `model_calls`.

## The gateway

The gateway is a FastAPI app (`grove.models.gateway`), served with `python -m grove.models.gateway` on `GROVE_GATEWAY_HOST:GROVE_GATEWAY_PORT` (`127.0.0.1:18300`). It holds the Meta key and the Glimmer key. OpenCode's provider key is a run token, minted with `grove.models.gateway_auth.mint` by whoever holds `GROVE_GATEWAY_SECRET`: it binds one world, one run and one job, and it expires (at most 6 hours).

For each call it refuses an unknown or contributor model and a model that does not chat, keeps only the fields and message keys it knows (so `store`, `ignore_eos` or `chat_template` never reach a provider), takes text parts, raster images as `data:` URLs, function tools and function tool calls only, and schemas that refer only inside themselves (a provider never fetches a URL or searches the web for an agent), applies the budget gate and the run cap, sets the effort from the job table (`reasoning_effort` for Spark, `chat_template_kwargs.reasoning_strength` for Glimmer; an effort the agent sends is dropped, so an agent cannot raise its own spend), keeps the output limit from 1,024 to 65,536, drops the fields Meta refuses, turns a `tool_choice` other than `"auto"` into `"auto"` (`"none"` removes the tools), adds a cache key from the system message, asks for the usage chunk on a stream, sends the call on, and writes it to `model_calls`. A stream reaches the client byte for byte. A pump task owns each stream and its record: when the client leaves, the pump reads on for up to 120 seconds to get the usage chunk, which Meta sends only at the end, so the real tokens count against the ceiling and the run cap; if the chunk never comes, the row carries an estimate of four characters a token. A call that fails, an answer that is not a JSON object or cannot be decoded, a redirect, and a stream the client closes early are written with their outcome; an empty completion is written as `invalid_output` and passes on as it came. A provider's error message reaches the caller with the keys the gateway holds, their parts, and any bearer value removed; a 200 answer and a stream pass as they came. An answer of any odd shape is still metered: a field that cannot be read counts as absent, and an answer with no token counts gets the estimate. A `budget_reached` refusal carries a `Retry-After` header with the seconds to the reset.

Only a failure that shows the provider did no work is sent again: a 429, a 503, a connection that never opened. A read timeout, a dropped read, and a 500, 502 or 504 may have run and billed, so they are sent once, and the row carries an estimate of the input (the layer does the same, and also for a background response whose poll failed). A 429 waits for its stated reset; a reset over 60 seconds comes back at once, with `Retry-After`, for the caller to reschedule.

A body over 32 MiB is refused with HTTP 413.

`GET /v1/models` lists the chat models. Errors use B.3's shape, `{"error": {"code", "type", "message", "details", "retryable"}}`.

OpenCode 1.18.32 sends this through the `@ai-sdk/openai-compatible` provider: `POST /v1/chat/completions`, always `stream: true` with `stream_options.include_usage`, `max_tokens` equal to the model's `limit.output`, and `tool_choice: "auto"`. Its first call of a run asks the small model for a title.

## The egress allowlist

`grove.models.egress` checks every outbound URL of the layer before a socket opens: https only, on port 443, to a listed host, with no user part. The layer's hosts are the Meta API host and the Glimmer host. The four agents have the same list, and the Curator alone may also reach the read-only connectors, which the `cleanup` lane adds. A redirect is checked hop by hop. A process that opens its own socket is out of this module's reach; the runner's network rules on the box cover it.

## Glimmer on Modal

`deploy/modal/glimmer.py` serves Muse Glimmer 30B (BF16, a pinned revision) with vLLM 0.32.0 on one A100 80 GB or H100, scaled to zero after five idle minutes, behind Modal's proxy auth. A server scaled to zero answers 503 until a container is up, so the layer and the gateway retry 503 on this route for about ten minutes. Glimmer's effort goes in `chat_template_kwargs.reasoning_strength`; vLLM ignores a `reasoning_effort` field without an error. The server fetches no remote media (`--allowed-media-domains media.invalid`, no media redirects). The file's docstring has the deploy steps.

## Settings

| Variable | Meaning |
|---|---|
| `MODEL_API_KEY` | The Meta Model API key. |
| `GROVE_META_BASE_URL` | Default `https://api.meta.ai/v1`. Only the host `api.meta.ai` loads: the allowlist comes from this URL, and the key goes to it. |
| `GROVE_GLIMMER_BASE_URL` | The Modal server's URL followed by `/v1`. Only a host under `.modal.direct` or `.modal.run` loads. |
| `GROVE_GLIMMER_KEY` | The Modal proxy token as `<id>.<secret>`. |
| `GROVE_DAILY_BUDGET_USD` | The ceiling of a new day's budget row; default 5.0; `unlimited` for none. |
| `GROVE_GATEWAY_SECRET` | At least 32 bytes; signs the run tokens. |
| `GROVE_GATEWAY_HOST`, `GROVE_GATEWAY_PORT` | Default `127.0.0.1` and `18300`. |

## Open points for later lanes

| Point | Owner lane |
|---|---|
| The job table has no row for load-bearing verification. D15 keeps those claims running at the ceiling and defers only the low-stakes ones; `verification` waits today. Add the job when the lane exists. | `truth-clock` |
| Who mints a run token, and how the runner holds `GROVE_GATEWAY_SECRET`. A pass needs a `token_cap`: the ceiling stops new calls, and the cap bounds one run. A pass that gets `budget_reached` leaves its requests queued. | `runner` |
| The judge's verdicts: A.1 has this layer record them in `judgments`, and no writer exists yet. | `truth-clock` |
| `transcribe` takes WAV only and no contact names or language bias: the forms probed for those fields were refused, and other encodings are not probed. | `ingest-a` |
| `brain fetch` must send through `AllowlistTransport`, not call `EgressPolicy.check` and then use another client: the transport is what checks each redirect hop. | `cleanup` |
| A process kill between an answer and its record loses that row, and so does a store failure at that moment. | `runner`, `deploy-a` |
| The runner's network rules: a process that opens its own socket is out of the allowlist's reach. `GROVE_GATEWAY_HOST` has no loopback check, because a container network may need another address. | `runner`, `deploy-a` |

## Tests

`server/tests/models/` holds the tests that CI runs: fake providers, the live dev store. `test_live.py` makes real calls on synthetic text and carries the `live` mark, so CI never runs it. It runs in a lane:

```
cd server
GROVE_SLICE_ID=models GROVE_LIVE_EVIDENCE_DIR=<dir> uv run pytest tests/models/test_live.py -m live
```
