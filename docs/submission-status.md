# Submission status

Documentation release snapshot: 2026-11-04 at 20:06:46 UTC.
This package records accepted source and dated release evidence. It does not report a new
host check, complete product acceptance, or external submission. At this snapshot, the manual
account/Muse flow was stopped and real prepared-account Muse use was **NOT RUN**. The
[later Muse update](#prepared-account-muse-update) replaces that state.

## Prepared-account Muse update

After the snapshot above, the team confirmed on 2026-11-04 that Muse ran against the prepared
account and answered from Grove. This repository holds no receipt for that run. The dated records
below stay as written, and each one states what was true at its own time.

## Deployed build update, 2026-11-05

At 2026-11-05 00:37 UTC, `main` and the served build became `494921e5d8bc161202254f61a90c106dc869bba3`.
`develop` holds the same files at `8e8d347376c8dc71528e3eee615e14494665f407`; the two ids differ
because `main` is a copy of `develop` with cleaned commit messages. This deploy followed a CI waiver,
so `Validate` is not a pass for that build. The records below stay as written, and each one states
what was true at its own time.

## Source and deployed revisions

| Surface | Recorded revision | Evidence boundary |
|---|---|---|
| Accepted source baseline | `7d94266e1512ef9c5815d343518131a18bc6cbd8` | Separate develop merge of PR #66; includes PRs #58 and #59, materials, and deployment prerequisites; not the deployed source SHA |
| Recorded served API, PR #66 | `a9da90686d687639a75a154172adbe2bfb4a1ea7` | API-only recording release over exact PR60; independent anonymous public verification passed at 20:06:46 UTC |
| Retained runner and gateway | `266d0cf7429eb2ca51bd5bebf9f5785acbcd7b2e` | Separate prior revision; the recording release replaced neither service |
| Merged materials, PR #61 | `0682eb201ad0ccfb8a8a9d074d07759ec8eb31a1` | Accepted source `d17151421e027a4fe19b269a9f06cf6dcd17fe33`; all 43 component paths match; not deployed in the recording API |
| Source helper, PR #63 | `c145cf8b450f3ae7ab43989cf07969ee5b493057` | Source `f9c29e7e9afcdedcfce8190c9569b9bc71fd294a`; separates checkout and production image manifests; does not change the served application |
| Verified `deployed.sha` | `a9da90686d687639a75a154172adbe2bfb4a1ea7` | Recording release marker matches the deployed PR66 source |
| Previous recording API, PR #60 | `564cc99b9c8a718f163503cc23227a8f2908fcd1` | Recording release base and `previous.sha`; also the target of the dated ZIP and Request19 checks |
| Historical Request17 API | `cbcfe574e03880569bfcdb9a9732f79a7ef3daea` | Request19's historical `previous.sha`, not the currently recorded served API |

The document commit belongs in its PR receipt because a commit cannot contain its own final SHA.
Published source does not prove deployed acceptance. The [event record](../HACKATHON.md)
retains its distinct GitHub snapshot at 18:36 UTC through PR #59, with later source merges noted separately.

## Recording release

PR #66 deployed source `a9da90686d687639a75a154172adbe2bfb4a1ea7` over exact PR60.
The separate develop merge is `7d94266e1512ef9c5815d343518131a18bc6cbd8`.
Its seven changed paths contain UI code and tests. The release performed one API build,
one API replacement, and one API start, with zero other-service replacements, migrations,
seeds, or retries. The wrapper matched 402 image paths. Global aggregate counts remained
unchanged across the release; those aggregates are not an account graph count.

Root's separate anonymous public verification passed all 26 checks at 2026-11-04
20:06:46 UTC, including 15 exact asset hashes and the exact `/healthz` build.
The independent predeploy UI review inspected 30 desktop/mobile screenshots with synthetic APIs.
Its bounded review passed; it was not a real user or Muse pass. The original browser run
retains 84 passes and two service-worker fixture failures; the corrected targeted run passed
28 checks. The inherited mobile long-name wrap remains recorded. PR66 Validate was CANCELLED;
review-verdict was absent and waived. Neither is a CI pass.

The recording receipt SHA-256 is
`82e9868263ca6776e46ec0755357c3371ed2a9c4f4a50c58dbdd643ba59ba738`.
This document reports supplied evidence; it did not repeat browser or host checks.

A real Claude ZIP upload passed on PR60 at 2026-11-04 19:53:06 UTC. It retained
one source, one episode, and two kept messages. The exact repeat added zero new records.
The upload did not change the graph. This proves that import and duplicate handling for
the tested archive and release, not graph extraction or real prepared-account Muse use.
At that time, the manual account/Muse flow was stopped and real prepared-account Muse use was **NOT RUN**.

Materials and their deployment prerequisites are merged but not deployed. Operation A inventory
returned **HOLD** with no mutation. Riverlight source is uncommitted and unverified, and it was
not seeded. The complete 40-story corpus and full product/public acceptance remain open.

## Preserved Request18 failure

The Request18 diagnosis ended on 2026-11-04 at 18:54:49 UTC. All 20 runtime paths matched
exact hashes, and five policy controls passed. The production image correctly excludes four
`web/checks` paths. The wrapper incorrectly expected those paths, so Request18 remains FAILED.
Root separately recorded `/healthz` HTTP 200 at exact PR60 with status/store `ok`.
The release marker was stale at that diagnosis; Request19 corrected it below. No new host probe ran here.

Request19 preparation records seven source tests and 24 local wrapper tests passed, with the
original missing-file failure preserved. That local preparation proof is separate from the
completed operational closure below. Root issued one closure grant after PR #63 merged.

## Request19 release evidence update

Release evidence update: 2026-11-04 at 19:04:34 UTC. Request19 closure and separate
verification PASSED. Both exited naturally with status 0 and empty stderr. Root independently
verified public `/healthz` HTTP 200 with status/store `ok` and exact PR60 API target
`564cc99b9c8a718f163503cc23227a8f2908fcd1`. The source snapshot remains
`c145cf8b450f3ae7ab43989cf07969ee5b493057`; this was not a new code deployment.
Request18 remains FAILED, with its original artifacts preserved.

`deployed.sha` matches the PR60 API. `previous.sha` retains
`cbcfe574e03880569bfcdb9a9732f79a7ef3daea`. The failed barrier is archived and inactive.
The closure receipt, progress, and terminal records passed separate verification.
All eight expected containers retained their required identities, states, images, and restart
counts. The three host locks were free and the local operator lock was absent at verification.
No rebuild, restart, migration, seed, or runtime data write occurred.

The receipt records 24 checkout files, 20 image files, five source-policy controls, and 24 public
checks passed, including all 15 asset hashes. The four image-excluded check files remained absent.
These verified receipt identifiers cite the closure without private paths or account identities:

| Record | SHA-256 |
|---|---|
| Closure receipt | `e029b7b87ba5bd68c8447a459239dc3d3713cc727d8ed72c9f5e093ad56a1013` |
| Separate verification events | `8b25df182eaf1081439a7544280f098a382ba19dc8f546ffd4bd5fcf7f647238` |
| Terminal record | `6b2bdcddcbe89f4548ddda7089b2a5d11ee31c99e8082f0d5a46ecf999f42e2c` |
| Preserved Request18 barrier archive | `a2657e915f29eed845929689fe17218aa80dc45bab6e99192e8e51f41a58b4d0` |

At this dated Request19 verification, the graph retained generation 53, 1,355 nodes, and
4,082 edges, with finite values, valid endpoints, and no truncation. The corpus retained
1,352 Items, 26 Sources, 26 saved Requests, and 26 receipts. These are dated verified counts,
not an unconditional current claim. The two Cedar Items retained their saved provenance digest;
the pending-work census was empty, and all 17 migration ledger entries passed verification.

The fixed application target remains PR60; Request19 did not deploy materials. Materials remain
merged but not deployed. Operational closure does not establish owner/Muse, authenticated
upload, Connect/Disconnect, worker activation, or full-product acceptance. The complete
40-story corpus remains incomplete; four earlier recall passes retain their original scope,
and Pebble remains INCOMPLETE. No new host or runtime checks ran for this document correction.

## Recorded source and historical evidence

PR #58 records 213 focused passes for ordinary JSON/ZIP parsing, mandatory screening,
retention, receipts, isolation, idempotency, and no graph mutation. PR #59 records 58 focused
passes for account status, catalog, scopes, and confirmed account-scoped disconnection.
Those saved source checks do not prove owner acceptance or live provider consent.
Their later automatic Validate runs failed at Ruff under the existing publication waiver.
The published UI repair in PR #60 has separate author and independent evidence for its exact heads.
Those checks do not establish complete deployed UI acceptance.

PR #61 original component proof passed 225 tests. The final repair passed 70 tests and seven
source checks. The final independent F4 source review is PASS at
`d17151421e027a4fe19b269a9f06cf6dcd17fe33`, not full R4. Earlier F1-F3, recovery, F4,
UI failures, and the immediate lock refusal remain preserved. These counts refer to different
source snapshots; they are not one combined run. No materials runtime acceptance is claimed.
Files remain `held_provenance` with no file Request. Notes use ordinary Requests.
Migration `0019_material_revisions.cypher`, the canonical external ledger, and coordinated
API/runner source require a future materials release. See [the material boundary](architecture.md#browser-material-boundary).

Historical Request17 recorded 24 public smoke checks, including 15 exact assets. It recorded
1,355 nodes, 4,082 edges, two core Cedar saved Items, and 26 Sources, saved Requests, and receipts.
These are historical receipt values, not fresh counts. The complete 40-story corpus remains
incomplete. Four richer topics passed an earlier backend read; Pebble remains INCOMPLETE.
The Request18 diagnosis did not repeat corpus, core-data, or public-route proof.
At that time, the natural Muse questions and automatic Grove use were unverified. Backend recall is not a Muse rehearsal.

## Capability boundaries

| Capability | Current source and remaining limit |
|---|---|
| Storage, writer, history, and time | Source components exist with scoped tests; a queued request is not a saved receipt |
| Ordinary chat uploads | PR #58 supports screened ChatGPT/Claude JSON and ZIP retention; the dated real Claude ZIP and exact repeat passed on PR60; no graph Items or links result from retention alone |
| Restricted demo | Prepared synthetic Cedar/project data supports restricted recall; the upload does not create that graph |
| General MCP | `DEFAULT_TOOLS = ()`; the restricted pair does not establish general `store`/`recall` acceptance |
| Calendar/Git | Read-only connector source exists; actual consent and configured live-source acceptance remain open |
| Pipedream | Authorization, status, catalog, and disconnect source; no content import or synchronization |
| Files and notes | Merged PR #61 supplies screened storage and ordinary note Requests; files remain `held_provenance` with no file Request; typed-file graph admission remains separate |
| Workers and Questions | Status and inactive protocol components exist; complete D7/D10/D11/D14 runtime acceptance remains open |
| Privacy, reset, and restore | Source components and guards exist; complete delete-brain and physical/runtime restore acceptance remain open |
| UI | PR #66 is the recorded served API; bounded synthetic desktop/mobile review and anonymous public checks passed; real user, full control, and complete responsive acceptance remain open |
| Performance | Earlier HTML and asset measurements are historical; total initial transfer and current first-paint acceptance remain unverified |

Unavailable controls do not satisfy the required feature scope. CI or source publication does
not change these limits. See [architecture](architecture.md) for component links.

## Preserved history

PR #18 merged as `93ea06e7a2b771e9051c1179f431882a40b22a44` from
`d9d6a74952f961938240874f2e7e9b0a2c07e309`. That refresh passed eight focused checks and
preserved two expected old-document failures. Its two automatic Validate runs were CANCELLED.
The current package continues that work and preserves the previous lane's evidence.

The earlier 2026-11-03 candidate `947627ca7dc0b801de660fe15fcb214c275d6d54` passed its
focused checks, identity/name lint, reviewed secret scan, and Validate. Its independent verdict
remained **FAIL/BLOCKED**: the browser stopped at unavailable account/inbox access, and isolated
Muse exited before consent with `OAuth HTTP request failed: error sending request`.
The older empty registry, missing upload route, and runner-network failure remain historical
findings. Later repairs do not erase those failures. The original provenance failure also remains.

## Next release action

Root owns CI observations and merge. Request19 closure and its separate verification are recorded above.
The release owner must sequence any future materials release.

CI and independent review are **WAIVED as source-publication gates**, not passed.
This package runs focused document controls only. New browser, Muse, account, provider,
model, database, and host checks are **NOT RUN**. Final release-candidate name/secret scans,
video, official rule review, and final submission links remain required preparation.

No complete forgetting, measured recall accuracy, full runtime acceptance, or final submission is claimed.
