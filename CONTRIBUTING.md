# Contributing

Use the [README setup](README.md#run-it-locally) to install the locked Python environment.
Coordinate shared-file ownership before parallel work.
Use a separate Git worktree for each active change. Preserve existing commits and evidence.

## Repository map

| Path | Purpose |
|---|---|
| `server/grove/api/` | HTTP, browser sessions, OAuth, upload, and view routes |
| `server/grove/writer/` | Validated changes, durable requests, history, and receipts |
| `server/grove/store/` | Scoped reads, schema catalogue, and database connection |
| `server/grove/mcp/` | MCP transport and tool definitions |
| `server/grove/ingest/` | Source parsing and retention |
| `server/grove/runner/` and `runner/` | Worker orchestration, CLI contracts, and agent configuration |
| `server/migrations/` | Append-only migration files |
| `server/tests/` | Focused Python tests, including synthetic integration tests |
| `web/` | Browser source, imported presentation assets, and build script |
| `deploy/` | Service, nginx, deployment, and backup procedures |
| `docs/` | Component contracts and submission evidence summaries |

## Test the change

From the repository root, run only the area relevant to the change. For this package:

```sh
uv run --project server python -m pytest server/tests/submission -q
```

These document checks need no database or provider. Integration tests can require the shared
local Neo4j store and can write synthetic worlds. Read the component instructions first.
Set a unique `GROVE_SLICE_ID` for database tests and preserve their cleanup fixtures.
Tests marked `live` or `browser` need separate configuration; they are not implied by a unit pass.
Never put real exports, credentials, account identifiers, or one-time codes into test artifacts.

On a shared development machine, coordinate the test window and use the configured nonqueued
test lock. If the lock is busy, stop rather than queue.
Do not install dependencies, start another store, or run a broad suite during another task's window.

## Materials and release evidence

The 2026-11-04 19:04 UTC source snapshot includes PR #61 materials and PR #63 source helpers.
Document checks do not execute material routes or prove runtime acceptance. Preserve the
`held_provenance` file boundary, ordinary note Requests, and the canonical external ledger contract.
A materials release requires migration `0019_material_revisions.cypher` and coordinated API/runner
source. Keep that release separate from the recorded PR60 API and Request19 closure.
See [the material boundary](docs/architecture.md#browser-material-boundary).

## Review and provenance

Every repaired defect needs a regression test in the same PR. Preserve the original failure
and produce a passing result after the final change. State the exact tested commit and limits.
Do not convert a failed, cancelled, waived, or unrun check into a pass.

Preserve [the pre-event attribution](HACKATHON.md#pre-event-material), the license, vendored
credits, and privacy contracts. Do not edit old migrations or commit generated caches and builds.
`web/build.py` also regenerates `docs/data-flow.md`; inspect that source-derived change explicitly.
A source merge does not authorize deployment, repository visibility changes, or external submission.
