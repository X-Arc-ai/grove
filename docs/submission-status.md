# Submission status

Interim snapshot: 2026-11-04 13:51 UTC. This document separates accepted source, deployment,
and actual acceptance. The full approved feature scope remains required. The direct publication
waiver permits this documentation refresh; it does not turn incomplete features into completed ones.

## Revisions

| Surface | Revision at this check | Meaning |
|---|---|---|
| `develop` | `6346a9fc7c6c9b8119a9e7310cb93944f2fb1afe` | Accepted source baseline, including inactive PRs #33 and #34 components |
| Public `/healthz` | `cac4b95cbc7ce5284c04fcb684ebb2f115162144` | Service/store OK; Request7 UI release |
| `main` | `b5e45f71b7fc84d3fbc57c1dc86a1fe4254fd19f` | Initial repository revision; this lane makes no main merge |

The document commit and merge identifiers belong to the PR and lane receipt because a commit
cannot contain its own final SHA. Final release-candidate R1 and secret scans remain **NOT RUN**.
The 2026-11-03 scans certify only their old candidate, not this source baseline or a later main revision.

## Claim and evidence map

| Component | Source and deployment evidence | Actual limit |
|---|---|---|
| Typed storage, requests and writer | PRs #1, #6, #9; `server/tests/writer/` | A queued request is not a saved receipt |
| Corrections, history and restore | PR #16; D3 writer/store suites; included in Request7 ancestry | Does not establish complete D14 physical restore acceptance |
| Truth clock, capabilities and Calendar facts | PR #15; Calendar provenance and clock suites; included in Request7 | Source timestamps do not prove narrated events occurred |
| Source screening and chat uploads | PRs #7, #17; `server/tests/ingest/` | General private-export processing is not enabled on the current host |
| Calendar/Git connectors | PR #20; `server/tests/connectors/`; included in Request7 | Actual provider consent and configured live-source acceptance remain open |
| Sign-in and OAuth | PRs #8, #11; auth/host suites | Current owner login and Muse consent remain NOT RUN |
| Restricted Cedar demo | PR #27; `server/tests/hosts/test_demo_recall.py`; Request6 controlled seed | One saved request and one directly recalled eligible item; owner Muse exercise NOT RUN |
| General MCP | `DEFAULT_TOOLS = ()`; standalone store component in `server/grove/mcp/store.py` | The restricted pair does not provide general `store`/`recall` acceptance |
| Mixed batch refusal | PR #31; MCP preflight tests; included in Request7 | Refusal checks do not establish the complete general read path |
| UI guides, catalog and real upload | PRs #25, #26, #32; Request7 exact public assets | Owner upload and full responsive behavior remain NOT RUN |
| Pipedream | PRs #29, #30; `docs/pipedream.md`; included in Request7 | Authorization/status only, no imports; actual consent remains unverified |
| Runner, gateway and models | PRs #5, #10, #12, #14, #19, #22, #24, #28; Request7 health/smokes | Bounded component checks do not establish complete agent behavior |
| Ontology | PR #13; ontology/writer suites | Full hygiene, merge/split/rename/retirement workflows remain open |
| Walk consequences | PR #23; `docs/cascade.md`; included in Request7 ancestry | Deployed source does not prove complete Walk/Beliefs runtime acceptance |
| Erasure and restore privacy source | PR #21 merged after Request7 | Not deployed; D14 physical/runtime acceptance remains open |
| Inactive authority validation and file leases | PR #34 | Not deployed or activated; does not complete general D7/D14 acceptance |
| Internal text and media components | PR #33; `docs/ingest-media.md`, `docs/ingest-audio.md`, `docs/ingest-whatsapp.md` | Not deployed; no new upload provider or complete typed persistence |

The [event record](../HACKATHON.md) lists PRs, UTC dates, and exact merge identifiers.
Test paths identify evidence sources. This documentation refresh does not claim a fresh run of those product suites.

## Deployment evidence and owner acceptance

Request7 deployed `cac4b95cbc7ce5284c04fcb684ebb2f115162144`. Its receipt records 14 successful host/Mac smoke checks,
15 exact public assets, six expected refusals, healthy API/gateway/store services, and a running runner.
The controller independently confirmed the served revision. The fresh 13:51 UTC health read agrees.
No seed repeated during Request7. These are service and anonymous checks, not owner acceptance.

The current demonstration has two separate accounts and datasets:

1. An ordinary account uploads the exact Lumen file and receives source-retention counts.
2. A prepared account uses restricted Cedar recall through Muse. Its content predates the Lumen upload.

The upload does not create the prepared graph. Both owner paths remain **NOT RUN**.
General `store`/`recall`, arbitrary imports, history, Areas and complete privacy remain incomplete.
The prepared account refuses uploads and Pipedream. Pipedream imports no content.

The public HTML is 1,073,088 raw bytes. The earlier local gzip result is 336,709 bytes.
The 15 asset bodies total 1,264,896 bytes. These different measurements do not establish total
compressed initial transfer or first paint. The 500,000-byte total target remains unverified.

## Remaining acceptance and owners

| Acceptance | Owner/dependency | Required evidence |
|---|---|---|
| Current two-account demo | Release owner and demo owners | Normal login, exact Lumen first/repeat upload, logout, actual Muse OAuth and two independent recalls |
| README-only fresh-context path | Submission and Muse owners | Fresh browser and isolated Muse Code HOME at the accepted final revision |
| General imports and typed persistence | Connector, ingest and writer owners | Configured screening, real retention/provenance, extraction, and complete input guards |
| General D7 reads and D14 privacy | Read-path and privacy owners | Complete authority guards, world-scoped results, erasure and physical/runtime restore acceptance |
| Full remaining feature scope | Beliefs, cleanup, ontology, evals and view owners | Original acceptance criteria with fresh evidence |
| Provider connection | Release owner / integration owners | Real authorized consent and refreshed status, never an inferred content import |
| UI completion and page weight | View owners | Responsive behavior, source/revoke integration and complete initial-transfer measurement |
| Film and rehearsal | Release owner / invented-data film owner | Recorded actual behavior and complete rehearsal, with no private data |
| Final release evidence | Submission and release owner | Current event history, exact release SHA, R1 and secret scans |
| Main/public release and submission | Controller / release owner | Authorized release actions and final links; this lane does none of them |

## Preserved historical failures and process status

The 2026-11-03 candidate `947627ca7dc0b801de660fe15fcb214c275d6d54` passed its focused checks,
identity/name lint, reviewed secret scan and Validate. Its independent verdict remained **FAIL/BLOCKED**:
the fresh browser stopped at unavailable account/inbox access, and isolated Muse exited before consent
with `OAuth HTTP request failed: error sending request`. The host then served `9a5cb23`.
The empty public registry, absent upload route, and failed runner-network transition were findings
at that earlier snapshot. They are not descriptions of today's restricted deployed demo.
The original logs, screenshots, reviewed scan matches and failed provenance test remain retained.

CI, independent review and full-product acceptance are **WAIVED as source-publication gates** for
this refresh. They are not reported as passed. The current focused document results and exact commit
belong to PR #18 and its final receipt. No new browser, Muse, account or provider session runs here.

[Write-up](drafts/submission-writeup.txt) and [X post](drafts/x-post.txt) remain unsent drafts.
No completed submission, complete forgetting, measured recall accuracy, or final public acceptance is claimed.
