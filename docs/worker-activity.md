# Worker activity

`GET /v1/views/workers` requires the normal account session. It accepts no query parameters.
It reads only the session's World. It does not start, select, retry, recover, or end work.

The fixed `grove.worker-activity.v1` response separates:

- `policy`: allowed for ordinary Worlds; limited Learner and disabled Curator/Connector for a designated demo World. Partial designation remains restricted.
- `capability`: legacy Learner/Connector only; general Curator authority remains unavailable.
- `activity`: `recorded_running`, `stale`, `idle`, or `unknown`, with bounded running/stale record counts.
- `service_state: unknown`: a saved RunRecord is not a process heartbeat.

The three agent entries always use this order: `learner`, `curator`, `connector`.
Disabled policy does not hide a previously saved active record. A successful historical run does not establish current capability.
The response includes `observed_at`, `observation_kind: stored_run_records`, and `service_reason: no_process_heartbeat`.

`recent_runs` contains at most 20 rows. Each row exposes only `agent`, `outcome`, `started_at`, and `finished_at`.
`recent_limit` is 20; `recent_has_more` reports whether the bounded read found another row.
Unknown codes become `unknown`; invalid timestamps become null. No IDs, prompts, model output, source text, account identifiers, or internal errors enter the feed.

Running counts aggregate the complete World selection before the recent-row limit.
A valid running row is stale at its stored start plus time cap plus the configured lease margin.
The read does not repair stale rows. A malformed row produces an unknown observation; a malformed aggregate refuses the response.
The three read transactions are separate observations, not an atomic work census or an authorization result.

Responses use `Cache-Control: no-store` and remain below 32 KiB. Anonymous, expired, or revoked sessions receive 401 before World projection.
Query parameters receive 400. Store failures and malformed designation/aggregate data receive a typed unavailable response, not an empty feed.
Reads use finite five-second transaction timeouts. The API never probes `/runner/due`.

The UI toggle can show this feed. It cannot start a worker. Item-follow animation, freshness percentages, and process-health claims need separate attested data.
General D7/D10/D11/D14 runtime and physical acceptance remain incomplete. No runtime factory, migration, or schema changes belong to this component.

Focused tests use synthetic read drivers and sessions. They exercise actual WorldQuery scoping, timeout selection, response projection and the HTTP dependency.
They do not establish native Cypher execution, a live process, provider operation, deployment, or real-account acceptance.
Temporal classification uses Neo4j's documented [duration.inSeconds](https://neo4j.com/docs/cypher-manual/current/functions/temporal/duration/) second component.
