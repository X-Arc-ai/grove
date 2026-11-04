# Beliefs and the Connector

Belief state belongs to the writer. The Connector reads a settled world, obtains recorded
judgments, and proposes one atomic batch. The runner selects Spark at high effort through the
`consolidation_pass` job. No state, source rank, score, or model verdict in a proposal is trusted.

## The input packet

`brain pass` returns `grove.consolidate.v1` for a Connector token. Code binds `run.run_id` to the
live pass and computes `run.triggers`. The triggers cover open contradictions, ten changed
items, changed supports, subject drift, and due externally testable beliefs. An idle poll can
queue a due re-test. Requests have priority over Curator jobs, which have priority over Connector
jobs. The existing world lock, budget ceiling, watchdog, and preemption remain in force.

The last successful Connector pass is the history cursor. A failed pass does not consume
revisions. A queued job keeps its existing retry delay. Once a job exhausts its attempts, an
unchanged input cannot create a fresh job on every poll; new input can trigger a new job.

The packet starts with contradictions, then changed supports, beliefs, complete histories,
evidence, and source blocks. Incoming dependents of changed items are included even when a
meaning's consequence flag is false, so the Connector can report a missed consequence check.
Stored prose appears only inside escaped `untrusted_data` blocks. Control fields carry IDs,
fixed codes, counts, and dates. Each selected entity's complete history is returned. A packet
above 2 MiB refuses as a whole instead of silently dropping history.

## One guarded envelope

The agent submits the packet's `batch_form.batch` through `brain propose`. Its keys are:

| Key | Meaning |
|---|---|
| `schema` | Exactly `grove.consolidate.v1`. |
| `run` | The exact code-owned `run_id` and `triggers` echo. |
| `evidence` | Writer observations with retained provenance; never source text or a derived source type. |
| `resolutions` | `resolve_contradiction` operations for every currently open contradiction. |
| `belief_ops` | Belief transitions. Short names such as `mint` map to writer names such as `mint_belief`. |
| `dispositions` | `verify` and `retest_belief` payloads with recorded judgment IDs. |
| `operations` | The supported item, link, evidence, and correction operations from the writer catalogue. |
| `contradictions` | New `open_contradiction` operations. |
| `support_changes` | `resolve_support_change` operations with their exact cause and recorded judgments. |
| `ontology_ops`, `walk`, `questions`, `declines` | Empty in this core schema. Non-empty sections refuse explicitly. |

`operation_catalogue` comes from the writer's `OPS` definitions and lists the exact required and
optional fields. The adapter rejects unknown fields and operations. The writer checks the
translated batch again. A raw Connector writer batch cannot bypass the queue.

The first operation is a code-generated `guard_consolidation`. Under the writer lock, it checks
the active run and the complete contradiction and support queues. A concurrent change between
packet read and commit therefore refuses the batch. The adapter orders each conflict's cited
operations before its resolution, and all conflict resolutions before later maintenance work.
Every resolution records a reason. A conflict open for seven days must become a question with
both readings. A `to_question` resolution cannot carry an operation that changes the standing
claim. The question is created by the writer in the same transaction.

The pass cannot end `ok` with an open contradiction or pending support change. It becomes a
failed pass and its job follows the normal retry rule. The Connector cannot hide unresolved
work in its final prose.

## Evidence, source reads, and re-tests

The Connector cites the retained evidence and the judgments returned by the code-controlled
read/judge tools. It must not manufacture a judgment ID, source timestamp, source type, or
confidence. A re-test supplies its observation to `evidence` and its writer payload to
`dispositions`. Each due re-test and each computed drift needs a recorded disposition.

The D4 generic `verify` operation still refuses belief claims. `retest_belief` is the separate
D9 path. The same source read must supply dispositions for the other claims it tested. A
pointer-only source or an old transcript does not prove that a fresh source read occurred.

A changed support has a cause change ID. A disposition must match that cause and cite the
recorded judgments. If a proposed revision also closes the support change, its operation IDs
run before the support disposition. A `gap_reason` records a consequence check missed by the
writer; it is not only an agent summary.

## Contract for hosts and views

This slice does not add a public endpoint. Hosts and views call the writer synchronously with
their authenticated world and a trusted caller. They must never accept a caller kind, actor,
world, source type, or belief state from an unauthenticated request body.

The writer's `correct_belief` operation accepts `target`, `correction`, `op_id`, `reason`, and
`evidence_refs`. The correction codes are `right`, `not_any_more`, `wrong`, and `restate`.
Optional fields include `title`, `body`, `stopped_being_true`, and `version`. A host relay must
carry the person's verbatim words and the message ID through the existing source/request
provenance. A paraphrase is not person authority. The operation returns a writer receipt at once;
it does not wait for a maintenance pass. Generic undo remains restricted to the person or writer.

Read consumers join `Belief.item_id` to `Item.id`. The item holds title, body, clocks, and horizon;
the belief holds state, origin, distinct supporting and contradicting episodes, source-time
bounds, external-test metadata, person hold, and successor. Display a belief with its state and
`inferred from N episodes`. Do not display it as a confirmed fact. Evidence polarity is separate:
contradicting episodes do not count as supports. Subject links use the primitive `describes`
meaning. Reads must keep world scope and the existing privacy checks.

## Scope and verification

Ontology hygiene, record compression, and the walk between areas are P1 work. This core envelope
does not silently accept those sections. Its refusal does not establish their acceptance.

`server/tests/consolidation` exercises idle triggers, queue completion, run binding, world
isolation, escaped complete histories, API refusal paths, question creation, and a concurrent
queue change before commit. Shared tests in `server/tests/runner` preserve dispatch, preemption,
and retry behavior. All local runs use the shared heavy-job lock.
