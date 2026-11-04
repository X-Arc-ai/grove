# Curator source components

This is an inactive D11 component, not full Curator runtime acceptance.
Authenticated Curator brain routes and direct Curator `pass_input` refuse with
`curator_authority_unavailable` (HTTP 503 at the API boundary). No configuration
flag enables them. Other agents retain their existing dispatch paths.

## Implemented source

- `grove.cleanup.verification.prepare_lane` uses the published D4 batch selector,
  including its oldest-unconfirmed reserve.
- `grove.cleanup.contracts.to_batch` accepts the bounded `grove.cleanup.v1` core
  packet. It translates four dispositions and supported writer operations into
  the existing D4/D3 writer contract. The caller must supply an idempotency key.
  The writer still checks provenance, capability, judgments, bands, atomic
  changes and truth clocks. The packet cannot supply scores or verdicts.
- The contamination check refuses identical normalized generated item prose
  across three targets. It is not a semantic paraphrase detector.
- Public page fetch checks an explicit host allowlist on every redirect, accepts
  HTTPS on port 443 only, rejects private DNS answers, and pins the checked IP
  with the original Host and TLS SNI. It sends no cookies or credentials, uses no
  environment proxy, accepts only uncompressed UTF-8 text/HTML, strips active HTML
  elements, and bounds responses to 64 KiB, three redirects and ten seconds.
  Two worker slots bound outstanding network work. An owner lease remains held
  in the actual worker after a caller timeout; late DNS completion cannot open
  another socket. A hung resolver can retain a slot until it returns.
- Fetch records a screened Source, ScreeningReceipt and dated Episode before
  any judge call. The date means page observation, not the event described by
  the page. Judges reload and validate the retained identity and observation.
  New A2 drafts, arbitrary excerpts and legacy undated material are not admitted.
- The first judge and the configured second judge use the model-call layer and
  separate fixed prompts. Neither receives the Curator's proposed disposition
  or the other judge's output. The ordinary borderline path uses the ordinary
  second-opinion budget; load-bearing checks retain the published D4 path.
  The fixed D4 private prompt/recorder helpers are a current source dependency.
- Legacy question creation remains readable but has no inferred action binding.
  A touched-item receipt cannot close it. The typed lifecycle below owns bound
  questions, exact answer application, partial work, and replay.
- Ambiguity narrowing intersects owner-supplied participant, co-mention and
  time context. It never picks by candidate order or lexical score. Unresolved
  mentions retain candidates, source/episode/locator identity, context rules,
  waiting operation IDs and a 30-day expiry. Waiting links remain unapplied.

All new snapshot writes use the existing WorldWriter lock and generation
protocol. They refuse designated demo worlds, including partial designations.
There is no production implementation of `Access`. Each internal integration
must supply a real owner authorization/forget lease, not a boolean switch.
Tests supply an explicitly synthetic authority and synthetic provider replies.

## Core packet

The exact top-level keys are `schema`, `run`, `dispositions`, `operations` and
`evidence`. `run` must identify the authorized run, `work: lane_batch` and
`area_id: null`. One to twelve dispositions target offered claims. Combined
operations are bounded to 100 and the serialized packet to 256 KiB. Existing
writer operation fields and evidence shapes apply; unsupported fields refuse.
Question and ambiguity writes are separate internal interfaces, not accepted
top-level packet fields or public routes.

## Remaining D11 acceptance

1. Complete D7 capture-filtered reads and D14 admission, cancellation and release
   authority across fetch, source retention, model dispatch and writer commit.
   Integrate a production Access provider and runtime dispatcher only then.
2. Complete dated typed persistence/admission with the A2 and B1 owners. Add
   Calendar, chat, note, file/release and git source checks. Do not substitute a
   legacy fallback for unresolved typed or undated material.
3. Integrate the whole D11 result schema: questions, contradictions, duplicate
   handling, ontology findings, overdue holds, mentions and audit findings.
   Bind every operation and evidence reference to an authorized capture.
4. Complete Area discovery, disjoint partitions, budgets, dirty queues, audit
   coverage and within-Area merges. Persist versioned record-axis Area history;
   current definitions must never stand in for historical definitions.
5. Integrate the inactive question transactions with real current-principal,
   source-search, permission, API and UI services. Add durable D10 consumers for
   transferred waiting operations and aliases. A transfer is pending work.
6. Consume pending third-deferral destinations through real question or retirement
   proposal services. Complete the orchestration that records an oscillation
   finding after the scored path refuses repeated resolution.
7. Integrate the no-source command into an authorized Curator dispatcher. The
   ordinary untagged verify operation still requires evidence pieces.
8. Run the full labelled D11 quality drills, including false questions, missed
   questions, false dispositions, omission rate, paraphrase contamination,
   partial resolution, oscillation and Area coverage. Run live providers and
   end-to-end acceptance separately. These are not proved by synthetic smoke.

The prompt states the full behavior goal, including still-open orchestration.
The runtime refusal prevents those instructions from implying an active feature.


## Inactive question lifecycle

Migration `0016_question_lifecycle.cypher` registers seven snapshot tables:
QuestionBinding, QuestionWork, QuestionApplication, QuestionEvent, CuratorLegwork,
CuratorEscalation and CuratorRunOrder. It adds bounded structural fields to Request
and Verification. It performs no legacy binding, closure or backfill. Applying
this migration does not activate the component.

`cleanup.question_lifecycle`, `cleanup.legwork` and `cleanup.question_intents`
provide closed immutable inputs and bounded JSON codecs. IDs, versions, option
plans and exact source/message bindings are separate from text. Unknown fields,
duplicate JSON keys, invalid versions and excessive inputs refuse.

`writer.question_lifecycle.create_or_join_question` uses a stable topic and the
world lock. Compatible occurrences retain every obligation. Changed plans or
versions require an explicit future rebind service; automatic overwrites refuse.
Identity caps are three per day and ten per labelled backfill week. Excess work
remains pending and the result does not claim a created question.

An answer must cite an existing screened Source, exact retained message, Source
and Episode versions, principal, binding revision and stored plan. Accepted text
must match the retained message exactly. This component does not retain new answer
sources. The source owner and complete authority provider remain prerequisites.
Host answers require exact retained verbatim message metadata. A host never uses
the view-only D4 no-judge shortcut.

`submit_answer` creates an immutable `question_answer_v1` Request. Generic batch,
request, queue completion and replay paths refuse protected requests. `apply_answer`
executes a bound plan, writes its exact application, updates obligations and changes
the question in one WorldWriter transaction. Cancellation takes the same lock.
An uncertain commit needs reconciliation with the same request key. A successful
application remains committed if the final release check refuses disclosure.

The `confirm` plan uses ordinary D4 checks. A claim with a horizon requires an
explicit stored batch that supplies the new date. `apply_batch` plans reference
bounded HistoryValue payloads with exact answer evidence and bound existing
targets; unsupported unbound operations refuse. D9/D12 protections still apply.
No API accepts arbitrary client operations or caller-selected authority.

`return_to_sources` and `resolution_transfer` apply durable answer transfers.
They may close a question while related work remains pending. They never confirm
a claim or mark a D10 waiting operation as applied. Partial answers keep the
remaining obligations open. A withdrawal records its reason and a complete
cancel/transfer disposition for each outstanding obligation.

`writer.api.prepare_batch` and `execute_prepared_in_tx` are the shared internal
transaction seam. They retain ordinary shape/caller checks, both source checks,
all operation finalization, and WorldWriter summary/generation behavior. The
caller owns protected request validation and completion. There is no generic
before-commit callback.

`writer.verify.record_no_source_deferral` records a checked unsuccessful attempt,
zero evidence weight and absent ratio. It changes operational recheck fields
only and preserves truth clocks, confirmation, status, evidence and judgments.
Committed verification history supplies the streak; CuratorRunOrder supplies
the sole accepted-run order. The third deferral stores one pending destination.
A pending destination is not a created question or retirement proposal.

QuestionEvent snapshots preserve structural before/after states. Restore checks
these states and their application/request identities separately from the existing
D3 fingerprint. Answer prose stays in Source; generated prose and operation
payloads stay in HistoryValue. D14 must register every new reference and history
home before runtime activation. The pure authority codec does not perform erasure.

Immediate component tests use synthetic in-memory transactions and controlled
graph executors. They do not prove native Neo4j commit durability, process-kill
recovery, live current-principal authority, or public API/UI acceptance.
