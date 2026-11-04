# Ingest: connectors, uploads and the take-in pipeline

Read-only Calendar and local Git connections use this pipeline through `take_parsed`.
Their consent, scope, cursor and operator configuration are in `docs/source-connectors.md`.

Spec: A.4 (connectors), D2 (sources and episodes), D5 (speaker and source types), D10 (batches,
order and time), D13 (coverage), D14 (screening). This file is the contract for every lane that
adds a connector. Code: `server/grove/ingest/` and `server/grove/writer/ingest.py`.

## The pipeline

```
upload bytes -> parser (ConnectorSpec.parse) -> chats
  per chat: delta (keys not stored yet) -> bounded parts
  per part: cap message text -> Screener.screen_source
  per chat: episode cut over all kept messages (ConnectorSpec.cut)
           -> write_chat_delta: source parts + receipts + episodes + watermark, one transaction
after all chats: record_read (coverage: times, outcome, failed count)
```

`grove.ingest.take_in.take_in(driver, world_id, provider, upload, *, screener) -> TakeInResult`.

- A file that is not an export of the provider raises `NotAnExport` before anything is written
  (no connector row either).
- Screening runs in code before the sources table (D14, decision 6). It finishes before the
  write transaction opens (SC1).
- One chat's complete delta commits atomically, including every source part and receipt.
  Source parts do not change the connector's episode rule: one episode can name several
  sources. If another upload stored any delta key first, the writer raises `DeltaMoved`
  and the pipeline reads the chat's delta again (at most 3 tries).
- A re-import of the same export finds every key stored: it writes no source, no receipt and no
  episode, and answers `nothing_new`. Only the connector's coverage times move.
- A delta that screening drops whole writes empty source parts (`[]`) with their receipts,
  but only for a chat with no source yet. So a chat kept out whole is visible in coverage once, and a
  re-import of a chat with dropped messages stays idempotent.
- Screening is deterministic, so a later read that drops a message again counts it again in that
  read's receipt only when the read also stores new messages of the chat.

## The connector interface

```python
from grove.ingest import (ConnectorSpec, ParsedChat, ParsedMessage, Upload, NotAnExport,
                          register, message_key, chat_locator, clean_text, clip,
                          one_episode, silence_windows, utc_from_iso, utc_from_epoch)
```

A parser module registers one spec on import and is imported by `grove/ingest/parsers/__init__.py`:

```python
SPEC = register(ConnectorSpec(
    provider="whatsapp",               # the upload code (?provider=...)
    connector_type="whatsapp",         # enum connector_type
    parser_version="whatsapp.v1",      # stored on each source
    batch_rule="silence_90m.v1",       # stored on the connector row
    key_prefix="wa",                   # 1 to 8 lowercase letters, one per provider
    parse=parse,                       # Upload -> Iterator[ParsedChat]
    cut=silence_windows(90),           # default one_episode
    cut_rule="silence_90m.v1",         # stored on each episode
))
```

| Name | Rule |
|---|---|
| `ParsedMessage(key, text, at, speaker_kind, modality="text", sender=None)` | `key` from `message_key`; `text` through `clean_text` and not blank; `at` the message's own aware time or None, never the upload's time; `speaker_kind` and `modality` from the source's metadata (closed enums). `sender` is a display sender the source names (it is screened); leave it None when `speaker_kind` says all, because a one-word sender with a number in the text reads as a service to the one-time code rule. |
| `ParsedChat(chat_id, messages, started_at=None, ended_at=None)` | One chat in its own order. `chat_id` is the source's own id; it is only hashed. `started_at`/`ended_at` are fallbacks when no message has a time. Keys are unique within a chat. |
| `message_key(prefix, *ids)` | `<prefix><32 hex>`, one word: a fingerprint of the source's own ids for one message (A.4's message key). Deterministic, so a re-import and an overlapping later export repeat the key. No name can ride in it (store-map open point, spec line 2505). A rename form or a denylist term matches whole words only, so with no separator none can match the prefix: a rename map that holds "CC" or "Cai" changes no key. |
| `chat_locator(prefix, chat_id)` | The source's `external_locator`: one per chat, opaque, the same one-word form. |
| `register(spec)` | Refuses a key prefix whose keys or locators screening would change (with the detectors alone, and with the prefix as a rename form and as a denylist term): "dapi" glued to 32 hex reads as a secret, for example. The pipeline fails such a chat closed (`KeyChanged`). |
| `in_time_order(messages)` | Every chat's messages in their own time order (D10), including nonmonotonic stamps within one file; a message with no time stays after the one it follows. Every parser of this build calls it. |
| `clean_text(value)` | Lone surrogates become U+FFFD, NUL is dropped, line ends are LF. The store and the model layer refuse lone surrogates. |
| `Upload(stream, size)` | `head()`, `read_all(limit)`, `budget` (a `ReadBudget`, below). The file name is never passed: the parser knows the format by its bytes. |
| `ReadBudget` | `Upload` creates a budget from its size. Before retaining a new chat, call `upload.budget.take_chat()`; before retaining a new message, call `upload.budget.take_message(len(text))`. Repeated message keys do not consume another message charge. |
| `grove.ingest.archive` | `is_zip`, `zip_members(upload, suffixes=...)` (in memory, one member at a time, never extracted, with caps on the bytes actually read), `zstd_decompress(data, budget=upload.budget)`, `json_document`, `json_lines(data, budget=upload.budget)` (one line at a time, 1-based line numbers; a cut line is skipped). Pass `upload.budget` on every call. |
| `NotAnExport` | A generic message, never a value from the file. |
| Cut rules | `one_episode` (one session or the part added since the last read), `silence_windows(minutes)` (a window closes after that much silence; a message with no time joins the window it follows). |

## Limits of one upload

A small crafted file must not take the API's memory, so every reader counts what one upload
expands to, whatever its size on the wire. Past a limit the upload answers 400 `invalid_arguments`
(`NotAnExport`) and nothing is written. An export past them goes in parts: a re-import is
idempotent.

| Limit | Value | Counts |
|---|---|---|
| `GROVE_UPLOAD_MAX_BYTES` | 256 MiB | the body (the API and nginx) |
| `archive.MAX_MEMBER_BYTES` | 256 MiB | one zip member, one zstd stream, one non-zip body read |
| `MAX_READ_BYTES` | 768 MiB | ceiling for every byte read out of zip members and zstd streams, over the upload |
| `MAX_EXPANSION`, `MIN_READ_BYTES` | 64 times the body size; 64 MiB floor | actual archive-read budget: `min(768 MiB, max(64 MiB, 64 * upload.size))`; nested decompression charges the same budget at each layer |
| `MAX_RECORDS` | 1,000,000 | every JSON line, including blank lines and the empty line after a final newline |
| `MAX_CHATS` | 10,000 | distinct chats retained by the parser |
| `MAX_MESSAGES` | 1,000,000 | distinct messages retained by the parser |
| `MAX_ID_CHARS` | 1,024 | each source chat/session ID and message/record ID used for retention or key generation |
| `MAX_TEXT_CHARS` | 50,000,000 | message text across the upload, charged as `min(len(text), MAX_MESSAGE_CHARS)` per retained message |
| `archive.MAX_DOCUMENT_VALUES` | 8,000,000 | values of one JSON document, counted before it is parsed (each follows a comma or opens a container) |
| `archive.MAX_LINE_VALUES` | 1,000,000 | values of one JSON line; a line past it is skipped unparsed, as a cut line is |
| `MAX_PARALLEL_UPLOADS` | 1 | uploads at once; the next answers 429 with `retry_after_seconds` |

Call `bounded_id` before retaining each source identifier. Identifiers above the limit are
refused before chat retention or key generation. They are
never truncated or re-keyed: accepted identifiers retain their original keys and locators.
The bound prevents one long chat ID from multiplying hash work by the message count.

`ParsedMessage` bounds retained text as soon as the parser creates it. Before screening,
`kept_text` also keeps at most `MAX_MESSAGE_CHARS` (100,000) characters from a
message, or `MAX_SENDER_CHARS` (200) from a sender, plus a cut marker. It cuts at whitespace,
so a partial token cannot evade a detector. A single overlong token becomes only the marker.
The cut is deterministic. `parts_of` groups the capped text and sender into consecutive parts
of at most `MAX_SOURCE_CHARS` (4,000,000) characters, with at least one message per part.
Each part gets its own source and receipt. The episode cut runs once over the full kept delta,
and each episode names only the sources that contain its messages. All parts commit together.
This limit counts text before screening, not the serialized JSON's metadata or bytes.
A re-import retains the same keys and adds nothing.

The earlier synthetic macOS parse-and-screen probes measured 226 MiB of peak RSS growth for a
refused 16 MB ChatGPT zip, 1,117 MiB for a document just below the value cap, and 1,409 MiB for
9,000 chats with 990,000 messages. The last probe used an explicit full read budget and took
234 seconds to parse and screen; it did not write to the store. These probes predate the atomic
multi-source write and are not a universal memory or runtime bound. The api container has a
4 GiB memory limit, including its 320 MiB tmpfs, with no extra swap (`docs/deploy.md`).
A parser reads one file or document at a time and retains its messages;
ChatGPT and claude.ai JSON documents are parsed whole. Store time and proxy timeout behavior
remain separate from these measurements.

A stored source's content is the screener's JSON array; each row holds `key`, `sender`, `text`,
`at`, and, from these connectors, `speaker_kind` and `modality`. An evidence locator into it is
`key:<message key>` (S38): `past_ai_conversations` allows `key` and `line`; use `key`.

## Connectors of this build

| provider | connector type | key | locator from | episode |
|---|---|---|---|---|
| `claude_code` | past_ai_conversations | `cc`: session id, record uuid, speaker kind | session id | one session or its new part |
| `codex` | past_ai_conversations | `cx`: rollout id, line number | rollout (session) id | one session or its new part |
| `chatgpt` | past_ai_conversations | `gpt`: conversation id, message id | conversation id | one conversation or its new part |
| `claude_ai` | past_ai_conversations | `cai`: conversation uuid, message uuid, speaker kind | conversation uuid | one conversation or its new part |

What each keeps (A.4, the engine's filter carried over, D5): the person's messages
(`person`); the fact that something was attached, never its bytes or extracted text; the
assistant's text (`ai_host`: in the spec the AI host is the assistant the person talks to);
one-line digests of tool calls (`ai_host`); one-line digests of failed tool results
(`automated`). Dropped: thinking and reasoning, successful tool output, system and developer
messages, and text a program injected in the person's slot. A session a program ran (Claude
Code `entrypoint` `sdk-*`; Codex `exec`, SDK or MCP) has no `person` message: its prompts are
`automated`. A subagent's prompt is the parent agent's text, so `ai_host`: Claude Code
`isSidechain`, and a Codex session whose `source` is an object (`{"subagent": ...}`).

## Connector rows and coverage (D13)

One `:Connector` per world, type and provider (`provider` is a ruling column), made under the
world lock. `auth_reference` is `upload`.

| Column | Set by | Meaning |
|---|---|---|
| `watermark` | take-in | `at:<ISO time>` of the newest message read |
| `read_count` | take-in | episodes added |
| `oldest_unprocessed_item` | take-in (min), `ingest-b` (clears) | start of the oldest episode no pass has taken |
| `last_attempted_read`, `last_successful_read`, `last_outcome`, `failed_count` | take-in | the last read; outcome `took_in`, `nothing_new`, `kept_out` or `failed` |
| `held_nothing_count`, `extracted_count`, `items_taken_in_over_7_days` | `ingest-b` | what the update agent made of the episodes |

An episode with no `run_id` is unprocessed: that is the handoff to the update agent.

## The upload route

The dedicated Calendar reader can attach a typed `CalendarEvent` through
`ParsedMessage.calendar_event`. This is code-held provider data, not an `Upload` option.
`take_in` refuses these facts from any upload parser. `take_parsed` permits them only for
the dedicated Calendar provider/type/parser tuple. After screening, `write_chat_delta`
passes each source part's retained `calendar_events` map to `write_screened_source` in
the same transaction. Dropped messages leave no event facts. Calendar and Git readers
also pass exact-key speaker kinds separately. `docs/source-connectors.md` defines the
provider APIs and the D5 contract.

`POST /v1/sources/upload?provider=<code>`, the raw file as the body.

- Auth: the session cookie, or `Authorization: Bearer gut_...`, an upload-only token for one
  world. The token works on this route only.
- Content types: `application/zip`, `application/json`, `application/x-ndjson`,
  `application/jsonl`, `application/zstd`, `application/octet-stream`. Form types are refused,
  so a cross-site form cannot post here.
- Limit: `GROVE_UPLOAD_MAX_BYTES`, 256 MiB by default; nginx has a location with the same limit
  and no request buffering (`docs/deploy.md`).
- After identity resolves the World, the route refuses designated demo Worlds and creates
  the code-supplied `ordinary_upload_v1` screening policy for each ordinary upload. It runs
  all mandatory credential, card, code, reset-link and third-party contact detectors.
  No identifier is exempt, including the account owner's. Personal rename/deny rules and
  opt-in pay classes are not configured by this route. It never reads another account's
  global film policy files. Those strict file settings remain required by the existing
  operator and connector paths that use `ScreeningConfig.from_environment`.
- A genuine upload policy construction fault answers 503 `unavailable` before any body
  read. Missing global `GROVE_RENAME_MAP` or `GROVE_DENYLIST` files do not cause that error
  for this ordinary upload policy. There is no exact-fixture fallback or public policy selector.
- The answer: `{state, provider, connector_id, chats, chats_new, sources_added, episodes_added,
  messages_read, messages_kept, failed_chats, screened: {class: count}}`.

`GET /v1/sources/connectors` (session only) lists the world's connector rows with their coverage.

Successful retention creates screened Sources, Episodes, ScreeningReceipts and connector
coverage through the same `take_in` pipeline. Re-imports remain idempotent. It does not itself
create graph Items, links or Requests, or call a model. Graph extraction and worker runtime
acceptance are separate. A future personal-policy interface must bind explicit rules to the
same World and refuse invalid rules, rather than silently fall back to this baseline.

## Upload-only tokens

`python -m grove.ingest token mint --world <world id> [--minutes N]` prints `{id, token,
world_id, expires_at}` once: the raw token appears nowhere else. The row stores its sha256 only.
Life: 60 minutes by default, at most 24 hours. `token revoke --id <id>`; `token list --world
<id>` (ids and times, never a hash). On the box the command runs inside the api container
(`docs/deploy.md`).

## Internal text-export fact transport

The WhatsApp and text/Markdown adapters remain unregistered. Their `as_chat()` methods return
`ParsedMessage.source_facts` from `grove.source_facts`; they do not invoke the upload pipeline,
write sources or episodes, or call a model. No public input or accepted connector changes.

`TextSourceFacts` contains the exact artifact SHA-256, a typed byte/line/page locator and typed
source-time candidates. The contract is `text_export.v1`, with identity basis `export_digest`.
Facts are immutable, strictly typed and limited to 2,048 canonical UTF-8 bytes per message.
The JSON decoder rejects duplicate, missing and extra fields. Integers reject booleans.
Positions fit a nonnegative signed 64-bit integer; lines and pages start at one. Byte ranges
are nonempty and half-open. Each text parser checks the range against its original input.
The page variant exists as a validated type only; no PDF adapter uses this contract yet.

The message's `at` is the sole instant. An `instant` fact must match it exactly. A `local`,
`ambiguous` or `absent` fact requires `at=None`. Short years retain their width and unresolved
century. Invalid dates, UTC overflow, conflicting zone/offset values and falsely resolved
repeated-hour times refuse. A source offset or zone never establishes a person's timezone.

`ParsedMessage` permits a null speaker only with these typed facts. Calendar facts cannot
coexist with text facts. The internal adapters leave speakers unknown; display names, bot
names, forwarded text, caller arguments and upload ownership establish no speaker authority.

For internal in-memory screening, pass `source_identity=WHATSAPP_TEXT` or
`source_identity=DOCUMENT_TEXT` to `Screener.screen_source`. The exact tuples are
`("whatsapp", "whatsapp.export.v1")` and `("document", "document.text.v1")`; their locator
kinds must be `line_bytes` and `bytes`, respectively. They are a code whitelist, not provider
registrations. Dictionaries, mixed typed/legacy messages and other tuples refuse before
screening. These Python types and tuples are internal contracts, not authentication tokens.
No HTTP option or model field can select this path. Future persistence still requires an
independent transactional producer/tuple check and must not trust decoded JSON as authority.

The real Screener still processes text, sender, key and external locator. It carries facts
only beside retained messages, with an explicit null `speaker_kind` when unknown. A dropped
message or whole source leaves no facts in the result. Legacy serialized bytes stay unchanged
when facts are absent. Facts remain private content, with no erasure exemption.

WhatsApp adapters restore original source order even when the component's presentation view
sorts local wall times. Text and equivalent ZIP wrappers share transcript identity and keys.
Documents retain original bytes and section offsets as identity inputs. The chat namespace
is `export_digest:<sha256>`. Different overlapping exports remain different artifacts;
identical unrelated transcripts cannot be distinguished by bytes alone. These tests prove
repeat parser identity, not transactional re-import idempotence or independent evidence.

Persistence, extraction, holds, cross-artifact evidence deduplication, speaker resolution,
production counts and typed-content erasure remain separate integration work. No complete
D5/D10, media, or release acceptance follows from this internal transport.
See `docs/ingest-media.md` for the component limits.

## Internal pure retention prerequisite

`grove.ingest.text_retention.prepare_text_delta` prepares an immutable `TextDeltaDraft` in
memory only. No production consumer imports it, and the four registered providers stay
unchanged. The draft is not a `ScreenedSource`, writer token, admission result or import
success. Its statuses are `empty_delta`, `kept_out` and `draft_only`.

The two exact internal identities use `whatsapp.export.v1` and `document.text.v1`.
WhatsApp accepts transcript bytes only; ZIP and binary signatures refuse. Documents use
`parse_text(markdown=True, source_at=None)` with no caller speaker metadata. Original keys,
artifact identity and source order stay intact. `already_stored` only simulates a delta;
actual replay and erasure fences require later transaction-local checks.

Only bounded synthetic `DraftScreeningRules` reach the real Screener. The wrapper never loads
privacy configuration from files or the environment. Fact values remain unadmitted. Real
fact-policy screening, erasure/restore compatibility, provenance guards and an authorized
writer integration remain prerequisites for any persistence.

| Internal draft limit | Bound |
|---|---|
| Exact input bytes | 16 MiB before parser allocation |
| Parsed messages / stored keys | 1,000 each; stored keys are an exact frozenset |
| Key size | 1,024 characters; aggregate key UTF-8 bytes at most 1,024,000 |
| Synthetic rules | 32 rename pairs, 32 denylist terms, 32 own identifiers |
| Rule strings | 128 characters / 512 bytes; rename labels at most 64 bytes; aggregate 32,768 bytes |
| Preliminary screening group | Locator/key/sender/text total 31,250 characters; canonical facts total 2,048,000 bytes |
| Final parts | At most 64; 4,000,000 UTF-8 bytes each; 16,000,000 total |

Pre-screen groups refuse an oversized single row. Retained JSON uses the existing
`ScreenedMessage.as_row()` fields and `json.dumps(sort_keys=True, ensure_ascii=False)`
spacing. Exact byte accounting includes metadata, escaping and array punctuation. Final
parts stay within their screening group. Group receipt counts attach once to its first
part; an entirely dropped group retains one empty diagnostic part and no episode.

Cuts operate across all retained rows, independently of final part boundaries. Fully timed
WhatsApp rows split only at a forward adjacent gap greater than 90 minutes. Each cut uses
its true minimum/maximum time. Any retained unknown time selects one null-bound WhatsApp
cut. Documents use one cut with absent times. Unknown speaker/time counts overlap;
`held_source_messages` counts their union. No display sender establishes speaker authority.

`grove.source_fact_values` is separately pure: standard library plus `grove.source_facts`
only. Its `text_fact_values.v1` projection preserves every local candidate, year width,
precision, exact UTC seconds, explicit signed offset and retained timezone spelling.
Explicit typed selectors match whole message keys. Full-batch validation includes
nonmatches, duplicates, Unicode, a 1,000-row limit and a 4,000,000-byte canonical record
limit. Only this budget view serializes `at` as null or canonical UTC seconds; it does not
change Source JSON. These helpers do not authenticate intent or perform erasure.

## Browser materials

`/v1/materials` provides session-only file and note retention. Supported files are UTF-8 text,
Markdown, textual PDF, DOCX and PPTX. Readers reject unsupported embedded content instead of
silently omitting it. Titles and body text pass the mandatory upload Screener before storage.
Original binaries are never retained. File timestamps and authors remain unknown; the source
facts retain the original digest and byte, page or office block positions. Files have no Request
and report `held_provenance` until the typed admission prerequisite is complete.

A note uses the actual submission instant and person source classification. Its body enters one
ordinary Request and one Episode in the same transaction as the material revision. Edits create
new submissions. Deletion erases every revision through the ordinary D14 approval and ledger.
