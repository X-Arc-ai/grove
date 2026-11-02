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
)
```

| Function | What it does |
|---|---|
| `apply_batch(driver, world_id, caller, batch, *, request_id=None, run_id=None) -> Receipt` | Checks and commits one batch. Raises `BatchDeclined` (nothing written), `WriteFailed` (nothing written, retry may help), `WriteOutcomeUnknown` (the connection dropped during the commit, so the batch may be saved: never retry blind; read the change set back by its `change_set_id` or `commit_id`) or `ReceiptUnavailable` (the batch committed but its receipt could not be read: never apply it again). The last two carry `commit_id`, `change_set_id` and `request_id`, and `retryable` is false. Returns a `Receipt` only after the commit, read back in a fresh read transaction. |
| `render_receipt(driver, world_id, receipt) -> str` | The receipt in the fixed grammar of D2. Titles, meaning names and values are read fresh, so a stored receipt holds only ids and codes. |
| `record_connector(driver, world_id, type, *, settings=None, staleness_limit=None) -> str` | One `:Connector` row. Returns its id. |
| `write_screened_source(tx, screened, *, source_id, connector_id, parser_version) -> dict` | One `:Source` and its `:ScreeningReceipt` in the caller's transaction, from a `ScreenedSource`. Text from a connector reaches `sources` only here or in `add_screened_source`. |
| `add_screened_source(writer, screened, *, connector_id, parser_version, source_id=None) -> dict` | The same in a transaction of its own. With no `source_id` it mints one. |
| `record_source(driver, world_id, *, connector_id, external_locator, content=None, content_pointer=None, parser_version, untrusted=False) -> str` | One `:Source` row with the text as given, or a pointer. Returns its id. For the writer's own tests: a test fails when code outside this package reaches for it. |
| `record_episode(driver, world_id, *, connector_id, source_ids, first_message_key, last_message_key, start_time, end_time, cut_rule, run_id=None) -> str` | One `:Episode` row, keyed on the connector and the first message key. A later cut of the same window keeps the id and extends the range. Returns its id. |

The package also exports `ReceiptLine` (one line of a `Receipt`), `DeclinedReceipt` (the type of
`BatchDeclined.receipt`), `InvalidRecord` (raised by the three `record_` functions, nothing written)
and `HTTP_STATUS`. The three `record_` functions are not B.2 operations and take no `Caller`: a
layer that exposes them to a token checks its scope first.

Read helpers on the read side, for tests and for later lanes: `grove.store.asof.item_as_of(query,
item_id, world_time)` replays an item's change entries to a world time (the head rule of D1),
and `grove.writer.receipts.short_id(id)` gives the short form a receipt prints.

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

`accounts.py` is not a B.2 operation: it holds sign-in's writes (S20, S40), users, sessions, email
codes and the person's own item. Only `server/grove/api/auth.py` calls it. `docs/signin.md` has the
flows.

The other B.2 operations belong to later lanes: correct, refute, supersede, merge,
`retire_record` and undo (`writer-b`); verify and the confirmation at create (`truth-clock`);
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
`depends_on_declined`. `BatchDeclined.receipt` renders as

```
declined (batch of 3, nothing written)
  ! link 9a1e0c33 -[lives in]-> 31cdee01: endpoint_kind_mismatch (from_kind)
```

## Receipts

A `Receipt` holds `state` (`saved`), `commit_id`, `change_set_id`, `request_id` and `lines`.
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
| Only a terminal status carries `stopped_being_true`, and a change that goes to history only cannot carry it (the writer has no place to store it, so it refuses and never drops it) | `invalid_arguments` (rules `stopped_needs_terminal`, `stopped_on_history_only`) | `test_contract_d2.py` |
| Only a link of the generic meaning keeps an original phrase | `invalid_arguments` (rule `original_phrase`) | `test_contract_d2.py` |
| A stale version is refused | `version_conflict` | `test_contract_d2.py` |
| One invalid operation rolls back the whole batch; a dependent of an operation refused at the end of the batch is named too; a failed commit raises and returns no receipt; an unknown commit outcome or an unreadable receipt is never a retryable "nothing written" | (none) | `test_atomicity.py` |
| The receipt is read back after the commit and matches the rows | (none) | `test_receipts.py` |
