# Inactive local authority publication

Status: **INACTIVE_LOCAL_PUBLICATION_ONLY**.

`grove.authority.publisher` supplies a local file protocol for established authority origins. No production code imports it. The package initializer remains a docstring. There is no default owner, root path, environment fallback, database connection, provisioning, or runtime factory.

## Public interface

`open_publication(root, bindings, checks=owner, deadline=...)` opens only an existing private root, stable `.lock`, and `authority/` directory. The caller supplies a `PublicationBindings` record with installation, activation, genesis, inventory, profile, root, lock, authority-directory and store identities. These are comparison inputs. A trusted owner must establish their provenance independently.

One session owns one world operation, one process and one worker thread. It acquires EX before any owner operation. Its methods are:

- `inspect(world_id)`: validate the complete current manifest, journal and legacy ledger. This method creates or repairs nothing. The result remains untrusted.
- `stage_legacy(world_id, bytes)`: validate and stage an exact immutable proposed ledger in the session's private directory. The returned handle belongs only to that session.
- `prepare(world_id, ProposedPublication(...))`: preflight the complete prepare and anticipated completion, their manifests, capacity and optional ledger. It refuses birth/adoption and conflicting retry identities. An optional fixed seal may follow a restrictive prepare before its completion.
- `publish_legacy(pending, owned_candidate)`: install the exact approved extension before any effect completion. Every previous raw row interval must remain identical and in order. Exactly one new legacy/v2 row must match the effect's full binding.
- `complete(pending, completion_event)`: require an exact terminal owner outcome, publish completion, finalize projection metadata, then publish and inspect the final manifest.
- `inspect_recovery(world_id, proposal)`: validate the actual complete tail and report conditional recovery requirements. It may stage private validation candidates; it never changes final files.
- `recover(world_id, proposal, resolution_event)`: reconcile only the exact current legal prepare/completion tail. It never promotes a temporary file, truncates a final tail, or selects an older matching pair.
- `seal(world_id, seal_event, final_manifest)`: publish the fixed capacity seal, or reconcile that exact seal after interrupted publication. A repeat does not append another event.
- `cancel()`: signal cancellation only. The owning worker must reach a safe boundary and close its session.
- `close()`: close owned descriptors and candidates after the synchronous operation ends. EX remains held if the lifetime owner cannot complete terminal exit. A later close may finish that exit.

`inspect_initialization(InitializationRequirements(...))` always reports unmet controller, resource, inventory, restriction and bootstrap-owner requirements. It creates no object and issues no authority receipt.

## Required owner

The injected owner implements `operation`, `check_current`, `check_legacy_extension`, `resolve_outcome`, `finalize_projection`, `check_recovery`, and `check_final`. Its lifetime context exposes stable `lifetime_id` and `operation_id` values and `assert_current`. These calls must inspect actual resources within the current worker lifetime. They cannot return before their own subordinate work terminates. Context exit must join all owned work before it returns.

Owner observations use frozen records. The publisher compares exact world/owner identities, installation and world epochs, erasure revision, logical result, ledger, projection, operation identity and outstanding requirement tuples. An outcome must be `COMMITTED_EXACT` or the exact supported `CANCELLED_EXACT`. `UNKNOWN`, an absent row, elapsed time or process loss cannot prove rollback. Cancellation also requires the exact prior `NOT_APPLIED_PROVEN` observation and a committed cancellation/approval-burn outcome. A retry still requires current owner checks and a terminal outcome.

The owner is a **trusted dependency**, not a capability manufactured by these records. A malicious owner implementation can lie. No production owner exists in this component. Synthetic providers exist only in tests. Matching digests, copied receipts and constructor success never establish real consent, transaction outcome or current principal identity.

The logical effect transaction and its durable outcome record belong to the future writer owner. The publisher never enters a retryable database callback. The current legacy writer cannot be nested under this lease without a separate writer-owned integration change.

## Publication and recovery order

The publisher validates complete canonical candidates with the published codec and reducer. Arithmetic alone never authorizes publication. The complete reserve remains 525,312 bytes, three events and 16,448 nodes. Unrelated world heads and immutable installation fields remain unchanged.

Ordinary order is prepare journal, pending manifest, optional legacy extension, exact effect outcome, completion journal, projection metadata, final manifest, and final inspection. The central clock supplies the actual completion time. A changed time causes complete candidate revalidation and an updated final head digest.

For an installation restore, a durable installation-wide `restoring` manifest precedes the first journal change. Recovery uses the same before/after epoch pair if that barrier is the only completed step. The final manifest remains `restoring`; this unit does not claim that all inventoried worlds or restored database records are reconciled. A world restore preserves the installation epoch and advances the world epoch. No native restore runs here.

Every file replacement uses a same-directory private candidate, file fsync, checked replacement, directory fsync and exact final inspection. File identities and bytes are checked again after owner calls. A replacement with uncertain durability requires recovery and preserves the current bytes. It never rolls back a restriction. Recovery re-establishes durability for the exact current files before acknowledgement.

## Bounds and refusals

The existing profile remains unchanged: journal 16,000,000 bytes/32,768 events/262,144 nodes; manifest 1,048,576 bytes/2,048 worlds; legacy file 16,000,000 bytes with its stricter row limits. There are at most two journal, three manifest and one ledger candidates, with 51,145,728 total payload bytes. Directory scans stop at 4,096 entries. Paths have at most 64 components and 4,096 encoded bytes.

Hash, write, capture and row-comparison chunks are at most 65,536 bytes. Each decoder lifetime retains only its admitted input set. Parsed views do not transfer across closed budgets; detached bindings receive bounded metadata admission. Legacy comparison retains one packed old-row span table, then validates the proposed ledger in a new lifetime.

The 67,108,864-byte decoder ceiling and its fixed partitions still apply. Immutable capture also admits its simultaneous mutable buffer and one chunk before allocation. Caller-provided ledger bytes and temporary record copies count toward admission. Thus individually legal maximum-size files can fail combined/transient admission. A file-size limit does not promise that every combination fits. Full-shape peak memory and latency remain **NOT RUN**. The source does not claim the later two-second D7 target.

Public failures have fixed codes and stages. They contain no path, source text, ledger contents, credential, or raw operating-system exception. Missing or changed objects refuse; ordinary open does not repair permissions or initialize files.

## Verification scope

The focused synthetic tests cover private objects, exact old-row bytes, canonical replay, retries, terminal outcomes, timestamp changes, cancellation, permanent closure, seals, restore epochs/barriers, publication fault windows and retained lease ownership. Local child tests check process loss and two-process serialization across world heads. They do not simulate power loss or establish native database atomicity.

Run from the repository root:

```sh
GROVE_SLICE_ID=forget uv run --project server python -m pytest -q \
  server/tests/privacy/test_authority_publication_files.py \
  server/tests/privacy/test_authority_publication.py \
  server/tests/privacy/test_authority_publication_faults.py \
  server/tests/privacy/test_authority_legacy.py \
  server/tests/privacy/test_authority_inactive.py \
  server/tests/privacy/test_authority_codec_inactive.py
```

Remaining services include controller/bootstrap custody, real principal/consent and source checks, the database outcome schema and adapter, legacy writer integration, D10/D11 erasure and restore closure, D7 filtering/final response handoff, filesystem conformance on deployment hosts, and physical backup acceptance. No runtime activation, host action or deployment follows from this source component.
