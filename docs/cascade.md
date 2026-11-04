# Consequences and explanations

`grove.cascade.plan(snapshot, changes)` is the single pure planner for P0 R1, R2 and R4.
The writer calls it inside its existing world transaction after validation. D7 supplies its
captured transaction snapshot to the same planner for simulation. R3 and R5 are P1.

R1 ends canonical consequence links at the trigger's world time when the trigger has confirming
evidence, or is the person's correction. Reports, unknown times, future times and provisional
meanings produce proposals. R2 marks downstream items through at most three consequence hops.
A trigger that reaches over 100 items keeps direct marks and a wide-impact proposal. History,
evidence and decomposition links do not carry P0 propagation. R4 proposes a prior status for a
blocked item with no live upstream; otherwise it proposes a review. Failed ordinary ending
validation preserves the trigger, stores a `cascade_failed` proposal, and prevents a false resume.

`writer/cascade.py` selects a trigger's own evidence and polarity. It does not inherit the item's
old source rank. It reads status roles and declared time properties, including the code-held
`scheduled_role` declarations from D4. New kind declarations can name `propagating_fields` as a
list of `{name, role}` pairs; roles are `start`, `due`, or `end`. The named property must exist
with value type `time` before a change propagates. No rule depends on a kind or meaning name.

An ordinary-item refutation from an unconfirmed source retains contradicting evidence, opens a
`Contradiction`, and emits the proposed terminal trigger without ending the claim. A confirmed
refutation closes that report queue. A paired D4 `verify(moved)` still checks the recorded
judgment against the actual claim and source; an unrelated page cannot pass that verification.
This is the ordinary-item beat. D9 owns belief transitions and the full contradiction workflow.

## Storage and history

An `affected by` primitive link points from the dependent to the trigger. `Impact` is keyed by
its link ID, with one mark per trigger change set and affected item. Marks have low stakes,
their trigger's source type and world clock, the original path, and an explicit state. A trigger
older than the dependent's latest world change produces `born_resolved`. An affected item's
ending resolves its open marks; undo retains the mark links and marks them `undone`.

`CascadeProposal.detail` contains IDs, codes, versions and the original trigger clock. Suggested
status text, explanation sentences, and decision reasons use `HistoryValue` references.
The `cascade_mark`, `cascade_proposal`, `cascade_end`, `cascade_disposition` and
`cascade_explanation` history markers do not project user properties or create version conflicts
on undo. Snapshot restore retains Impact, CascadeProposal and Contradiction state.

The writer accepts these explicit operations through `apply_batch`:

- `accept_proposal`: `proposal`, `reason`, optional `version`, `operations`, `evidence_refs`.
  A provisional mark also requires `apply_mark: true`. Ordinary operations preserve caller
  authority and may touch only the proposal's target or listed links.
- `reject_proposal`: `proposal`, `reason`, optional `version`.
- `resolve_mark`: `mark`, `disposition` (`still_holding`, `revised`, `dismissed`), `reason`,
  optional `version`, `evidence_refs`. Cleanup requires a live confirming source read after the mark.

A changed target makes a proposal stale and saves recomputed proposals. It applies no stale
operation. The original event's source rank and time remain intact. Receipts show `~` marks and
`?` proposals alongside the trigger and ordinary endings.

## Read and model boundary

`store/cascade_reads.py::snapshot` accepts a scoped reader. A D7 caller must use its single
transaction adapter, not combine independent reads. It captures pre-cascade active links and
post-operation item states. The standalone `what_it_breaks` helper is a domain helper; D7 owns
public hypothetical parsing and REST/MCP response contracts.

`cascade_jobs.read_explanation(driver, world_id, mark_id)` is store-pure and model-free. It
returns a cached sentence or a screened template plus `needs_explanation`. It does not enqueue.
An explicit session-authorized `POST /v1/cascades/{mark_id}/explanation` with body `{}` requests
the job. The route checks JSON content type and the Origin header. It never calls a model.

`api/app.py::create_app` starts `api/cascade.py::ExplanationDispatcher` for its owned driver.
Injected-driver tests opt in with an explicit factory. The worker scans scoped pending jobs and
proposal marks, then calls `explain_pending` outside the writer transaction. A missing model key
or screening configuration leaves jobs retryable. Claims have renewable leases. Shutdown stops
new work and returns within a bounded wait; an active provider call retains its driver until exit.

The metered `explain_break` model job receives only the computed path's screened titles, old and
new state, clocks, source type and meaning descriptions. Constrained JSON selects references
and closed sentence forms. Code renders titles, rank and time locally. Off-path, private,
multisentence or overlong output falls back to a template. A cache write rechecks mark version,
packet fingerprint, source call and screening under the world lock. It cannot change reach,
links, claim state or mark disposition.

This source implementation does not prove deployment. D14 owns the deployed data-flow statement
and physical forget coverage. D7 owns complete packet and read acceptance. D9/D12 consume the
accepted planner through code-held `run.cache["belief_cascade_changes"]`; they own real transition
and safety-net integration tests. No unmerged consumer implementation is required by this module.

## Checks

Run `GROVE_SLICE_ID=<lane> uv run pytest tests/cascade` from `server/`. CI excludes `live`.
The suite includes rule-disable red controls, exact reach for ten changes on 30 nodes, writer
atomicity, gate and provisional controls, backfill, undo, restore, simulation, proposal decisions,
source/time checks and explanation isolation. The opt-in synthetic model test is
`tests/cascade/test_live.py -m live` and records its call through the model layer.

`writer/cascade.py::health` exposes live consequence links touching ended items, open mark times,
waiting proposals and cascade failures. D13 owns presentation and age buckets for these counters.
