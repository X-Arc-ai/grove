# Submission status

Documentation source snapshot: 2026-11-04, after PR #60. Repository: **PRIVATE**.
This package records accepted source and existing release receipts. It does not report a new
host check, complete product acceptance, or external submission. The current owner manual
login/upload/Muse flow is paused and remains **NOT RUN** for this release.

## Source and deployed revisions

| Surface | Recorded revision | Evidence boundary |
|---|---|---|
| Accepted source baseline | `564cc99b9c8a718f163503cc23227a8f2908fcd1` | Includes PRs #58 and #59 plus PR #60; these repairs are not deployed in the receipt below |
| Request17 API | `cbcfe574e03880569bfcdb9a9732f79a7ef3daea` | API-only release; anonymous root redirects to `/login` |
| Request17 runner and gateway | `266d0cf7429eb2ca51bd5bebf9f5785acbcd7b2e` | Existing services retained their source and did not restart |
| Published UI repair, PR #60 | `564cc99b9c8a718f163503cc23227a8f2908fcd1` | Published source in this snapshot; not deployed in Request17 |
| Separate materials candidate | `b4b804acf94250c2092f2e3c7623d6e79ee8bb59`, unpublished | Excluded from this snapshot and deployment; requires separate migration `0019_material_revisions.cypher` |

The document commit belongs in its PR receipt because a commit cannot contain its own final SHA.
The controller's public health read still reports the Request17 API revision above.
The next API deployment is not yet executed. Published source does not prove deployed acceptance.
The [event record](../HACKATHON.md) remains the dated GitHub snapshot at 18:36 UTC through PR #59;
it is not a deployment or acceptance list.

## Recorded evidence

Request17 records 24 successful public smoke checks, including 15 exact assets; the root
redirect and login page passed. Anonymous protected reads refused access. The retained graph
contained 1,355 nodes and 4,082 edges with valid endpoints. The core Cedar proof contained two
saved Items. The corpus record contained 26 Sources, 26 saved Requests, and 26 receipts.
These are receipt values, not fresh counts measured by this document task.

The complete 40-story corpus remains incomplete. Four richer topics passed an earlier backend
read, but Request17 final verification repeated only the accepted core Cedar proof. The natural
Muse questions and automatic Grove use remain unverified. Backend recall is not a Muse rehearsal.

PR #58 records 213 focused passes for real ordinary JSON/ZIP parsing, mandatory screening,
retention, receipts, isolation, idempotency, and no graph mutation. PR #59 records 58 focused
passes for account status, catalog, scopes, and confirmed account-scoped disconnection.
Those saved source checks do not prove deployed behavior or live provider consent.
Their later automatic Validate runs failed at Ruff under the existing publication waiver.

The published UI repair in PR #60 has separate author and independent evidence for its exact heads.
Those checks do not establish deployed acceptance. The unpublished materials candidate records
222 focused native controls and requires migration 0019 plus release integration. Files report
`held_provenance` and create no file Request. This documentation does not certify materials acceptance.

## Capability boundaries

| Capability | Current source and remaining limit |
|---|---|
| Storage, writer, history, and time | Source components exist with scoped tests; a queued request is not a saved receipt |
| Ordinary chat uploads | PR #58 supports screened ChatGPT/Claude JSON and ZIP retention; no graph Items or links result from retention alone |
| Restricted demo | Prepared synthetic Cedar/project data supports restricted recall; the upload does not create that graph |
| General MCP | `DEFAULT_TOOLS = ()`; the restricted pair does not establish general `store`/`recall` acceptance |
| Calendar/Git | Read-only connector source exists; actual consent and configured live-source acceptance remain open |
| Pipedream | Authorization, status, catalog, and disconnect source; no content import or synchronization |
| Files and notes | A separate unpublished candidate supplies material storage and note processing; files remain `held_provenance` with no file Request; typed-file graph admission remains separate |
| Workers and Questions | Status and inactive protocol components exist; complete D7/D10/D11/D14 runtime acceptance remains open |
| Privacy, reset, and restore | Source components and guards exist; complete delete-brain and physical/runtime restore acceptance remain open |
| UI | PR #60 is published source, not deployed acceptance; source checks apply only to their recorded heads; full control and responsive acceptance remain open |
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

The release owner must sequence accepted UI/backend source, refresh the exact service revisions,
and complete the [operator checklist](submission-checklist.md). Keep the repository private.
Do not resume the paused owner flow or submit externally without the corresponding authority.

CI and independent review are **WAIVED as source-publication gates**, not passed.
This package runs focused document controls only. New browser, Muse, account, provider,
model, database, and host checks are **NOT RUN**. Final release-candidate name/secret scans,
video, official rule review, and final submission links remain required preparation.

The [write-up](drafts/submission-writeup.txt) and [X post](drafts/x-post.txt) are unsent drafts.
No complete forgetting, measured recall accuracy, full runtime acceptance, or final submission is claimed.
