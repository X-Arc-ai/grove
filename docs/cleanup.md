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
- The question writer requires recorded unsuccessful legwork, screens question
  prose, coalesces open questions by subject, and records only bound options.
  It closes an answer only after a saved interactive receipt covers every
  subject. AI-host answers require verbatim metadata. The same close request is
  idempotent. A withdrawal requires a screened reason. Ordering puts risk first,
  decisions next and oldest questions first within a class.
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
5. Integrate the question queue with the real interactive answer writer,
   complete source-search legwork, check-sources jobs and permission handling.
   A saved receipt alone does not establish semantic answer quality. The current
   helper does not apply an answer itself and does not implement UI endpoints.
6. Persist and execute third-consecutive-deferral escalation. The current helper
   returns a question or retirement proposal only. Implement partial-resolution
   and three-run oscillation handling, D10 link/alias integration and expiry work.
7. Decide the no-source deferral operation with the D4 owner. The published
   writer requires a nonempty piece list; the current deferred drill uses a
   recorded `not_about_claim` judgment. No empty-source success is claimed.
8. Run the full labelled D11 quality drills, including false questions, missed
   questions, false dispositions, omission rate, paraphrase contamination,
   partial resolution, oscillation and Area coverage. Run live providers and
   end-to-end acceptance separately. These are not proved by synthetic smoke.

The prompt states the full behavior goal, including still-open orchestration.
The runtime refusal prevents those instructions from implying an active feature.
