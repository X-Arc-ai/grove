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
