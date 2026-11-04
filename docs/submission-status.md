# Submission status

Documentation source snapshot: 2026-11-04 at 19:04 UTC. Repository: **PRIVATE**.
This package records accepted source and dated release evidence. It does not report a new
host check, complete product acceptance, or external submission. The current owner manual
login/upload/Muse flow is paused and remains **NOT RUN** for this release.

## Source and deployed revisions

| Surface | Recorded revision | Evidence boundary |
|---|---|---|
| Accepted source baseline | `c145cf8b450f3ae7ab43989cf07969ee5b493057` | Includes PRs #58 and #59, UI PR #60, materials PR #61, and the separate PR #63 source helper |
| Recorded served API, PR #60 | `564cc99b9c8a718f163503cc23227a8f2908fcd1` | Request19 closure and separate verification PASSED at the dated release update below; Root verified healthy `/healthz`; Request18 remains FAILED |
| Retained runner and gateway | `266d0cf7429eb2ca51bd5bebf9f5785acbcd7b2e` | Request19 verified unchanged container identities, states, images, and restart counts |
| Merged materials, PR #61 | `0682eb201ad0ccfb8a8a9d074d07759ec8eb31a1` | Accepted source `d17151421e027a4fe19b269a9f06cf6dcd17fe33`; all 43 component paths match; not deployed in the recorded PR60 API |
| Source helper, PR #63 | `c145cf8b450f3ae7ab43989cf07969ee5b493057` | Source `f9c29e7e9afcdedcfce8190c9569b9bc71fd294a`; separates checkout and production image manifests; does not change the served application |
| Verified `deployed.sha` | `564cc99b9c8a718f163503cc23227a8f2908fcd1` | Request19 release marker matches the PR60 API |
| Historical Request17 API, retained `previous.sha` | `cbcfe574e03880569bfcdb9a9732f79a7ef3daea` | Verified previous release marker, not the currently recorded served API |

The document commit belongs in its PR receipt because a commit cannot contain its own final SHA.
Published source does not prove deployed acceptance. The [event record](../HACKATHON.md)
retains its distinct GitHub snapshot at 18:36 UTC through PR #59, with later source merges noted separately.

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
The natural Muse questions and automatic Grove use remain unverified. Backend recall is not a Muse rehearsal.

## Capability boundaries

| Capability | Current source and remaining limit |
|---|---|
| Storage, writer, history, and time | Source components exist with scoped tests; a queued request is not a saved receipt |
| Ordinary chat uploads | PR #58 supports screened ChatGPT/Claude JSON and ZIP retention; no graph Items or links result from retention alone |
| Restricted demo | Prepared synthetic Cedar/project data supports restricted recall; the upload does not create that graph |
| General MCP | `DEFAULT_TOOLS = ()`; the restricted pair does not establish general `store`/`recall` acceptance |
| Calendar/Git | Read-only connector source exists; actual consent and configured live-source acceptance remain open |
| Pipedream | Authorization, status, catalog, and disconnect source; no content import or synchronization |
| Files and notes | Merged PR #61 supplies screened storage and ordinary note Requests; files remain `held_provenance` with no file Request; typed-file graph admission remains separate |
| Workers and Questions | Status and inactive protocol components exist; complete D7/D10/D11/D14 runtime acceptance remains open |
| Privacy, reset, and restore | Source components and guards exist; complete delete-brain and physical/runtime restore acceptance remain open |
| UI | PR #60 is the recorded served API; source checks apply only to their recorded heads; full control and responsive acceptance remain open |
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
The release owner must sequence any future materials release and complete the [operator checklist](submission-checklist.md).
Keep the repository private. Do not resume the paused owner flow or submit externally without authority.

CI and independent review are **WAIVED as source-publication gates**, not passed.
This package runs focused document controls only. New browser, Muse, account, provider,
model, database, and host checks are **NOT RUN**. Final release-candidate name/secret scans,
video, official rule review, and final submission links remain required preparation.

The [write-up](drafts/submission-writeup.txt) and [X post](drafts/x-post.txt) are unsent drafts.
No complete forgetting, measured recall accuracy, full runtime acceptance, or final submission is claimed.
