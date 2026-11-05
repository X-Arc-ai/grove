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

The UI toggle can show this feed. It cannot start a worker. This feed holds no item ids and no freshness numbers. The separate worker record below holds both, from stored rows only. A process-health claim still needs separate attested data.
General D7/D10/D11/D14 runtime and physical acceptance remain incomplete. No runtime factory, migration, or schema changes belong to this component.

Focused tests use synthetic read drivers and sessions. They exercise actual WorldQuery scoping, timeout selection, response projection and the HTTP dependency.
They do not establish native Cypher execution of this feed, a live process, provider operation, deployment, or real-account acceptance.
Temporal classification uses Neo4j's documented [duration.inSeconds](https://neo4j.com/docs/cypher-manual/current/functions/temporal/duration/) second component.

## Worker record

`GET /v1/views/workers/record` gives what a person can follow on the map, and the freshness of the World.
It has the rules of the feed: session only, no query parameters, `Cache-Control: no-store`, below 32 KiB, five-second read timeouts, and a typed unavailable response on a store failure or a malformed row, never an empty record.
It reads only the session's World. It starts nothing. The `grove.worker-activity.v1` feed does not change.

The fixed `grove.worker-record.v1` response holds `observed_at`, `observation_kind: stored_change_entries`, `agents` and `freshness`. The three agent entries use the order of the feed.

- `last_run`: the agent's newest RunRecord by `started`, then `id`, both descending, or `null`. It holds only `outcome`, `started_at` and `finished_at`. The outcome uses the codes of the feed, and a malformed row reads as `unknown`.
- `trail`: the newest of the agent's newest 20 RunRecords that has a ChangeEntry of type `item` or `link` for an item that still exists, or `null`. It holds the three fields of `last_run`, and `changed_items`, `item_ids` and `has_more`.
- `changed_items`: the total of distinct items of that run that still exist. A `link` entry counts both endpoint items of that link.
- `item_ids`: at most 50 ids, by the newest ChangeEntry sequence of each item, newest first. Two endpoints of one link entry have one sequence, and their ids order them. `has_more` is true when `changed_items` is more than the ids returned.
- `freshness`: over every `:Item` of the World. `total_items` counts them. `dated_items` have a valid `touched_at`. `fresh_items` have a `touched_at` from exactly 30 days before now to now, both ends included. `window_days` is 30. `newest_change_at` and `oldest_change_at` are the newest and oldest valid `touched_at`, or `null`. A `touched_at` later than now is dated and is not fresh.

An item that is gone is dropped from the ids and the total, because each read joins the entry to the item that exists now. A forget deletes the item and its links, and it does not rewrite history. So a forgotten item never appears, and a run whose items are all gone is not a trail. A merged item keeps its row, so it stays in the ids.
The record holds no text, no titles, no run ids, no prompts and no account ids.

What the record gives, and what stays unproven:

- A trail proves that a stored ChangeEntry of that run names that item. It does not prove that a worker runs now, and it does not prove what the change said.
- Freshness says when each item row last changed. It does not say that a fact is still true, and nothing schedules a refresh.
- No process heartbeat exists, so the record makes no process-health claim. `service_state` of the feed stays `unknown`.
- `Area.last_audited_at` and `Area.dirt_score` have no writer. So there is no regional freshness and no area score. Freshness is one measure of the whole World.

The reads are separate transactions, not a common snapshot. The id page goes before the total, so a run that writes between the two reads can only raise the total. No read collects ChangeEntry rows, and freshness is one aggregate row.
Each change read first reduces the entries of a run to distinct entity ids. That step makes the store seek each item and link by id. Without it, the plan scans every link of the store for each entry.

`server/tests/views/test_worker_record_live.py` runs these reads as native Cypher against the dev store. It proves World isolation with rows of two Worlds, both link endpoints, dropped items, the choice of the trail and the 30-day edge. It does not establish a live process, provider operation, deployment, or real-account acceptance.
