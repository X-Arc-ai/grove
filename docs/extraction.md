# Bounded extraction source component

`grove.extraction` provides a nonactivated D10 extraction component. It has no production
dispatcher, provider registration, source persistence, migration, or watermark writer.
Its publication does not complete D10 or authorize typed-source admission. The existing
runner remains responsible for run ownership, atomic commit, versions, completion, and requeue.

## Public contract

`SourceMessage(key, locator, text, source_time, speaker_kind, modality="text",
speaker_item=None, original_author=None, timezone=None, eligibility="eligible")` carries
one exact retained message. `source_time` requires an explicit offset. A relative time also
requires a known IANA `timezone`; the component never guesses one. The caller supplies
speaker facts from authorized metadata. A forwarded message requires its original author.

`SourceBatch(world_id, run_id, episode_id, source_id, connector_type, messages,
source_version, episode_version, screened=False, provenance_mode="legacy")` binds a tuple of
messages to the caller's snapshot. It accepts at most 128 messages, 16,384 characters per
message, and 65,536 UTF-8 bytes in all message text. IDs are limited to 1,024 characters.
Duplicate locators, duplicate keys, reverse time order, unknown connectors, held or unknown
eligibility, and absent screening all refuse before the first model call.

These Python records are not proof of admission. Construct them only from the authorized
retained source, its Episode, and its Run. Never deserialize untrusted request JSON into them.
Every provenance mode other than `legacy` refuses, including a claimed `typed_admitted`
mode. Typed facts and draft projections from A2 do not release this boundary.

`RegistrySnapshot(kinds, meanings=(), properties={}, versions={}, scheduled_fields={})`
contains bounded authorized registry values. Kinds map to allowed statuses. Properties map
each kind to its declared names. Versions map existing targets to their snapshot versions.
Scheduled fields map a kind and role to a declared property name.

`extract_batch(layer, batch, registry, interactive=False)` uses only the supplied ModelLayer.
The maintenance path calls Glimmer through `chat(job="triage", effort="low")`. Valid triage
must account for every input locator. An empty or truncated response is never an empty
result; an empty response gets at most one component retry. Additional redaction candidates
return `unsupported` before Spark, so a later owner must rescreen them in code.

The interactive path accepts only `ai_host` and `person_in_views` sources and skips triage.
The caller must obtain request time from the authoritative Request, never its import time.
Spark runs through `respond(job="extraction", effort="minimal")`. One classification
disagreement triggers one `low` effort adjudication. Registry and source values appear only
in the user data packet, never in the stable instruction prefix. No automatic Spark fallback
changes the data route when Glimmer is unavailable.

The result is an `ExtractionPlan` with `status`, `observations`, `operations`, `call_ids`,
`unsupported`, `document`, `source_binding`, and `disagreements`. Status is `ready`,
`held_nothing`, or `unsupported`. A `ready` result provides `to_batch()` with fresh copies
of the writer's `{evidence, ops}` shape. `source_binding` is the tuple
`(world_id, run_id, episode_id, source_id, episode_version, source_version)`.
The integration must revalidate these identities and versions inside the writer transaction.
This component neither commits nor records durable coverage, including `held_nothing`.

## Schema and writer mapping

The constrained schema carries the `grove.extract.v1` identifier. This implementation is an
explicit bounded profile, not the complete D10 schema. Operation field names and required
fields derive from `writer.ops.OPS`; adapters map the flat references and evidence clocks to
the current writer contract. The schema contains no source-type field. Its statement classes
include `artifact`, which preserves the accepted writer's artifact path without asserting
that an artifact proves anything beyond its own contents.

Supported operations are `create_item`, `update_item`, `change_status`, `end_item`,
`create_link`, `end_link`, `attach_evidence`, `refute`, and `supersede`. The component checks
the update caller boundary and runs the writer's shape validator on every generated batch.
Target IDs must appear in the authorized snapshot or an earlier declared temporary ID.
Code supplies existing target versions. Multiple attachment references produce separate
writer operations; no reference or polarity disappears.

Create fields map only to declared properties. Update fields currently support `title`,
`summary`, and `body`. A title or summary update requires fresh search terms. Search terms
remain typed objects in `plan.document`, while the current writer receives their text list.
Term types, languages, relation-to-standing metadata, and model time proposals stay in that
in-memory document; no durable storage for these extra proposal fields is claimed.

Evidence observations preserve exact source keys, locators, times, speaker facts and modality.
`msg` resolves to that message's own time. Relative words use the accepted `timewords` module.
Claimed dates and claim-time words must occur in the source text. Proposed rechecks may use
relative words absent from the source, because they propose operational checks rather than
facts. Missing time remains explicitly unknown. Scheduled times map only to declared
scheduled properties of a created item. Unsupported scheduled or recheck mappings refuse.

The component refuses inferred premises, `correct`, `verify`, `reopen_item`, durable holds,
belief mints, ontology mints, and any other unsupported operation. Nonempty mentions,
contradictions, questions, belief candidates, ontology proposals, or declines block the entire
writer proposal with an explicit unsupported result. These branches accept only locator and
reason placeholders in this profile. They are not silently discarded or recorded as complete.
The writer retains source derivation, capability, confirmation, replay and tenant checks.

## Optional pure and read-only helpers

`resolve_name` uses an authorized WorldQuery, checks exact aliases first, then calls the
existing writer lexical resolver. Distinct pairs exclude both alias and lexical matches.
Shared neighbors and message-time overlap can narrow ambiguity; ties stay ambiguous.
Read results are bounded to 256 items and 1,024 aliases. Existing threshold defaults remain
uncalibrated. Timed fuzzy aliases remain ambiguous because the shared lexical resolver does
not apply alias validity intervals. The helper creates no alias, item, question or durable hold.
Extraction does not automatically call it or claim complete graph grounding.

`estimate_backfill` computes a pure estimate with a separate two-pass reservation.
`extraction_window` returns the next bounded per-source lookahead from a committed count.
Neither helper creates jobs, maintains an ordered committer, resumes work, changes coverage,
or provides the complete backfill runtime.

## Checks and remaining work

The focused offline tests use invented text, supplied model fakes and read fakes. They test
schema generation, complete triage, exact metadata, unsupported paths, time binding, artifacts,
search terms, attachment mapping, snapshot versions, lexical reuse, distinct exclusions and
bounded scheduling. They make no provider or database calls.

Full D10 still requires guarded runtime dispatch, durable holds and unresolved mentions,
grounding integration, supported ontology and belief handoff, one-repair commit/requeue
semantics, an ordered backfill committer, exact replay and kill evidence, and the labelled
week's precision, recall, question rate, Spark share and latency measurements. Typed-source
persistence, admission, erasure and restoration remain with A2 and D14. No live model,
provider, deployment, runtime, or full D10 acceptance follows from this source component.

## Inactive pure execution protocol

The additional execution modules publish `grove.learner.legacy.v1` as
`PURE_EXECUTION_PROTOCOL_ONLY`. They produce validated **untrusted data** and pure transition
plans. They do not provide principal authority, source admission, a database adapter, a model
dispatcher, or a production route. `runtime.require_runtime()` always raises
`ExtractionRefused("learner_authority_unavailable")`. No flag or supplied callback enables it.
The existing `SourceBatch`, `RegistrySnapshot`, `ExtractionPlan`, and extraction entry points
remain unchanged.

`execution_types` defines frozen records with tuple children. `decode_binding()` accepts UTF-8
bytes or JSON text and returns an `ExecutionBinding`; `encode_binding()` returns canonical
UTF-8 JSON. The codec rejects duplicate, unknown, and missing fields, invalid Unicode,
nonfinite or fractional numbers, boolean counters, negative counters, and integer overflow.
The binding limit is 1 MiB, the proposal limit is 512 KiB, nesting is limited to 16 levels,
and counters use nonnegative signed 64-bit integers. Individual retained JSON/request/output
strings have a stricter 262,144-character bound. Direct Python records also pass aggregate
limits before hashing. Binding, proposal, state, and disposition hashes use separate domains;
identity fields are never joined with an unescaped separator.

The binding includes protocol/source/prompt/schema versions; untrusted principal, grant,
origin, policy and privacy references; Run and Request versions; claim and budget fields;
the producer; the complete retained Source and speaker JSON; the exact Episode; every
retained message descriptor; selected keys; context versions and membership; and the fixed
model/writer profiles. Embedded Source text remains private derived material. This component
does not persist it or declare an erasure policy for a future durable store.

Call `check_legacy_profile()` after decoding to check semantic consistency. A successful
decode alone proves only structural validity. The exact producer tuples are:

| Connector type | Provider | Parser | Batch and cut rule |
|---|---|---|---|
| `past_ai_conversations` | `chatgpt` | `chatgpt.v1` | `session_delta.v1` |
| `past_ai_conversations` | `claude_ai` | `claude_ai.v1` | `session_delta.v1` |
| `past_ai_conversations` | `claude_code` | `claude_code.v1` | `session_delta.v1` |
| `past_ai_conversations` | `codex` | `codex.v1` | `session_delta.v1` |

The first profile requires one complete Source and one complete Episode, at most 128 retained
messages, 16,384 characters per message, and 65,536 UTF-8 text bytes. It validates the complete
retained JSON through the existing typed-source guard before selecting any message.
A typed marker outside the proposed selection still refuses the complete work. Raw text,
pointers, multipart Episodes, multiple Episodes per Source, media, unsupported producers,
and oversized work cannot become a successful prefix.

Every descriptor must match its retained key, `key:<key>` locator, text digest, original
source timestamp including offset, speaker, sender, modality, and original author.
Keys use the writer's minimum of four characters and must fit the full locator limit.
Run IDs use the model layer's stricter 80-character ASCII limit. Blank text stays in the
inventory but does not enter the model selection. Neither Episode bounds, read time,
display names, upload ownership, nor a guessed timezone can replace message provenance.
The profile supplies no person timezone.

Context has six complete membership categories: registry, target, alias, neighbor, evidence,
and history. Each category permits at most 256 references with corresponding versions.
`check_dependencies()` compares source content, source metadata, producer and Episode facts,
principal/policy/privacy references, protocol identity, context versions, and membership.
Operational `read_generation`, capture identity, and protocol versions do not replace those
semantic dependencies. A new alias or candidate requires another grounding pass.
Saved replay omits old context/target checks because the accepted effect can change them;
it still validates the complete current source/profile and privacy references.

The fixed model stages are triage on Modal/Glimmer at low effort, extraction on Meta/Spark
at minimal effort, and optional classification adjudication on Meta/Spark at low effort.
There is no fallback or backfill route. `Invocation` binds a normalized request envelope,
stage, Run, claim epoch, ordinal, route/model/job/effort, actual result bytes, ModelCall ID,
and request/output digests. The envelope has `binding_digest`, `stage`, and `payload` fields.
Its payload is untrusted; the future model adapter must construct and admit the actual
provider request and verify each ModelCall association. This protocol does not query a
ModelCall store or assert that a named call occurred.

`decode_proposal()` creates immutable operation/evidence descriptors, dispositions, and
invocation references. An operation descriptor identifies the operation, its evidence, and
its payload digest; it is **not an executable writer batch**. The future writer adapter must
load and validate the complete matching payload with all normal writer checks. The pure
component validates descriptor membership and the nine-operation profile without replacing
the existing writer's shape, source-type, capability, or transaction checks.

`plan_completion()` requires exactly one primary disposition per retained message, in order.
Only code-proved `ignored_blank`, completely bound `reviewed_empty`, and fully mapped
`extracted` outcomes can complete. Every triage `(locator, statement_class)` pair needs a
final class outcome and operation/evidence mapping. A changed class requires a bound
adjudication result. Held input/facts, redaction, unresolved work, rejected work, and revoked
work refuse completion. An unknown later call cannot reuse an older successful result.
Counts derive from the complete ledger, not model totals.

`advance(state, event)` returns a new immutable state with a predecessor digest and incremented
operational versions. Events carry the expected revision, claim epoch, operational time, and
current untrusted snapshot. Send and commit plans require a current active Run/claim and
unchanged dependencies. Operational versions advance separately from the immutable input
binding. Consumers must supply the expected current versions after each planned transition.
This is optimistic state comparison only; it does not acquire a lock or establish authority.

The reducer supports claim, invocation intent/send/result, failure/unknown outcome, proposal,
decline, repair, stale/rebind, recovery, budget wait, block, revoke, and completion. It permits
five total transport attempts, two triage calls, two adjudication calls, one grounding retry,
and one reserved writer repair. A repair reserves its slot before the next call. The same
declined proposal returns the unchanged state. Requeue preserves spent budgets. A grounding
retry archives the old calls and requires a newly checked complete binding; it does not
renew the transport or repair budget. Failed transport delays are 30, 60, 120, and 240 seconds,
subject to the shared deadline. Programming errors and malformed protocol data refuse.

`plan_replay()` returns the matching immutable completion only after current source/privacy
and live-claim checks. A replacement Run needs a newer recovery claim. A late failure cannot
reverse a completion. `plan_counter_delta()` computes once-per-Episode empty/extracted
contributions, preserves historical extraction after a later empty revision, and advances
an extraction cursor only over contiguous completed entries. It never changes the import
watermark. Actual creation counts, seven-day metrics, unknown-date coverage, counter writes,
and cursor persistence require the later durable adapter.

The focused tests use synthetic records only. Simulated crashes prove reducer transitions,
not database rollback, physical process recovery, or exactly-once provider execution.
Publication of this component does not activate runtime D10. The next source unit must use
the accepted shared writer transaction seam, obtain its own schema reservation, and preserve
the D14 data/erasure contract. Real admission, each-send lifetime checks, durable dispositions,
atomic graph/completion/counter/outbox writes, provider integration, and full labelled-week
acceptance remain open.

## Inactive durable Learner source

The `learner_execution_v1` protocol stores one protected Request per Work, an append-only
execution journal, bounded call records, complete message dispositions, one completion,
ordered episode progress, and durable downstream intents. Migration
`0017_learner_execution.cypher` declares these seven homes. Publication of this source does
not apply the migration or authorize production persistence.

The internal functions in `writer/learner_execution.py` require an existing `WorldTx`.
The ordinary `PreparedBatch` executor supplies all graph, source, caller, Curator and Beliefs
checks. A proved blank or completely reviewed empty batch uses an explicit no-graph branch.
Completion, actual effect identities, Request state, progress and intents share one native
transaction. Stable event IDs reject changed retries. Current source, actual RunRecord,
claim epoch and owner checks also precede replay. A failed predecessor blocks later progress.
Import watermarks and import counts remain separate from extraction accounting.

`RunWitness` separates the actual RunRecord row version from the pure protocol version.
An ended unrelated Run cannot authorize recovery. A new claimant cannot renew the original
deadline, token limit, five-call bound, single repair, or single re-ground allowance.
Full executable operations and observations must match their descriptors and deterministic
translation of the stored provider output. A supplied ModelCall ID is insufficient authority.

Private JSON values use explicit typed references in HistoryValue. The bounds are:

| Value or structure | Maximum |
|---|---:|
| Binding, initial input, event | 1 MiB each |
| Proposal, executable batch, stored call request or result | 512 KiB each |
| State, triage, completion, handoff | 256 KiB each |
| Call binding, disposition | 16 KiB each |
| Individual transport or normalized text | 256 KiB |
| Complete retained value closure per Work | 16 MiB |
| Execution revisions, binding revisions, calls | 64, 2, 5 |
| JSON depth, nodes, collection members | 16, 16,384, 2,048 |
| Ordered progress window, downstream targets | 256 each |

Token preflight runs before JSON allocation. Closed wrappers reject unknown fields, duplicate
keys, non-finite numbers, floats, invalid Unicode, mixed reference kinds and cycles. Snapshot
validation reconstructs the journal and checks complete private closure, protected identity,
completion effects and downstream references before load and after transformed ledger replay.
A missing or erased dependency causes refusal. This validation does not supply a Forget
selector or prove that the current physical erasure compiler reaches these new families.

Production authority remains unconditionally unavailable. No provider constructor, API,
runner mode, registry entry or outbox drainer activates this protocol. The missing owners must
supply reciprocal per-send Models association and current D14 admission, worker lifetime,
transaction checks and final release. The inactive local authority publisher does not supply
those owners. Held, unresolved, redacted, rejected, incomplete, typed and media work cannot
become successful v1 completion. Full D10, full D14, Request7 and owner demo acceptance remain open.

The focused regression files distinguish pure codec checks and in-memory transaction controls
from opt-in native Neo4j and actual subprocess cuts. The native/process module requires
`GROVE_LEARNER_NATIVE_TESTS=1` under separate test authorization. Its fixtures use synthetic
owner and model responses. Native atomicity, process recovery, physical erasure and live owner
acceptance remain **NOT RUN** in this source session; synthetic controls do not prove them.
