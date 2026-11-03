# The writer

The writer is the only path into storage. Models and agents propose operations; the writer checks
every one of them in code, commits the batch whole or not at all, and answers with a receipt that
it builds from a fresh read of what committed. A failed commit is an error, never a receipt.

This file is the public API. Other lanes build on these names. A rule in the table at the end has
a contract test in `server/tests/writer/`, named in the right column.

## Entry points

```python
from grove.writer import (
    Caller,
    apply_batch,
    render_receipt,
    BatchDeclined,
    WriteFailed,
    WriteOutcomeUnknown,
    ReceiptUnavailable,
    Refusal,
    Receipt,
    record_connector,
    record_source,
    record_episode,
    enqueue_request,
    write_request_row,
    claim_next_request,
    apply_request,
    finish_request,
    fail_request,
    request_state,
    RequestState,
    ClaimedRequest,
    committed_for_request,
    decline_log,
    pending_requests,
    surfaced_requests,
    worlds_with_due_requests,
)
```

| Function | What it does |
|---|---|
| `apply_batch(driver, world_id, caller, batch, *, request_id=None, run_id=None) -> Receipt` | Checks and commits one batch, and with it its request (see "Requests"). With no `request_id` it mints one, so every change entry and every decline has a key. Raises `BatchDeclined` (nothing of the batch written; the decline is logged), `WriteFailed` (nothing written, retry may help), `WriteOutcomeUnknown` (the connection dropped during the commit, so the batch may be saved) or `ReceiptUnavailable` (the batch committed but its receipt could not be read). After the last two a replay with the same `request_id` and the same batch is safe: the request row says whether it committed, and the replay writes nothing if it did. They carry `commit_id`, `change_set_id` and `request_id`, and `retryable` is false. `WriteFailed` carries `request_id` too. Returns a `Receipt` only after the commit, read back in a fresh read transaction. |
| `render_receipt(driver, world_id, receipt) -> str` | The receipt in the fixed grammar of D2. Titles, meaning names and values are read fresh, so a stored receipt holds only ids and codes. |
| `record_connector(driver, world_id, type, *, settings=None, staleness_limit=None) -> str` | One `:Connector` row. Returns its id. |
| `write_screened_source(tx, screened, *, source_id, connector_id, parser_version) -> dict` | One `:Source` and its `:ScreeningReceipt` in the caller's transaction, from a `ScreenedSource`. Text from a connector reaches `sources` only here or in `add_screened_source`. |
| `add_screened_source(writer, screened, *, connector_id, parser_version, source_id=None) -> dict` | The same in a transaction of its own. With no `source_id` it mints one. |
| `record_source(driver, world_id, *, connector_id, external_locator, content=None, content_pointer=None, parser_version, untrusted=False) -> str` | One `:Source` row with the text as given, or a pointer. Returns its id. For the writer's own tests: a test fails when code outside this package reaches for it. |
| `record_episode(driver, world_id, *, connector_id, source_ids, first_message_key, last_message_key, start_time, end_time, cut_rule, run_id=None) -> str` | One `:Episode` row, keyed on the connector and the first message key. A later cut of the same window keeps the id and extends the range. Returns its id. |

The package also exports `ReceiptLine` (one line of a `Receipt`), `DeclinedReceipt` (the type of
`BatchDeclined.receipt`), `InvalidRecord` (raised by the three `record_` functions, nothing written),
`HTTP_STATUS`, and the queue's constants `MAX_ATTEMPTS`, `BACKOFF_SECONDS`, `LEASE_SECONDS` and
`QUEUE_WORKERS`. The three `record_` functions are not B.2 operations and take no `Caller`: a
layer that exposes them to a token checks its scope first.

Read helpers on the read side, for tests and for later lanes: `grove.store.asof.item_as_of(query,
item_id, world_time)` replays an item's change entries to a world time (the head rule of D1),
and `grove.writer.receipts.short_id(id)` gives the short form a receipt prints.

### What is current (read side)

```python
from grove.store.current import standings, current_items, standing, has_ended, link_standing
from grove.store.evidence_counts import distinct_episodes
```

D3's `current()` is one module, and every read and every guard asks it (S43). Each function that
takes a `source` takes a `WorldQuery` or a `WorldTx`; in a `WorldTx` it reads that transaction's
own writes, so a guard inside a batch sees what the batch wrote.

| Function | What it does |
|---|---|
| `standings(source, item_ids) -> dict[str, Standing]` | The standing of each item's head row at the world's `clock.now`. An id with no item is left out. |
| `current_items(source, item_ids) -> list[str]` | The ids, in order, whose standing is `current`. |
| `standing(row, *, at, lifecycle_class, planned_origin=False, scheduled=None, successors=()) -> Standing` | The pure rule for one row: a head row, or a row from `item_as_of`. `Standing` holds `state` (`current`, `planned`, `reported`, `not_started`, `ended`, `overtaken`), `end_class`, `status_role`, `scheduled` (`{role: value}`), `successors` (`Successor(item_id, link_id, meaning)`) and `reason` (a code), and `is_current`. |
| `has_ended(row) -> bool` | Rule 1 alone: an end class (`spent` too), a status role in `TERMINAL_ROLES`, or a stop clock. The writer's guards call it. |
| `link_standing(row, *, at) -> Standing` | A link's state at `at`: `ended`, unless its stop clock lies wholly after `at` (never for `refuted`); `not_started` for a future start; else `current`. |
| `distinct_episodes(source, target_ids) -> int` | One count of distinct episode ids among the active supporting rows of all the targets together (spec line 766). |

The rules of `standing`, in order: (1) `ended`; (2) `overtaken` by an active "corrected by" or
"superseded by" successor; (3) a bounded period, or a record that a plan scheduled, reads by its
evidence and never by a date: confirmed and not inferred goes on (a period, or a record with a
scheduled start, also needs a known start), reported done and not inferred reads `reported`,
anything else reads `planned`; (4)
`not_started` for a known start after `at`; (5) `current`. The successors and the scheduled values
ride on every state.

### The resolver

```python
from grove.writer.resolver import resolve_word, resolve_link_phrase, lexical_candidates
```

D8's reuse first (S44). The writer's own lookups on `Registry` are its strict form; extraction and
the clean-up agent use the soft forms below. `Registry.load(source)` takes a `WorldTx` or a
`WorldQuery`.

| Function | What it does |
|---|---|
| `resolve_word(registry, word_type, phrase, *, kind_id=None) -> Resolution` | One phrase against the registry's usable words, in D8's order: an exact id or name, the normalised name, an alias; and only when none matched, the same again with a leading "is" or "are" dropped. `word_type` is `kind`, `meaning` or `property_name`, and `kind_id` is for property names only. `Resolution` holds `outcome` (`resolved`, `none`, `ambiguous`), `target`, `reversed`, `via`, `phrase`, `normalised` and `candidates` (`{id, name, reversed}` of an ambiguous phrase). Raises `Refused` (rule `empty_phrase`) for a phrase that normalises to nothing. |
| `resolve_link_phrase(registry, phrase) -> LinkWord \| None` | A resolved phrase gives its meaning, with no phrase kept. An unmapped or ambiguous phrase rides on the generic meaning, with its phrase kept and its candidates. `LinkWord` holds `meaning`, `reversed`, `original_phrase`, `candidates` and `outcome`. A phrase that does not resolve gives `None` when the world has not exactly one usable generic meaning with no consequences. |
| `lexical_candidates(source, kind_id, phrase, *, exclude_distinct_from=None) -> list[Candidate]` | Every item of the kind at trigram similarity 0.3 or more (`CANDIDATE_THRESHOLD`) over its title and its item aliases, best first. `Candidate` holds `item_id`, `score` (3 places) and `ended`: an ended item is flagged, never dropped. With `exclude_distinct_from`, that item and its distinct pairs are left out. |
| `title_scores(source, phrase, titles) -> dict[str, float]` | The scoring that the candidate band and the near-duplicate gate share. |

## The caller

```python
Caller(kind="update", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:learner")
```

- `kind`: one of `update` (U), `cleanup` (C), `consolidation` (K), `ai_host` (H), `person` (P),
  `writer` (W). Each operation lists the kinds it accepts (B.2).
- `scopes`: a write needs `grove.write` or `grove.propose`. A caller with neither, such as a
  read-only token, is refused by every operation with `scope_denied`, before the store is read.
- `actor`: a code stamped on every change entry (never a person's name).

## The batch

A batch is a JSON object with two arrays. `evidence` holds the observations (one place in one
source), as in `grove.extract.v1`. `ops` holds the operations, in order: a later operation sees
what an earlier one did.

```json
{
  "evidence": [{
    "ev_id": "e1", "episode_id": "<episode id>", "source_id": "<source id>",
    "locator": "line:12", "message_key": "<connector key>", "source_time": "2026-10-28T09:15:00Z",
    "statement_class": "report_happened", "derivation": "observed", "premises": [],
    "speaker_kind": "person", "speaker_item": null, "original_author": null, "modality": "text",
    "source_offset_minutes": 240, "person_timezone": "Asia/Dubai"
  }],
  "ops": [{
    "op": "create_item", "op_id": "o1", "temp_id": "$i1", "kind": "task",
    "title": "Draft the venue list", "status": "open",
    "became_true": {"earliest": "2026-10-28T09:15:00Z", "latest": "2026-10-28T09:15:00Z",
                    "precision": "minute", "words": "this morning"},
    "recheck_by": "2026-11-04T00:00:00Z",
    "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
    "reason": "the message assigns the task"
  }]
}
```

- A temp id starts with `$` and is declared by `temp_id` on `create_item` or `create_link`. Any
  later field that takes an item or link id also takes a temp id of this batch.
- `evidence_refs` on an operation turns each cited observation into one evidence row whose target
  is the operation's item or link. An `ev_id` names an observation of this batch; `evidence_id`
  names an active evidence row that already exists, and lends it its observation. Each ref has a
  `polarity`: `supports` or `contradicts`. A ref whose (message key, target, polarity) row exists
  already writes no row: the operation's change entries and provenance name that existing row.
- An observation's `speaker_kind` is the caller's classification. It is stored, but it never raises
  a row's source type: the type comes from the connector and the statement class only.
- A clock is `"unknown"` or `{"earliest", "latest", "precision", "words"}`: `before` has only
  `latest`, `after` only `earliest`, every other precision has both, earliest not after latest.
  Times are ISO 8601 with an offset; the writer stores UTC. `words` (optional) is stored as a
  value id.
- The writer sets `id`, `world_id`, `recorded`, `version`, `touched_at`, the derived source type,
  the derivation, the speaker and the modality of a claim, and `provenance_evidence_id`. A caller
  that sends one of them is refused.

## Operations

| Operation | Callers | Fields (required in bold) | Receipt line |
|---|---|---|---|
| `create_item` | U C K P | **op_id**, **kind** (id, name or alias), **title**, **became_true**, **evidence_refs**, **reason**, temp_id, summary, body, properties (object of declared property names), search_terms (list of strings), status (required for a kind whose lifecycle class is `work_with_state`, optional for the others), recheck_by, recheck_how, distinct_from (list of `{item, reason}`) | `+ item 9a1e0c33 (task): <title>` |
| `update_item` | U C K P | **op_id**, **item**, **version**, **fields** (any of title, summary, body, properties, search_terms, became_true, recheck_by, recheck_how), **evidence_refs**, **reason** | `* item 44b0aa12: <field>=<value>` |
| `change_status` | U C K P W | **op_id**, **item**, **status**, **evidence_refs**, **reason**, version, stopped_being_true (terminal roles only, and refused on a change that goes to history only), recheck_by | `* item 44b0aa12: status=<status>` |
| `end_item` | U C K P | **op_id**, **item**, **end_class**, **stopped_being_true**, **evidence_refs**, **reason**, version | `x item 44b0aa12 (<end class>)` |
| `reopen_item` | C K P | **op_id**, **item**, **reason**, evidence_refs, recheck_by, version. Its world time comes from evidence_refs; with none it is unknown (its entries carry no world time and the status since is left unset), never the processing time | `^ item 44b0aa12 reopened` |
| `create_link` | U C K P H | **op_id**, **from**, **meaning** (id, name or alias), **to**, **became_true**, **evidence_refs**, **reason**, temp_id, recheck_by, recheck_how, original_phrase (generic meaning only) | `> link 5c21aa90: 9a1e0c33 -[blocks]-> 31cdee01` |
| `end_link` | U C K P W | **op_id**, **link** (not of a primitive meaning), **end_class**, **stopped_being_true**, **reason**, evidence_refs | `x link 2f07ab11: ... (<end class>)` |
| `attach_evidence` | U C K P W | **op_id**, **target** (item, link or belief id), **ev_id** or **evidence_id**, **polarity**, judgment_ids | `= item 77d2aa01 evidence e88f0a1b (supports, said)` |
| `end_evidence` | C K P | **op_id**, **evidence**, **end_reason** (`correction`, `forget`, `merge`) | `- evidence e88f0a1b (correction)` |
| `correct` | U C K P | **op_id**, **target** (the record), **successor** (an item id or a temp id of this batch), **evidence_refs**, **reason**, no_head_update_reason | `> link 5c21aa90: 9a1e0c33 -[corrected by]-> 31cdee01`, with the head's own lines |
| `refute` | U C K P | **op_id**, **target** (item or link), **evidence_refs**, **reason**, version | `x item 44b0aa12 (refuted)` |
| `supersede` | U K P | **op_id**, **target**, **successor**, **evidence_refs**, **reason**, stopped_being_true, version | `x item 44b0aa12 (superseded)` and the `superseded by` link |
| `merge` | C K P | **op_id**, **survivor**, **absorbed**, **reason**, evidence_refs | `* item a3b7c1d2 merged into b7e8f9a0`, the re-pointed links |
| `undo` | P W | **op_id**, **change_set**, **reason** | `u change set 18422aa0 undone`, then the inverse lines |
| `retire_record` | K | **op_id**, **record**, **lesson** (the belief or head that holds its lesson), **reason**, version | `x record 9a1e0c33 (spent; lesson in b7e8f9a0)` |

`accounts.py` is not a B.2 operation: it holds sign-in's writes (S20, S40), users, sessions, email
codes and the person's own item. Only `server/grove/api/auth.py` calls it. `docs/signin.md` has the
flows.

D3's composite operations live in `composite.py` (`writer-b`). Each moves lineage, the head and the
links in the batch's one transaction, and nothing is deleted:

- `correct` adds a "corrected by" link from the record to the item that carries the newer truth.
  The record's row is not written at all: it stays byte for byte as it was, `touched_at` and
  `version` included. The head's update rides in the same batch as its own operations
  (`update_item`, `change_status`, `create_link`, ...), or `no_head_update_reason` says why there is
  none (rule `head_update_required`). The successor may be minted earlier in the batch.
- `refute` ends an item or a link as `refuted`, with no world end time: it was never true. The
  evidence must rank at or above the claim (planned < said < reported_done < confirmed; an inferred
  row or claim below planned), rule `refute_rank`; the person's own word passes (D9).
- `supersede` ends the old item as `superseded`, with a stop clock (sent, or `before` the evidence
  time), and adds "superseded by" to its successor.
- `merge` needs one lifecycle class (rule `merge_lifecycle`) and every active link of the absorbed
  item fitting the survivor (`endpoint_kind_mismatch`, rule `merge_link_fit`). The absorbed item
  ends as superseded by the survivor (its successor link starts at its own start, so it reads
  overtaken at every time); each of its active links ends (`superseded`, no stop clock) and is
  created again on the survivor with the same clocks, provenance and evidence rows, or adds its
  evidence to the survivor's link with the same key; its title becomes an item alias of the
  survivor, and its own item aliases move to the survivor.
- `undo` appends the inverse of one change set, newest entry first, each entry with `inverse_of`:
  a field goes back to its old value, a late value gets a late inverse at its own world time, a
  created item or link ends as `refuted`, a created evidence row ends (`correction`), an ended one
  is active again, a created registry row is `retired`. It is refused when a row the change set
  moved has moved since (`version_conflict`, rule `undo_conflict`), when the change set was undone
  already (`already_undone`), when it is the starter set (`not_undoable`), or when a value it holds
  was forgotten (`value_forgotten`). An undo can itself be undone.
- `retire_record` ends a record on the record axis only: `end_class` `spent` and `lesson_in` (the
  new item column), both entries with no world time, and no stop clock. `current()` stops returning
  it; history and world-axis as-of reads keep it, and its evidence rows stay active.

A "corrected by" or "superseded by" link starts at the operation's evidence time, never after now
(rule `successor_in_future`, S46). No operation edits a successor link by hand: `create_link`,
`end_link` and `refute` refuse a primitive meaning; only an undo ends one. A belief is refused by
these operations (rule `belief_transition`): the beliefs lane moves beliefs by D9's state machine.

The other B.2 operations belong to later lanes: verify and the confirmation at create (`truth-clock`);
beliefs and contradictions (`beliefs`); proposals and marks (`walk`); questions, holds and
mentions (`cleanup`, `ingest-b`); ontology operations (`ontology`); forget (`forget`).

Two B.2 clauses of the operations above wait for the `walk` lane, because no walk and no R1 ending
exist yet: "triggers the walk" on `change_status` and `end_item`, and "R1's endings come back only
as a proposal" on `reopen_item`.

## Refusals

`BatchDeclined.refusals` is a list of `Refusal(op_id, op, code, rule, message, details)`. `code`
is a typed error code of B.3 (`grove.writer.errors.HTTP_STATUS` maps it to its HTTP status);
`rule` names the writer rule. The writer checks every operation, so one decline can name several
operations; an operation that depends on a declined one is refused with rule
`depends_on_declined`. `BatchDeclined.retryable` is true only for a stale version: the next pass
re-reads and proposes a new batch. `BatchDeclined.receipt` renders as

```
declined (batch of 3, nothing written), request 7f3c90d1
  ! link 9a1e0c33 -[lives in]-> 31cdee01: endpoint_kind_mismatch (from_kind)
```

Every decline is logged, one `:Decline` per refused operation, under the request's id (`decline_log`
reads them). A row holds ids and codes only, never the message or the details, and an operation id
that could hold text is stored by its hash. A refusal that never reached the request is not logged:
`scope_denied` with rule `write_scope` or `queue_worker`, `request_key_reused`, `repair_spent`,
`lease_lost`, and `not_found` from `enqueue_request` or `apply_request`. A near-duplicate that passes
on a named `distinct_from` writes a row too, with `override_used` true.

A kind, a meaning or a property name that reaches two usable words is never picked: it is refused
with `unknown_kind`, `unknown_meaning` or `invalid_arguments`, all with rule `ambiguous_phrase` and
the candidates in `details`. A phrase that normalises to nothing (`"___"`, `"..."`) is refused with
`invalid_arguments`, rule `empty_phrase`. The near-duplicate gate weighs only items with no end
class, at 0.8 (`DEFAULT_THRESHOLD`, Appendix C): one starting value until `ingest-b` calibrates it
per kind.

## Receipts

A `Receipt` holds `state` (`saved`), `commit_id`, `change_set_id`, `request_id` and `lines`.
`commit_id` and `change_set_id` are `None` only for a request that a pass finished with nothing to
write (`finish_request`); it renders as `saved (0), request 7f3c90d1`, then a line that names the
source.
Each line is ids and codes only (symbol, entity type, entity id, field, value id, end class,
flags). It is built after the commit from the change entries of the commit, read back in a fresh
read transaction. `Receipt.to_json()` is the stored form. `render_receipt` prints, for example:

```
saved (4), commit 0c33aa17, request 7f3c90d1
  + item 9a1e0c33 (plan): Thursday demo rehearsal
  > link 5c21aa90: 9a1e0c33 -[blocks]-> 31cdee01
  x link 2f07ab11: 44b0aa12 -[assigned to]-> 88aa1290 (superseded by new link)
  = link 77d2aa01: 44b0aa12 -[part of]-> 0b3fe712 (already known; evidence added)
```

A short id is the last 8 hex digits of the id, the random part of a UUIDv7. A change that goes to
history only also prints its world time as month and day (UTC), as in D1:
`* item 44b0aa12: status=open at 03-03, history only (the head is newer)`. Its line carries that
time as `world_time` (ISO text).

## Requests

A request commits exactly once, in one batch, and `saved` moves in the same transaction as that
commit. A kill before the commit rolls everything back, so the next pass may extract again. A kill
after it leaves `saved`, so no pass runs again. There is no list of batches on the row.

- **More facts for the same words take another id.** A pass with more to say mints a new request:
  `request_id=None`, or `<key>:2`.
- **A backfill re-ground takes a new id per try:** `<K>:g<N>`. The evidence key (message key, target,
  polarity) still holds layer 1, so a second try adds no row for a message it read before.
- **A request id** is `[A-Za-z0-9_.:-]{1,128}` at every entry point, or `ValueError`. It lands on
  change entries, and a receipt prints part of it, so `hosts-b` passes a hash of the host's
  idempotency key, prefixed with the OAuth client id.

| From | What happens | To |
|---|---|---|
| absent | `enqueue_request` | `queued` |
| due: `queued` with `retry_at` absent or past, `failed` with `retry_at` past, `applying` with `lease_until` absent or past | `claim_next_request` | `applying`, attempts + 1, with a lease. With the attempts at `MAX_ATTEMPTS`: `failed`, surfaced, code `attempts_exhausted` |
| absent, `queued`, `applying`, `failed`, or `declined` for its repair | the batch commits | `saved`, in the batch's transaction |
| absent, `queued`, `applying`, `failed`, or `declined` for its repair | the batch is declined | `declined`, in the log transaction. On a declined row it spends the repair |
| `queued`, `applying`, `failed`, `declined`, with a `source_id` | the only refusal is a stale version | `queued` with a `retry_at`, code `version_conflict`. A conflict on a repair counts as the repair |
| `queued`, `applying`, `failed`, with a `source_id` | the store failed, `WriteFailed` | `failed` with a `retry_at`, code `unavailable` (best effort) |
| `applying`, by the holder | `finish_request` | `saved`, with no commit |
| `applying`, by the holder | `fail_request` with `retryable` and attempts left | `failed` with a `retry_at` |
| `applying`, by the holder | `fail_request` with no retry, or at `MAX_ATTEMPTS` | `failed` with no `retry_at`: surfaced |

Who can be claimed: only a row `enqueue_request` wrote, that is one with a `source_id`. A direct
`apply_batch` never leaves a claimable row. `saved` is terminal. A row that a claim did not move
counts its attempt when it commits or when its decline is logged, so a saved row shows at least 1.

`request_state(driver, world_id, request_id) -> RequestState | None` is a pure read. A `RequestState`
holds `request_id`, `status`, `receipt`, `declines`, `attempts`, `last_error_code`, `source_id`,
`run_id` and `retry_at`. `api_state` is `saved` only when the status is saved and a receipt is in
hand, `declined`, `failed` when surfaced, and `saving` for everything else, also for a `failed` row
that waits for a retry. `to_api()` is B.3's `{state, request_id, receipt?, declines?,
last_error_code?}`. A receipt in it holds ids and codes, so a host renders through `render_receipt`.

A replay is keyed on the row, by the fingerprint of the batch (sha256 of its sorted JSON):

| The row | The batch | What happens |
|---|---|---|
| saved | the same | No write. The receipt is read fresh, and attached if the row has none. |
| saved | another, or any batch after a finish | `invalid_arguments`, rule `request_key_reused`. Nothing written, not logged. A shape fault too: a silent "saved" for other content would be D2's own bug. |
| declined | the same | No write. `BatchDeclined` again, from the stored refusals, with the near-duplicate candidates in `details`. |
| declined | a new one, `repairs` 0 | The one repair attempt runs. A decline of it spends the repair, a shape fault too. |
| declined | a new one, `repairs` 1 | `invalid_arguments`, rule `repair_spent`. Nothing written, not logged. |
| queued, applying, failed, absent | any | It applies. |

The rules run with no lock before the world lock, so replays of a saved request never queue behind
it, and again under the lock, because the first read can be stale. A batch's transaction takes
`WorldTx.lock_world()` first (one batch at a time per world), then the request row by `MERGE`. Any
decline rolls all of it back, the row included, and the log writes the row afterwards.

### The queue

`enqueue_request` needs a write scope, any caller kind. `claim_next_request`, `finish_request` and
`fail_request` need a kind in `QUEUE_WORKERS` (`update`, `cleanup`, `consolidation`) and the scope
`grove.propose`; anything else gets `scope_denied`, rule `queue_worker`, before the store.

| Function | What it does |
|---|---|
| `enqueue_request(driver, world_id, caller, request_id, *, source_id=None, write_source=None, content_key=None, run_id=None, verbatim=None, host_message_id=None, said_at=None, about=None) -> RequestState` | Makes the request durable. Exactly one of `source_id` and `write_source`. `write_source(tx) -> source id` runs inside the transaction only when the row is new, so one key stores its words once; screening's `write_screened_source` plugs in here, and it needs `content_key` (the caller's hash of the screened content). With `source_id` the content key is the source id. A second call with the same content writes nothing. Other content under the key raises `BatchDeclined`, rule `request_key_reused`. An unknown `source_id` is `not_found`. A store failure raises `WriteFailed`, and a retry with the same key is always safe. |
| `write_request_row(tx, caller, request_id, *, ...) -> (row, fresh)` | The same, for a caller that holds a transaction. |
| `claim_next_request(driver, world_id, caller, *, worker, lease_seconds=LEASE_SECONDS) -> ClaimedRequest \| None` | Moves the oldest due row to `applying`, counts an attempt and sets the lease. A row whose attempts ran out is surfaced with `attempts_exhausted`, and the claim moves to the next row. `ClaimedRequest` holds `id`, `source_id`, `run_id`, `attempts`, `caller`, `last_error_code`, `verbatim`, `host_message_id`, `said_at` and `about`. |
| `apply_request(driver, world_id, caller, request_id, batch, *, run_id=None) -> RequestState` | `apply_batch` under an existing row, or `not_found`: only an enqueue makes a request that can read "saving". A decline is a state with `declines`. `WriteFailed`, `WriteOutcomeUnknown` and `ReceiptUnavailable` are `saving` with `unavailable`, `outcome_unknown` or `receipt_unavailable`. `request_key_reused`, `repair_spent` and `scope_denied` propagate as `BatchDeclined`, and a writer fault propagates as itself. |
| `finish_request(driver, world_id, caller, request_id, *, worker) -> RequestState` | A pass that found nothing to write: `applying` to `saved`, with a receipt with no commit. A worker that is not the holder gets `lease_lost`. Any other status comes back unchanged. |
| `fail_request(driver, world_id, caller, request_id, code, *, worker, retryable=True) -> RequestState` | A pass that failed: `applying` to `failed`, by the holder only. With `retryable` and attempts left it waits for a retry; otherwise it is surfaced. Any other status comes back unchanged. |
| `committed_for_request(driver, world_id, request_id)` | The request's change entries in order, ids and codes only. |
| `decline_log(driver, world_id, *, rule=None, request_id=None, limit=100)` | The decline log, newest first. |
| `pending_requests(driver, world_id, *, limit=100)` | The rows a claim could take now. |
| `surfaced_requests(driver, world_id, *, limit=100)` | The requests that read `failed`, for health. |
| `worlds_with_due_requests(driver, *, limit=100) -> list[str]` | The runner's watcher: world ids only. |

**Back-off.** `MAX_ATTEMPTS` 5. `BACKOFF_SECONDS` (30, 60, 120, 240), indexed by the attempts and
capped at the last. `LEASE_SECONDS` 300. A stale version on an enqueued row goes back to `queued` with
a `retry_at`, and the next pass re-reads and proposes a new batch. On any other row it logs its
`:Decline` rows and writes no request row. A store failure moves an enqueued row to `failed` with a
`retry_at`, best effort: a failed write of that is harmless, because the lease brings the row back. A
direct call has no row, so its next try starts over.

**For `runner`.** A killed pass reads `saving`, and its row is `applying` until its lease passes. The
watchdog that sees a pass die calls `fail_request(retryable=True)`, and sets `lease_seconds` to the
watchdog time plus a margin. A worker name is unique per pass, because `lease_lost` compares it.

**For `hosts-b`.** Pass a hash of the host's idempotency key as the request id, prefixed with the
OAuth client id. Build the 5 second wait of remember from `request_state`. Render a receipt through
`render_receipt`. Answer a surfaced failure on remember with the typed error `unavailable`.

## The rules

| Rule | Code | Test |
|---|---|---|
| A caller with no write scope is refused by every operation | `scope_denied` | `test_bypass.py` |
| An operation from a caller kind it does not list is refused | `scope_denied` | `test_bypass.py` |
| A kind or a meaning must exist after alias resolution; a status must be in the kind's vocabulary, at create and at a status change | `unknown_kind`, `unknown_meaning`, `unknown_status` | `test_contract_d1.py`, `test_contract_d2.py` |
| A reversed alias swaps the endpoints | (none) | `test_contract_d1.py` |
| Endpoint kinds must match the meaning | `endpoint_kind_mismatch` | `test_contract_d1.py` |
| One active link per (from, meaning, to): a restatement adds evidence, not a link | (none) | `test_contract_d1.py` |
| A one-of-a-kind slot ends the prior link at the new link's start; a late link with an earlier start is stored already ended | (none) | `test_contract_d1.py` |
| A field update or status change older than the head's value goes to history only; a properties change that goes to history only merges onto the properties as they stood at its own world time | (none) | `test_contract_d1.py` |
| A reopen with no evidence takes no world time, so a later status change from older evidence still moves the head | (none) | `test_contract_d1.py` |
| A state property only on a record, one per record | `state_property_on_item` | `test_contract_d1.py` |
| A link of a state meaning, and any other claim about now (work with a state at a live status, a bounded period that began and has not ended, a record with a state property), needs a re-check by, on create and after every update | `missing_recheck` | `test_contract_d1.py` |
| No new consequence link to an ended item: ended by `end_item`, or at a terminal status | `ended_target` | `test_contract_d1.py` |
| `create_link` checks its endpoints under their write lock | (none) | `test_atomicity.py` |
| An item of a work kind (lifecycle class `work_with_state`) is created with a status | `unknown_status` (rule `status_required`, with the valid list) | `test_contract_d2.py` |
| Every claim names evidence; an evidence row names an episode read from its cited source and a locator that resolves; a forgotten source is not citable | `missing_provenance`, `unresolvable_locator` (rule `source_forgotten`) | `test_contract_d2.py` |
| A caller-supplied source type is refused; a caller's speaker kind never raises a row's source type; a scripted connector takes the source type of the connector type it imitates | `caller_source_type` | `test_contract_d2.py` |
| An evidence row cited by `evidence_id` must be active | `missing_provenance` (rule `evidence_not_found`) | `test_contract_d2.py` |
| A change entry or a provenance names a real evidence row, also when the cited row existed already; one message cited twice in one operation writes one row | (none) | `test_contract_d2.py` |
| Became true is a clock or "unknown"; an end may not precede a start; a terminal status at create stores its end time | `missing_became_true`, `end_before_start` | `test_contract_d2.py` |
| Required fields and required links of the kind hold on create and on every update, against the merged state; an update may not blank the title or a required field | `invalid_arguments` (rules `required_field`, `required_link`) | `test_contract_d2.py` |
| Only verify moves last confirmed | `invalid_arguments` (rule `verify_only`) | `test_contract_d2.py` |
| A record's claim never changes: an update may touch only its search terms, and a record refuses a status, an end and a reopen | `record_immutable` | `test_contract_d2.py` |
| A cited verdict must be a recorded judgment; a caller's score, weight, band or load-bearing flag is refused | `unrecorded_verdict`, `caller_score` | `test_contract_d2.py` |
| A contradicting row on a belief cites two recorded judgments of this belief (subject `belief:<id>`, a judge role, and the cited evidence row when the judgment names one), from two models, that both say it contradicts | `unrecorded_verdict` (rules `judgment_subject`, `judgment_role`, `judgment_evidence`), `second_judge_required`, `judges_disagree`, `judge_not_supports` (rule `judge_not_contradicts`) | `test_contract_d2.py` |
| A terminal done, also one that goes to history only, needs in its batch an observed confirming row (written or cited again), or a rolled-up row whose premises are active confirming rows of the item's parts (linked by a meaning that rolls up completion), with every part at a terminal role and every done part covered | `terminal_needs_confirmation` | `test_contract_d2.py` |
| A near-duplicate title (of an item's title or of an alias of the item) needs `distinct_from` with a reason | `near_duplicate`, `invalid_arguments` (rule `distinct_reason`) | `test_contract_d2.py` |
| An inferred or rolled-up row names its premises, each an active evidence row of this world | `inferred_evidence` (rules `premises_required`, `premise_not_found`) | `test_contract_d2.py` |
| `end_link` never ends a link of a primitive meaning | `unknown_meaning` (rule `primitive_meaning`) | `test_contract_d2.py` |
| `end_item` and `end_link` need a valid end class and a world end time; `reopen_item` needs a reason; `end_evidence` needs a valid end reason | `invalid_arguments` (rules `missing_field`, `enum`) | `test_contract_d2.py` |
| `attach_evidence` needs a target that exists, and is unique on message key, target and polarity | `not_found` (rule `target_not_found`) | `test_contract_d2.py` |
| An item, a link or an evidence row ends once | `invalid_arguments` (rule `already_ended`) | `test_contract_d2.py` |
| Only a terminal status carries `stopped_being_true`; a late terminal status that goes to history only keeps its end time as a history-only entry at its own world time | `invalid_arguments` (rule `stopped_needs_terminal`) | `test_contract_d2.py` |
| Only a link of the generic meaning keeps an original phrase | `invalid_arguments` (rule `original_phrase`) | `test_contract_d2.py` |
| A stale version is refused | `version_conflict` | `test_contract_d2.py` |
| One invalid operation rolls back the whole batch; a dependent of an operation refused at the end of the batch is named too; a failed commit raises and returns no receipt; an unknown commit outcome or an unreadable receipt is never a retryable "nothing written" | (none) | `test_atomicity.py` |
| The receipt is read back after the commit and matches the rows | (none) | `test_receipts.py` |
| A request commits once, and `saved` moves in the same transaction as the batch; a failed batch rolls the request back with it | (none) | `test_requests.py` |
| One key stores its words once, also when two enqueues race; other content under a used key is refused | `invalid_arguments` (rule `request_key_reused`) | `test_requests.py` |
| Only a row an enqueue wrote can be claimed; two claims give one winner; a lease brings a killed pass back; back-off, then surfaced | (none) | `test_requests.py` |
| A replay of a saved request writes nothing and reads its receipt, also when the read before the lock was stale, and never waits for the world lock | (none) | `test_requests.py` |
| Other content on a saved key is refused, a shape fault too, and the row stays saved | `invalid_arguments` (rule `request_key_reused`) | `test_requests.py` |
| A declined request gets one repair; a failed repair spends it, a shape fault too | `invalid_arguments` (rule `repair_spent`) | `test_requests.py` |
| Only the holder finishes or fails a claimed request; only a queue worker works the queue | `invalid_arguments` (rule `lease_lost`), `scope_denied` (rule `queue_worker`) | `test_requests.py` |
| "Saved" needs a receipt in hand; an unknown outcome or an unreadable receipt reads "saving" | (none) | `test_requests.py` |
| A batch takes the world lock first, so two near-duplicate creates cannot both pass; a batch that waited for it takes the next free sequence | `near_duplicate` | `test_requests.py`, `test_concurrency.py` |
| A request id holds no text, at every entry point | `ValueError` | `test_requests.py` |
| Every decline is logged by its operation, in ids and codes, with its near-duplicate candidates; an override is logged too | (none) | `test_declines.py` |
| A log failure never masks a decline, and a late log never changes a saved row | (none) | `test_declines.py` |
| A stale version is the only retryable decline | `version_conflict` | `test_declines.py` |
| A read-only caller is refused by every write of the queue before the store | `scope_denied` | `test_bypass.py` |
| A pass killed as a real process right after the enqueue, inside the open transaction, or after the commit before the receipt gives one effect; its receipt matches the rows, and one more replay changes nothing | (none) | `test_durability.py` |
| The same id and batch twice change nothing; a known link restated from a new source gets one evidence row and no second link; an overlapping message adds no row and no text | (none) | `test_idempotency.py` |
| Two agents on one item: the loser gets a stale version and goes back to the queue, and both changes stand; N replays of one request give one effect; the reads under a link's lock see an end that committed since; a stress loop never loses a link a receipt returned | `version_conflict` | `test_concurrency.py` |
| One liveness function for every read and every guard: ended (an end class, a terminal role, a stop clock), then overtaken by an active "corrected by" or "superseded by" successor, then the scheduled class by its evidence, then a future start; the successors ride on every state | (none) | `test_current.py` |
| A bounded period, or a record that a plan scheduled, reads planned or reported until a confirmation, and a period also needs its start; no date moves it; an inferred row never confirms | (none) | `test_current.py` |
| The earning bars count distinct episodes among the active supporting rows of all the targets together | (none) | `test_evidence_counts.py` |
| A kind, a meaning or a property name resolves once, in D8's order; a name wins over an alias; a leading "is" or "are" falls away only when nothing matched | (none) | `test_resolver.py` |
| An alias of a retired or split word reaches nothing | `unknown_kind`, `unknown_meaning` | `test_resolver.py` |
| A phrase with two usable targets is never picked | `unknown_kind`, `unknown_meaning`, `invalid_arguments` (rule `ambiguous_phrase`) | `test_resolver.py` |
| A phrase that normalises to nothing is refused | `invalid_arguments` (rule `empty_phrase`) | `test_resolver.py` |
| A text of only whitespace and invisible format characters reads blank: a title, a reason, a `distinct_from` reason or a required field is refused | `invalid_arguments` (rules `required_field`, `type`, `distinct_reason`) | `test_contract_d2.py` |
| A property name resolves per kind: an alias of another kind's property is no match | `invalid_arguments` (rule `unknown_property`) | `test_resolver.py` |
| Two spellings of one property in one create or update with different values are refused, so no value is lost | `invalid_arguments` (rule `property_named_twice`) | `test_resolver.py` |
| An unmapped or ambiguous link phrase rides on the generic meaning with its phrase kept, never on a meaning that carries consequences | (none) | `test_resolver.py` |
| The candidate band is every item of the kind at 0.3 or more, best first; an ended item is flagged and kept; a distinct pair is left out | (none) | `test_resolver.py`, `test_neardup_labelled.py` |
| A correction leaves the record byte-identical and moves the head in the same commit; with no head update it states why; the successor may be minted in the batch; a successor link never starts after now; an ended record is not corrected | `invalid_arguments` (rules `head_update_required`, `successor_in_future`), `ended_target` | `test_contract_d3.py` |
| A refutation ends an item or a link as never true, with evidence at the claim's rank unless the person says it; a successor link is not refuted by hand | `invalid_arguments` (rule `refute_rank`), `unknown_meaning` (rule `primitive_meaning`) | `test_contract_d3.py` |
| A supersession ends the old item with a stop clock and keeps the lineage | `invalid_arguments` (rule `already_ended`) | `test_contract_d3.py` |
| A merge re-points the links with the same clocks and turns names into aliases; a merge then an undo restores both items, their links and their aliases; one lifecycle class, links fitting the survivor | `invalid_arguments` (rule `merge_lifecycle`), `endpoint_kind_mismatch` (rule `merge_link_fit`) | `test_contract_d3.py` |
| An undo of an automatic change takes one call and keeps both change sets; it conflicts when the row moved since; a late value's undo restores the as-of read; an undone creation is refuted; only the person or the writer undoes; the starter set is not undone | `version_conflict` (rule `undo_conflict`), `invalid_arguments` (rules `already_undone`, `not_undoable`), `scope_denied` | `test_contract_d3.py` |
| A spent record leaves `current()`, keeps its evidence and its history, and reads true on the world axis; only the consolidation agent retires, and only a record | `invalid_arguments` (rule `not_a_record`), `scope_denied` | `test_contract_d3.py` |
| A clock entry holds the value id of its words | (none) | `test_contract_d3.py` |
| Labelled pairs: reworded titles at 0.8, 0.86 and 1.0 are refused, pairs at 0.72 and 0.3 come back as candidates, and revision N after N-1 passes only with `distinct_from` and is logged with `override_used`; `distinct_from` clears only the item it names; a title that differs only by an invisible format character meets the gate | `near_duplicate` | `test_neardup_labelled.py` |
