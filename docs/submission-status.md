# Submission status

Interim snapshot: 2026-11-03. This document records remaining acceptance work; it does not
reduce the approved feature scope or authorize a public release.

## Revisions

| Surface | Revision at this check | Meaning |
|---|---|---|
| `develop` | `57da012f05eecfd0641773e12e4f3a0912bff5cb` | Merged baseline for these documents |
| Public `/healthz` | `9a5cb23ba2935a66f0091e72ccc7e38c54695722` | Response reported `status: ok`, `store: ok` |
| `main` | `b5e45f71b7fc84d3fbc57c1dc86a1fe4254fd19f` | Initial repository revision; no release merge |

A later SHA needs new checks. The health response measures service and store access only.
Exact document-candidate scan results belong to the PR and its verification receipt, because a
commit cannot contain its own final SHA. No main-candidate certification is claimed here.

## Claim and evidence map

| Claim | Repository evidence | Boundary |
|---|---|---|
| Typed storage and validating writer | PRs #1, #6; `server/tests/writer/` | Does not prove complete correction and restore behavior |
| Durable requests and reuse-first resolver | PR #9; `test_idempotency.py`, `test_resolver.py` in the writer suite | Does not prove the complete public upload path |
| Source screening | PR #7; `server/tests/screening/` | Requires connector integration before a live import claim |
| Google and email-code sign-in | PR #8; `server/tests/auth/`, `server/tests/e2e/test_signin_browser.py` | Full real public account flow still needs fresh acceptance |
| OAuth and MCP transport | PR #11; `server/tests/hosts/test_flow_checks.py` | Production tool registry is empty |
| Initial graph views and persistent positions | PR #4; `server/tests/views/test_positions_api.py` | Some other views retain simulated state |
| Model calls and Glimmer deployment | PRs #5, #10, #12; `server/tests/models/` | Live component probes do not establish public application behavior |
| OpenCode runner and brain CLI | PR #14; `server/tests/runner/` | Public runner deployment and final feature prompts remain pending |
| Ontology floor | PR #13; `server/tests/writer/test_ontology.py` | Does not include the later ontology hygiene operations |

The [event record](../HACKATHON.md) links every PR and records exact dates.
Test paths identify evidence sources, not a fresh run of those tests in this document change.

## Open acceptance gates

| Gate | Owner or dependency | Required evidence |
|---|---|---|
| Export upload reaches storage | `ingest-a1` / PR #17, remaining connector slices, `views-b1` | Real upload, source receipt, resulting item, idempotent re-import |
| Truth-clock and complete corrections | `truth-clock` / PR #15; `writer-b` / PR #16 | Final tests and independent review at their final heads |
| Public `store` and `recall` | `hosts-b1`, `hosts-b2`, `ingest-b` | Exactly two tools, scoped read/write, receipt after commit |
| Public runner and gateway | `deploy-c`, after the upload/deploy overlap resolves | Checked-in network repair, successful full-SHA deployment, public feature tests |
| Full feature scope | `walk`, `cleanup`, `beliefs`, `forget`, `evals`, remaining views and connectors | Their original acceptance criteria, with fresh evidence |
| README-only path | `submission`, `muse-e2e`, account access | Fresh browser and isolated Muse Code HOME complete sign-in, upload, recall and store |
| Film and rehearsal | `film-synthetic`, feature owners | Invented-data world and complete public rehearsal |
| Final history and candidate scans | `submission`, release owner | Refreshed PR dates, exact SHA, identity/name lint and secret scan |
| Main and public switch | Release owner | Explicit approval, CI runner transition, main checks, public visibility decision |

The public runner deployment failed during a Docker network transition, and the prior revision
was restored. A merged runner PR is not proof that the runner is live. The README presents the
remaining judge path as a pending test, not as completed product behavior.

## Drafts

[Write-up draft](drafts/submission-writeup.txt) and [X post draft](drafts/x-post.txt) remain unsent.
Their counts and limits require refresh after later merges. No benchmark accuracy, recall latency,
complete forgetting, or completed public connection is claimed by these drafts.
