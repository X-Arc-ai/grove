# Inactive authority outcome writer

This component connects the existing local publisher to fixed, world-scoped
transactions. It has no production factory, route, worker registration, account
integration, bootstrap procedure, or default owner services. Importing it does
not activate it. It does not establish general D7, D10, D11 or D14 acceptance.

## Ownership and entry

`InactiveAuthorityWriter` requires a driver, a world ID, a complete proposed
publication, and an `AuthorityOwners` bundle. A forget proposal also requires
the exact original and proposed legacy ledger bytes. Construction rejects
unsupported effects before it opens a file or starts a transaction.

The five services are trusted resource owners. Their method names describe an
integration contract, not authentication by structural typing:

| Service | Required methods and responsibility |
| --- | --- |
| installation | `validate_binding`, `validate_projection_in_tx`, `resolve`: actual installation, store identity, epoch and genesis custody |
| principal | `validate_current`, `approval_in_tx`, `resolve`: current person/session, exact choice and preview, revision, expiry and authentic historical approval |
| references | `validate_in_tx`, `validate_legacy_in_tx`, `validate_result_in_tx`, `resolve`: actual source versions, complete lineage, logical result and historical outcome evidence |
| journal | `capture`, `confirm`, `resolve`: actual private files and stable descriptor identities under the borrowed EX lease |
| workers | `operation`, `resolve`: actual owned worker context, with `assert_current` and terminal `drain` |

No production implementation of these services is supplied. An arbitrary
implementation can lie; this trusted boundary is not a sandbox for untrusted
Python. Synthetic services belong only in tests. A protocol object, digest,
copied receipt, raw dictionary or boolean does not bind the writer to a session.

The caller opens the existing publisher with this writer as `checks`, then calls
`session.bind_writer(writer)`. That method binds exactly once and runs read-only
preflight inside the one owned lifetime before any legacy candidate is staged.
Preflight checks the actual journal, the existing native projection, required
owners, remaining capacity, and the exact legacy plan. It never creates a
projection from the current database.

`session.assert_writer_owner` validates the same object, session, world,
operation, lifetime, process, thread, deadline and existing lease identities.
It does not acquire another flock or reenter the publisher's operation guard.
The publisher retains lease custody. A failed worker drain leaves that custody
with the original owner; cancellation cannot release it from another thread.

## Transactions and immutable facts

Migration `0018_authority_outcomes.cypher` declares five unique constraints over
three world-scoped ledger tables. See the generated store map for every column:

- `AuthorityProjection` holds checked logical and publication metadata plus the
  monotonic attempt fence. It is a cache, never consent.
- `AuthorityOperation` holds immutable intent registration, one current attempt
  slot, and immutable terminal outcome facts.
- `AuthorityApprovalUse` holds irreversible use/burn facts. It contains no bearer
  token, private payload, target text, raw event or filesystem path.

These tables are excluded from logical D3 snapshots and protected from ordinary
forget plans. Physical dumps can contain old copies, so a future restore owner
must quarantine/reconcile them and preserve the live fence high-water mark.
Matching hashes cannot make restored operational rows current authority.

Every mutation uses an issued frozen command, the concrete live binding, and
`WorldWriter.transaction`. The World lock precedes the callback's reads.
Attempt, operation, barrier and event identities are fixed before managed
callbacks. Callbacks perform no journal read, publication or new identity
allocation. Lease metadata checks remain with the publisher.

The seven fixed kernel entry points are:

1. `register_attempt_in_tx`: compare exact original identity and predecessor
   fence; reserve one stable attempt and publish pending metadata.
2. `inspect_outcome_in_tx`: read bounded current and historical facts without a
   mutation; missing facts remain `UNKNOWN`.
3. `fence_attempt_in_tx`: serialize behind prior writers, compare the full base,
   advance the persistent fence and revoke the old attempt.
4. `apply_effects_in_tx`: run the fixed supported forget dispatcher, consume the
   approval and write the terminal result in the same transaction.
5. `cancel_intent_in_tx`: recheck the current barrier proof, burn the original
   approval and commit cancellation without an expansion.
6. `finalize_projection_in_tx`: compare the actual durable completion and update
   publication metadata only.
7. `finalize_seal_in_tx`: update only exact seal metadata, with no operation,
   approval-use or logical-effect row.

An exact finalization duplicate issues no update and preserves the complete
World row, `read_generation`, `next_sequence` and D3 history. A conflicting or
older target refuses. Terminal operation rows never change during finalization.

Only a committed barrier followed by a fresh exact locked observation can
return `NOT_APPLIED_PROVEN`. A timeout, cancelled future, absent row, process
exit or generation number is not that proof. Lost acknowledgements remain
`UNKNOWN` until a fresh read establishes the exact result. A cancellation still
requires a later committed `CANCELLED_EXACT` before journal completion.

Rows use closed variants, exact field sets, bounded integers and canonical
identifiers. Digests are exactly 64 lowercase hexadecimal characters. The schema
lint permits only the 17 declared authority digest columns. Per-world operation
and approval-use counts are capped at 32,768; detached row facts at 16,384 bytes.
No TTL or compaction deletes idempotency, fence or approval-use facts.

## Supported effects and retained refusals

The apply dispatcher accepts exactly one `forget_apply` with a legacy or v2
ledger row. It never calls public `forget()`, reacquires a ledger lock, or runs
arbitrary PreparedBatch operations. Prepare and exact legacy publication precede
the effect transaction. The legacy seam verifies the complete old ledger and
preserves the exact prior row spans and new raw row bytes.

The seam compares all admitted domain bytes again before application. It
preserves D3 ChangeEntry rows, both time axes, corrections, undo behavior and
the established legacy `row_value` refusal. It validates replay after the
transformation. Current source/lineage checks remain separate from historical
operation evidence.

The current selector does not implement complete Learner or question closure.
Occupied later families and protected Requests therefore refuse before an
effect. A successful snapshot check alone cannot replace that closure. Typed
v3/v4 plans, Area, media, credentials, policy, origin, close and restore effects
remain unavailable.

The kernel supplies exact cancellation bookkeeping for already registered pure
expansions, but this component registers no production expansion handler.
The approval helper supports an exact renewed approval and atomic original burn;
that helper does not grant an expansion dispatcher or a runtime reapproval flow.

## Evidence limits

Synthetic owner/driver tests show local control behavior only. Native transaction
tests separately measure World locking, rollback, fencing and metadata no-ops.
Owned child termination is a third evidence class and does not prove power-loss
durability. Actual installation custody, current consent, complete erasure and
restore, and final response release remain live-owner work.

No new deployment, physical erasure drill, account consent or public acceptance
follows from this inactive source component.
