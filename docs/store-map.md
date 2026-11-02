# The store map

This file maps every table of the Grove design to where it lives in Neo4j: its label or relationship type, its properties, its keys and its indexes. The spec's table and column names stay the names in code and tests. This map says which label, relationship or property holds each.

The part between the two `generated` markers comes from `server/grove/store/catalogue.py`. Never edit that part by hand. Everything before it is written by hand.

The design comes from two private maintainer documents: the architecture decisions document and the build plan. "Spec line" means a line of the architecture decisions document. Spec lines are references for maintainers, and the map reads without them. "B.1" is that document's list of every table, and A.5 and D1 to D15 are its sections. A lane is one work stream of this build, named in backticks, such as `writer-a`. S numbers are the rulings below.

## Naming

A table keeps its spec name in code and tests. Its label is the singular in PascalCase: `items` is `:Item`, `oauth_clients` is `:OAuthClient`. A spec column name becomes snake_case and hyphens drop: `became true` is `became_true`, `re-check by` is `recheck_by`. S2 has the rule in full.

## How to change the store

1. Add a new numbered file to `server/migrations/`, the next `000N_<name>.cypher`. Never edit a file that has been applied: the migration runner treats a changed checksum as an error. Every `CREATE` uses `IF NOT EXISTS`.
2. Change the row in `server/grove/store/catalogue.py`. Constraint and index names equal the names in the migration.
3. Run `uv run python -m grove.store.storemap` from `server/`, and commit the regenerated file.

`uv run python -m grove.store.storemap --check` exits 1 when this file differs from the catalogue, and a test runs it.

## Neo4j Community limits

Every row was checked on `neo4j:2026.10.1-community` (Neo4j Kernel 2026.10.1, Cypher 5 and 25) on 2026-11-01. The "current" manual pages match this version.

| Limit | Check on 2026.10.1-community | Manual |
|---|---|---|
| One user database. | Confirmed. `CREATE DATABASE` is refused with 51N27, "not supported in community edition". `SHOW DATABASES` lists `neo4j` and `system` only. | https://neo4j.com/docs/operations-manual/current/database-administration/ |
| No roles or grants. | Confirmed. `CREATE ROLE`, `GRANT ROLE`, `GRANT MATCH` and `SHOW ROLES` are refused with 51N27. Every client that connects has full power, so the application enforces the split between reads and writes. | https://neo4j.com/docs/operations-manual/current/authentication-authorization/manage-roles/ |
| No property-existence constraints, no key constraints, no property type constraints. | Confirmed. All six forms, on nodes and on relationships, are refused with 50N11 (`Neo.DatabaseError.Schema.ConstraintCreationFailed`), cause 51N27 ("Property existence constraint is not supported in community edition"). A re-probe on 2026-11-01 through the Python driver gave the same order, and the refused statement created nothing. So the writer must check required properties in code; `writer-a` builds it (S6). | https://neo4j.com/docs/cypher-manual/current/schema/constraints/ |
| Backup is an offline dump, and `STOP DATABASE` is refused. | Confirmed. `STOP DATABASE` and `START DATABASE` are refused with 51N27. `neo4j-admin database` has no `backup` command, and its dump help says a database mounted in a running server cannot be dumped. So the daily backup pauses the runner, stops the server, dumps, and starts it again. The dump itself was not run on the shared store. The manual says a dump does not hold users and roles metadata. | https://neo4j.com/docs/operations-manual/current/backup-restore/offline-backup/ and https://neo4j.com/docs/operations-manual/current/docker/dump-load/ |
| Heap and page cache are set explicitly. | Partly. The server does start without these settings and computes its own values, so this is the rule of this build, not a rule of the server; the manual only recommends it. The names and the env form are confirmed: `server.memory.heap.initial_size`, `server.memory.heap.max_size` and `server.memory.pagecache.size`, as `NEO4J_server_memory_heap_initial__size`, `NEO4J_server_memory_heap_max__size` and `NEO4J_server_memory_pagecache_size`. The dev store ran with 512 MiB heap and 256 MiB page cache. The manual's Docker page says the image defaults to 512M for both, but the image's entrypoint sets only the page cache; that comes from reading the entrypoint, not from a second container. | https://neo4j.com/docs/operations-manual/current/performance/memory-configuration/ and https://neo4j.com/docs/operations-manual/current/docker/configuration/ |

The build also relies on these facts.

| Fact | Check on 2026.10.1-community | Manual |
|---|---|---|
| Composite uniqueness ignores a row that lacks a property. | Confirmed. Two rows with `world_id` and no `id` were both accepted under a constraint on `(world_id, id)`. | https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/ |
| The empty string counts for uniqueness. | Confirmed by a live check on nodes and on relationships: a second `('', 'k')` is refused, where a null escapes. The primitive rows use it (S5). The manual page does not address it. | https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/ |
| Relationship uniqueness spans the store, not an endpoint pair. | Confirmed. A duplicate key on another end node is refused. So `active_key` carries the world and both ends (S7). | https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/ |
| Read mode refuses writes, but it is no security guarantee. | Confirmed. Data and schema writes in read access mode fail with `Neo.ClientError.Statement.AccessMode`, 42N02. The manual warns: "There is no security guarantee that a write query submitted in read mode will be rejected." With no roles, read mode is a safety rail and the writer lint is the rule. | https://neo4j.com/docs/python-manual/current/transactions/ |
| Cypher 25 must be the default, set explicitly. | Confirmed. The built-in default of `db.query.default_language` is `CYPHER_5`. The image's `neo4j.conf` sets `CYPHER_25`, and a custom config without that line would drop to 5, so the compose file sets it and the query layer prefixes `CYPHER 25`. `SHOW CONSTRAINTS` gives different `type` values under the two versions. | https://neo4j.com/docs/operations-manual/current/configuration/cypher-version-configuration/ |
| The `SEARCH` clause replaces the full-text procedures. | Confirmed. `db.index.fulltext.queryNodes` and `queryRelationships` run, with a deprecation notice under Cypher 25. `SEARCH` works on Community and only under Cypher 25. | https://neo4j.com/docs/cypher-manual/current/indexes/search-indexes/full-text-indexes/ |
| `SEARCH` filters after `LIMIT`. | Confirmed. A `WHERE` or a label in `MATCH` runs after the top rows are cut, so a filter there can return too few rows. The world filter sits inside the Lucene query (S17). | https://neo4j.com/docs/cypher-manual/current/clauses/search/ |
| `IF NOT EXISTS` no-ops under another name. | Confirmed. When the same schema exists under another name, the statement succeeds, creates nothing and returns only a notification. So a test compares names through `SHOW CONSTRAINTS` and `SHOW INDEXES`. | https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/ |
| Transaction memory has a cap. | Confirmed. `dbms.memory.transaction.total.max` is 358.40 MiB on the dev store, so large batches must stay under it. | https://neo4j.com/docs/operations-manual/current/performance/memory-configuration/ |

## What A.5 asks, what Neo4j gives

A.5 sets Postgres as the default and accepts another store with a recorded reason, if it gives the same things. S1 records the reason, and this table is the check of the guarantees.

A filter in `MATCH` or `WHERE` reads with no lock. A probe raced two transactions on one row: both passed the same filter, and the second overwrote the first. A `SET` takes the lock on a row, so a check must run after a lock, in one of two tested forms:

1. First write a different property of the row, which takes the lock, then `WITH`, then the check, then the change. The lock write must not touch the checked property. If the check fails, the lock write stays on the row unless the transaction rolls back. So the caller rolls back on a failed check, or the lock write uses a property whose new value does no harm.
2. Put the check inside a `CASE` on the right side of `SET`, and let the caller compare the returned value, for example the returned owner of a claim.

| A.5 asks for | Neo4j gives |
|---|---|
| A batch that is all or nothing | A write transaction. |
| Optimistic concurrency | A version check in one of the two forms above. `writer-a` must build it, with a two-transaction race test. |
| Hops and the walk | Variable-length patterns on `:LINK`. |
| Full text and trigram match | Full-text indexes, and fuzzy `~` in the query. Neo4j has no trigram similarity, so the trigram thresholds must be computed in code (S17). |
| Append-only history | `:ChangeEntry`, under the writer rule. |
| JSON columns | JSON strings. A map cannot be a property (S16). |
| The job queue | `:Job`. A claim checks the state in one of the two forms above. There is no `SKIP LOCKED`. `runner` must build the claim, with a two-transaction race test. |
| Row-level security | The query layer. No store rule exists. |
| Backups | A whole-store offline dump each day with the server stopped, kept 30 days. A.5 asks for snapshots of the snapshot-only tables; the whole-store dump is a change from A.5 (S21). |
| Restore | `neo4j-admin database load` into a stopped store, then rebuild the projections from history, replay the forget ledger and compare fingerprints. `deploy-a` runs the dump and its load into a scratch store each day, with a matching dump fingerprint (`docs/deploy.md`). The rebuild, the replay and D3's fingerprint belong to `writer-b` and have not run. |

## Rulings

A ruling states the design. Where the code does not exist yet, the named owner lane builds it.

**S1. The store** (spec line 3065). A.5 asks a different store for a recorded reason and the same guarantees. The reason: one Community store holds the graph, history, requests, jobs, model calls, sessions and OAuth clients, and a graph store fits items, links and their walks. The table "What A.5 asks, what Neo4j gives" above is the check of the guarantees. S21 records that the whole-store dump is a change from A.5.

**S2. Names** (spec lines 3081, 2098). Spec column names become snake_case, and hyphens drop: `became true` is `became_true`, `re-check by` is `recheck_by`, `normalised phrase` is `normalised_phrase`. A table keeps its spec name in code and tests. Its label is the singular in PascalCase (`items` is `:Item`, `oauth_clients` is `:OAuthClient`, `ontology_change_log` is `:OntologyChange`, `horizon_stats` is `:HorizonStat`, `support_changed` is `:SupportChanged`).

**S3. Clocks** (spec lines 902, 903, 914, 915, 981, 1140, 3081). `became true` and `stopped being true` are world clocks: `<name>_earliest`, `<name>_latest` (ZONED DATETIME, UTC) and `<name>_precision` (enum `precision`). "Unknown" is `became_true_precision = 'unknown'` with both bounds absent. `stopped_being_true_*` are absent while the claim holds. `recorded`, `last_confirmed` and `recheck_by` are single instants. The spec gives no precision list, only `before` at line 1140, so the build fixes one in `enums.py`: instant, minute, hour, day, week, month, quarter, year, before, after, unknown. For lines 914 and 915, evidence adds `source_offset_minutes` and `person_timezone` (an IANA code), and items and links add `became_true_words` and `stopped_being_true_words` (value ids) for the original words.

**S4. Ids** (spec lines 736, 3081). Every id column holds UUIDv7 text from `new_id()`. The exceptions are `requests.id` (S9), the fixed primitive ids, `sequence`, and `oauth_clients.client_id`, which is a `locator` (S32). A subject reference of type `ref` is not an id (S32). A receipt prints a short form; the `writer-a` lane picks the length and resolves it.

**S5. `world_id`** (spec lines 3081, 3089, 3102). `world_id` is on every row, also `declines`, `area_audits` and the four D13 tables, because line 3081 wins. Empty for primitive rows means the sentinel `''` (`PRIMITIVE_WORLD`), not null: null escapes a composite uniqueness constraint, and `''` does not. Store-wide rows are the primitive rows and the default capabilities. `checks` rows are per world (S35). Every other `world_id` has the form of S36, which `check_world_id` enforces; the sentinel `''` reaches a read only through `WorldQuery.primitives`. Platform nodes carry `user_id`. `:OAuthClient` and `:OAuthToken` carry `world_id` too, and on them it may be absent until consent.

**S6. Keys** (spec lines 3088 to 3092, 3104, 3105, 3130). A single-property uniqueness constraint is enough for an id. Each B.1 non-id key is a composite uniqueness constraint, with `world_id` first where the table has it. A composite constraint ignores a row that lacks a property, and Community has no existence constraints, so the writer must check required properties in code. `writer-a` builds that check.

**S7. Links** (spec lines 1270, 1562, 1612 to 1614, 3088, 3130). Links are one relationship type, `:LINK`, from item to item. The meaning is a property that holds the meaning id, never a relationship type: a rename or a merge changes a registry row, and names stay out of code. `from_item` and `to_item` repeat the endpoints for index use. A merge re-points a link by delete and create with the same id. B.1's `unique active (world_id, from item, meaning, to item)` is `active_key`, text of the form `world|from|meaning|to`, present only while the link is active: a uniqueness constraint ignores an absent property, and the rule spans the store, so the key carries the world. A mark is a `:LINK` of the primitive meaning `affected by` from the affected item to the trigger item, plus one `:Impact` keyed by `link_id`. A mark's `active_key` adds its trigger change set, so a second change set on the same item can mark it again. This settles the conflict between lines 3088 and 3130.

**S8. The original phrase** (spec lines 1596, 2522, 2855, 3255). The original phrase of a generic link is `original_phrase`, a value id on `:LINK`. The free-text rule leaves no other home. "Link notes" at line 2522 is not a column.

**S9. Request ids** (spec lines 763, 3116, 3195). `requests.id` is the caller's idempotency key, so it is not globally unique: uniqueness is `(world_id, id)`. `request_id` of B.3 is this id.

**S10. Sequence** (spec lines 736, 3114, 3133, 3134). `change_entries.sequence` is per world, unique on `(world_id, sequence)`, and allocated by `SET w.next_sequence = w.next_sequence + 1` on the `:World` node inside the writer's transaction. That one direct `SET` holds the lock until commit, so sequence order is commit order and a rollback leaves no gap. A per-store counter would serialise every world on one lock. "Change id" in `contradictions` and `support_changed` means this sequence. `commit id` and `change set id` are UUIDv7, and one commit may hold several change sets.

**S11. Status role** (spec lines 648, 3087, 3089). `status_role` on `:Item` (enum `status_role`) is written with `status`, because the index `(world_id, kind, status role)` needs it and the role lives on the kind row.

**S12. Version and touched-at** (spec lines 922, 3081). `version` and `touched_at` are on every row the writer updates in place, also where B.1's list leaves them out: links, kinds, meanings, property_names, aliases, capabilities, evidence, beliefs, holds, unresolved_mentions, contradictions, questions, impacts, cascade_proposals, connectors, areas, findings, and also episodes and sources (spec lines 761, 2525, 3103). Append-only rows have neither. Other rows that change after they are written are an open point.

**S13. Belief ids** (spec lines 1742, 3105, 3132). A belief's id is its item's id. Evidence that targets a belief uses `target_type = belief` and `target_id = <item id>`.

**S14. Contradictions** (spec lines 1841, 3133, 3176). `fresh_evidence_id` may be absent when the two judges disagree. Then `fresh_source_id` and `fresh_locator` say what was read.

**S15. Evidence end reason** (spec lines 3105, 3166). `evidence.end_reason` is the closed enum `evidence_end_reason` (correction, forget, merge), not free text.

**S16. JSON and lists** (spec lines 3087, 3119). A map cannot be a property. `items.properties` and `run_records.proposed_operations` are JSON strings. `items.property_values` is the derived `LIST<STRING>` that the full-text index reads. Lists are homogeneous and never hold null. An empty list is stored as an absent property, because the empty list has the type `LIST<NOTHING>`.

**S17. Lexical search** (spec lines 1386, 1387, 2560, 3087, 3092, 3280). `grove_items_lexical` runs over `:Item` (`world_id`, `title`, `summary`, `body`, `property_values`, `search_terms`). `grove_aliases_lexical` runs over `:Alias` (`world_id`, `phrase`, `normalised_phrase`). `grove_past_values` runs over `:PastValue` (`world_id`, `value`). `grove_value_index` runs over `:Item|Alias|HistoryValue|Kind|Meaning|PropertyName|RunRecord` (`world_id` and every text property that line 2560 names). All use the analyzer `standard-folding`, which folds accents and case and drops 33 English stop words. The text indexes `items_title_text` on `:Item(title)` and `aliases_normalised_phrase_text` on `:Alias(normalised_phrase)` serve substring match. Neo4j has no trigram similarity: typo tolerance is full-text fuzzy `~`, and Appendix C's trigram thresholds (0.3, 0.8) must be computed in code over the candidates. `hosts-b` builds that for the lexical index and `ingest-b` for the deduplication ladder. Full-text queries use the `SEARCH` clause. Because `SEARCH` filters after `LIMIT`, the world filter sits inside the Lucene query: `+world_id:"<id>" +(<terms>)`. A probe on 2026-11-01 proved it: a world with 1 hit among 60 hits of another world came back with `LIMIT 5`. The analyzer splits the phrase on hyphens and folds its case, so the phrase is exact only because of the world id form of S36, and `WHERE n.world_id = $world_id` stays as the exact check. Search text is capped at 1024 characters, 64 characters a term and 64 analyzer tokens, and text that the analyzer folds to stop words or drops returns no rows.

**S18. Past values** (spec lines 834, 1387). A `:HistoryValue` that holds an old value of an item's content column also has the label `:PastValue` and the property `item_id`. No text is copied, so a forget that replaces the value clears the index.

**S19. The value index and sources** (spec lines 2525, 2560). The value index leaves out `sources`, as line 2560 does. A forget reaches a source span by its locator. So a value the person names inside source text has no index path; see the open points.

**S20. Writes outside the agents** (spec lines 2501, 3120, 3121). `model_calls` and `judgments` are written only by the model-call layer, sessions and OAuth rows by sign-in, job claims and budgets by the runner. Each such write must be a function in `server/grove/writer/`, and only its owner calls it. `models`, `signin`, `hosts-a` and `runner` build them. The writer rule stands: only `server/grove/writer/` opens write transactions, and the writer lint enforces it.

**S21. Restore groups** (spec lines 806 to 808). Projection: items, links, evidence, kinds, meanings, property_names, aliases, capabilities, beliefs. Snapshot: the 32 others. Ledger: forget_ledger. Platform rows restore as they were, and tokens are stored hashed. A snapshot is the whole-store offline dump, and the rebuild replaces the projection rows after the load. The catalogue records the group per table.

**S22. Seeded rows** (spec lines 802, 806, 3222). Seeded registry rows are projections, so `create_world` writes one change entry per seeded row (`entity_type = registry_row`, `field = row`, `new_value_id` = a history value that holds the JSON `{"table": ..., "row": ...}`, `actor = seed`). Primitive rows are fixed in code and are not rebuilt from history.

**S23. The forget ledger** (spec lines 808, 3147). `forget_ledger` is `:ForgetLedger` in the store. Its copy outside the dump is the job of the `forget` lane.

**S24. Days and voice** (spec lines 2684, 3120, 3122). `model_calls.day` and `budgets.day` are DATE values taken from the clock provider. Voice calls add `audio_seconds`. The price table is a config file owned by the `models` lane.

**S25. Additions** (spec lines 832, 1238, 1250, 1252, 1264, 1265, 1596, 1630, 1752, 2004, 2067, 2105, 2161, 2376, 2427, 2450, 2527, 3197). The spec needs these columns and B.1 leaves them out, so each has origin `ruling`: `impacts.affected_item`, `impacts.flag`; `cascade_proposals.proposal_type` (enum: cascade, cascade_failed, wide_impact, reopen) and `cascade_proposals.detail` (JSON of ids, codes and counts); `kinds.propagating_fields`; `meanings.generic` (lets code find the generic connection meaning by a declared field); `property_names.scheduled_role` (enum start, due, end); `audit_runs.invalid`; `audit_grades.rationale` (value id); `findings.declined_reason` (value id); `questions.unblocks` (ids) and `questions.withdraw_reason` (value id); `unresolved_mentions.expiry_reason` (code); `beliefs.end_class` (enum `belief_end_class`).

**S26. Registry columns** (spec lines 1572, 1573, 3090, 3092). `meanings` carries the registry columns of `kinds` except lifecycle class, status vocabulary, required fields and required links, which do not fit a meaning. `aliases` adds `status`, `origin`, `minted_at` and `minted_by`. `reversed` is false on non-meaning aliases.

**S27. Connector types** (spec lines 2561, 3046, 3101, 3108). Type-level connector fields (parser, locator format, source-type mapping, speaker rules) are developer config in code, not rows. `screening_receipts.screened_where` has one reachable value, `server`: there is no local runner.

**S28. Area membership** (spec lines 1368, 2145, 3095). `areas` membership has no column in B.1. It stays open for the `cleanup` and `hosts-b` lanes; see the open points.

**S29. Checks and controls** (spec lines 2376, 2424, 2425, 2561). The wording of `checks` (question, measure, cadence, bands) is developer config, exempt from the value-id rule, and is repeated on each world's row (S35). `audit_runs.controls_planted` is a count, and the planted controls stay with the harness.

**S30. Primitive rows declare their fields** (spec lines 650, 1270, 1757, 1763, 3257). The four primitive meanings all have `direction` `from_to`, `cardinality` `many`, `carries_consequences` false, `decomposition` false, `rolls_up_completion` false, `generic` false and `status` `canonical`, and no `downstream_end` and no `default_horizon`. Endpoints use the seed's JSON form. `describes` goes from `{"kinds": [<id of the belief kind>], "classes": []}` to `{"any": true}`, with temporal class `permanent` (line 1757). `superseded by` and `corrected by` go from any to any, with temporal class `permanent`. `affected by` goes from any to any, from the affected item to the trigger item (line 1270), with temporal class `event`. The belief kind has `lifecycle_class` `lasting_thing`, an empty `status_vocabulary`, empty `required_fields`, `required_links` `[<id of describes>]` (line 1763), empty `propagating_fields`, and `status` `canonical`. The spec states no cardinality or temporal class for the last three meanings. `many` is chosen because it refuses nothing that the writer's own operations allow. The seed writes these fields when it creates a row and also when it finds one, so a change in code reaches a store that holds older rows.

**S31. A status word is a registry name** (spec lines 648, 2092, 2561). A kind's status vocabulary is its own, and an agent can mint a new status word. So a status word is neither a closed enumeration nor developer configuration. Line 2561 lists the registry's names as a home of free text, so `items.status` is `text_home` and `kinds.status_vocabulary` is `json_home`. The value index must gain both; `forget` owns the value index and adds them (see the open points). Until then, a forget of a status word renames the word in the kind.

**S32. Subject references and client ids** (spec lines 1594, 1667, 2426, 3036, 3121, 3148). The type class `ref` is text of the form `<target type>:<id>`. A pair is the two references in sorted order, joined by `|`. A word that has no row yet is `value:<history value id>`, where the history value holds the proposed name. A source event is `event:<connector id>:<message key>`. `judgments.subject` and `audit_grades.subject` are `ref`. `oauth_clients.client_id` is `locator` and is an exception to S4: dynamic registration gives a UUIDv7, and a Client ID Metadata Document gives its URL.

**S33. "Not supported" is not a row verdict** (spec lines 942, 996, 2157, 2627). The verify judge's closed list for a row is `supports`, `contradicts` and `not_about_claim`. When the judge finds that a row does not support the claim, the row gets `contradicts` if it speaks against the claim, and `not_about_claim` if it does not speak to it. The claim is "not supported" when no row supports it. `truth-clock` owns any change.

**S34. No script folding** (spec line 1386). Line 1386 says aliases are normalised for case, accents and script. `normalise_phrase` folds case, accents, underscores, hyphens, punctuation and spacing, but not script: `Москва` and `Moskva` stay different phrases. Transliteration needs a language model or a table that this build does not have. `writer-a` owns the resolver and any change, and the resolver must refuse an empty phrase.

**S35. Checks are per world** (spec lines 2398, 2424, 3081, 3202). Line 3081 puts `world_id` on every table, and a check's state and value measure one world. So there is one `checks` row per check per world, with the real `world_id`. The wording (the person's question, measure, cadence, bands) is developer configuration, repeated on each row. This replaces the store-wide reading of `checks` in S5 and S29.

**S36. The world id form** (spec lines 3081, 3087, 3092). A world id is a lowercase UUID (`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`), or the test form `t_<slice>_<that uuid>` with the slice `[a-z0-9-]+`. `check_world_id` refuses any other text, and the primitive sentinel `''` stays a separate path. The full-text analyzer splits on hyphens and folds case, and with this form the token sequence of one world id never occurs inside another's unless both share the UUID. So the world phrase of S17 is exact in practice, and one world's rows cannot use up another world's `LIMIT`. This replaces the earlier pattern `[A-Za-z0-9_-]{1,80}`, under which `t_x_<h>` was a token prefix of `t_x_<h>-000`.

## Open points for later lanes

| Point | Spec lines | Owner lane |
|---|---|---|
| How an item's area membership is stored. | 1368, 2145, 3095 | `cleanup`, `hosts-b` |
| A value the person names inside source text has no index path. | 2518, 2525, 2560 | `forget` |
| Which bound orders `latest`, and where "unknown" sorts. Both bounds have an index. | 1390, 1487 | `hosts-b` |
| The names of the verification bands. `verifications.band` is a code until then. | 957 | `truth-clock` |
| The stakes value of an R2 mark, and a mark's clocks. | 1247, 1254, 1270 | `walk` |
| Where a search term's type and language live. `items.search_terms` holds the text of each term. | 2076, 2098 | `ingest-b` |
| A stored receipt holds ids and codes, because a title in it would be a copy no forget reaches. The writer reads titles fresh when it shows a receipt. | 733, 737, 2529 | `writer-a` |
| The deferral count: stored, or derived from `verifications`. | 2159, 2174 | `cleanup` |
| The value lists of `property_names.value_type` and `meanings.direction`. | 1583, 3241 | `ontology` |
| A request row needs its source id before screening has finished. | 750, 2462, 2505 | `screening` |
| The grade rows of controls, and findings raised by agent runs: their run id and their text. | 2198, 2376, 2427 | `evals`, `cleanup` |
| The newest-id part of the fingerprint for a table with no id, and whether a rebuild keeps `touched_at`. | 812, 922 | `writer-b` |
| Rows outside the S12 list that change after they are written carry no version: `requests`, `jobs`, `support_changed.taken_at`, `ontology_change_log.undone_by`, `history_values.forgotten_at`, `budgets.spent`, `horizon_stats`, `walk_rotation`, `checks`, `run_records`, and the finished times of `audit_runs` and `area_audits`. | 3081 | `writer-a` |
| The value index must gain the status words, `items.status` and `kinds.status_vocabulary` (S31). Until then, a forget of a status word renames the word in the kind. | 2560, 2561 | `forget` |
| `why` of a contradiction from `grove.cleanup.v1` (the `grove.extract.v1` form) has no value-id column in `contradictions`. | 2087, 2199, 3133 | `beliefs` |
| Migrations run from the checkout's `server/` folder, never from the installed package: the wheel does not hold them. Closed by `deploy-a`: the API image holds `server/` as in a checkout with an editable install, and a deploy runs the migrations from it (`docs/deploy.md`). | none; a build fact | `deploy-a` |
| `GROVE_ENV=production` must be set on the box, or the guard that refuses a test clock does not run. Closed by `deploy-a`: `deploy/compose.yml` sets it for `api` and `runner`, and a test checks it. | 354, 991 | `deploy-a` |
| D3's restore has not run on this store: rebuild the projections from history, replay the forget ledger, compare D3's fingerprint. The daily dump and its load into a scratch store run on the box since `deploy-a`, checked by the dump fingerprint (`docs/deploy.md`). | 806 to 808, 3065 | `writer-b` |
| `docs/data-flow.md` has no rows for the mail provider, Google sign-in, Calendar and backups. They must be added before the privacy page. | 2540 to 2552 | `forget` |

## Added by later lanes

These are not in this slice. Each owner lane adds its rows here and in the catalogue.

| Rows | Owner lane |
|---|---|
| Map positions per item | `views-a` |
| My Grove guidance | `views-b` |
| Pending exports | `views-b` |
| Eval scripts and their results | `evals` |

<!-- generated:begin -->
## Type classes

| Class | Meaning |
|---|---|
| `id` | UUIDv7 text from new_id(), with the exceptions of S4 |
| `ids` | a list of ids |
| `value_id` | the id of a history_values row that holds the text |
| `value_ids` | a list of value ids |
| `ref` | S32: a subject reference, `<target type>:<id>`; a pair is two references, sorted, joined by a bar |
| `text_home` | free text in one of the homes of line 2561 |
| `json_home` | a JSON string in one of the homes of line 2561 |
| `json_ids` | a JSON string that holds only ids, codes and numbers |
| `enum:<name>` | a code of the closed list of that name in enums.py |
| `instant` | a ZONED DATETIME in UTC |
| `date` | a DATE from the clock provider |
| `int` | an integer |
| `float` | a float |
| `bool` | a boolean |
| `locator` | a place in a source, or a URL |
| `code` | a machine code, not free text |
| `config` | developer-written configuration |

Text homes, the only columns of type `text_home` or `json_home` (line 2561, and the status words of S31): `items.title`, `items.summary`, `items.body`, `items.properties`, `items.property_values`, `items.search_terms`, `items.status`, `kinds.name`, `kinds.status_vocabulary`, `meanings.name`, `property_names.name`, `aliases.phrase`, `aliases.normalised_phrase`, `sources.content`, `history_values.value`, `run_records.proposed_operations`.

## The graph and its registry

### items

`:Item`, node. Scope world. Restore group projection. Owner D1, D4, D5.

Keys: `items_id_unique` (id).

Indexes: `items_world_id_kind_status_role_idx` (world_id, kind, status_role); `items_world_id_became_true_earliest_idx` (world_id, became_true_earliest); `items_world_id_became_true_latest_idx` (world_id, became_true_latest); `items_world_id_recorded_idx` (world_id, recorded); `items_title_text` (title) text.

Full text: `grove_items_lexical`, `grove_value_index`.

B.1's (world_id, became true) is two indexes, one per bound: a range index leaves out a row without the property, and a precision of before has no earliest bound. Full text and trigram are S17. Scheduled times live in properties (line 920).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| kind | `kind` | `id` | b1 | a kind id |
| title | `title` | `text_home` | b1 |  |
| summary | `summary` | `text_home` | b1 |  |
| body | `body` | `text_home` | b1 |  |
| properties | `properties` | `json_home` | b1 | a JSON string (S16); a state property only on a record, one per record |
| (none) | `property_values` | `text_home` | ruling | S16: LIST<STRING> derived from properties, read by the full-text indexes |
| search terms | `search_terms` | `text_home` | b1 | LIST<STRING>, the term text only (D7, D10) |
| status | `status` | `text_home` | b1 | S31: a status word of the kind's vocabulary, a registry name an agent can mint |
| status role | `status_role` | `enum:status_role` | ruling | S11: written with status; B.1 names it only in the index |
| status since | `status_since` | `instant` | b1 |  |
| became true | `became_true_earliest`, `became_true_latest` | `instant` | b1 | S3: UTC bounds; absent while unknown |
| became true | `became_true_precision` | `enum:precision` | b1 | S3 |
| (none) | `became_true_words` | `value_id` | ruling | S3, line 915: the original words |
| stopped being true | `stopped_being_true_earliest`, `stopped_being_true_latest` | `instant` | b1 | S3: UTC bounds; absent while unknown |
| stopped being true | `stopped_being_true_precision` | `enum:precision` | b1 | S3 |
| (none) | `stopped_being_true_words` | `value_id` | ruling | S3, line 915: the original words |
| end class | `end_class` | `enum:end_class` | b1 |  |
| recorded | `recorded` | `instant` | b1 | set by the writer at commit |
| last confirmed | `last_confirmed` | `instant` | b1 |  |
| confirmed against | `confirmed_against` | `id` | b1 | a verification id |
| re-check by | `recheck_by` | `instant` | b1 |  |
| re-check how | `recheck_how` | `json_ids` | b1 | connector, locator or query, what to compare; or the code ask_the_person |
| derived source type | `derived_source_type` | `enum:source_type` | b1 |  |
| derivation | `derivation` | `enum:derivation` | b1 |  |
| speaker | `speaker` | `enum:speaker_kind` | b1 | of the evidence that sets the source type |
| modality | `modality` | `enum:modality` | b1 |  |
| provenance | `provenance_evidence_id` | `id` | b1 | the creating evidence id |
| provenance | `provenance_reason` | `value_id` | b1 | the reason |
| version | `version` | `int` | b1 |  |
| touched at | `touched_at` | `instant` | b1 |  |

### links

`()-[:LINK]-()`, relationship. Scope world. Restore group projection. Owner D1, D3, D4, D6.

Keys: `links_id_unique` (id); `links_active_key_unique` (active_key).

Indexes: `links_world_id_from_item_meaning_idx` (world_id, from_item, meaning); `links_world_id_to_item_meaning_idx` (world_id, to_item, meaning).

unique active (world_id, from item, meaning, to item) is active_key (S7). Neo4j has no partial index, so the two range indexes also hold ended links, and a query for active links filters on active_key IS NOT NULL. A mark is a link of the primitive meaning affected by, plus one impacts row.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| from item | `from_item` | `id` | b1 | repeats the start node for index use (S7) |
| meaning | `meaning` | `id` | b1 | a meaning id, never a relationship type (S7) |
| to item | `to_item` | `id` | b1 | repeats the end node for index use (S7) |
| became true | `became_true_earliest`, `became_true_latest` | `instant` | b1 | S3: UTC bounds; absent while unknown |
| became true | `became_true_precision` | `enum:precision` | b1 | S3 |
| (none) | `became_true_words` | `value_id` | ruling | S3, line 915: the original words |
| stopped being true | `stopped_being_true_earliest`, `stopped_being_true_latest` | `instant` | b1 | S3: UTC bounds; absent while unknown |
| stopped being true | `stopped_being_true_precision` | `enum:precision` | b1 | S3 |
| (none) | `stopped_being_true_words` | `value_id` | ruling | S3, line 915: the original words |
| end class | `end_class` | `enum:end_class` | b1 |  |
| recorded | `recorded` | `instant` | b1 | set by the writer at commit |
| last confirmed | `last_confirmed` | `instant` | b1 |  |
| confirmed against | `confirmed_against` | `id` | b1 | a verification id |
| re-check by | `recheck_by` | `instant` | b1 |  |
| re-check how | `recheck_how` | `json_ids` | b1 | connector, locator or query, what to compare; or the code ask_the_person |
| derived source type | `derived_source_type` | `enum:source_type` | b1 |  |
| derivation | `derivation` | `enum:derivation` | b1 |  |
| provenance | `provenance_evidence_id` | `id` | b1 | the creating evidence id |
| provenance | `provenance_reason` | `value_id` | b1 | the reason |
| version | `version` | `int` | b1 |  |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |
| (none) | `active_key` | `code` | ruling | S7: world, from, meaning, to joined by a bar; present only while active; a mark adds its trigger change set |
| (none) | `original_phrase` | `value_id` | ruling | S8: the phrase of a generic link |

### kinds

`:Kind`, node. Scope world. Restore group projection. Owner D1, D8.

Keys: `kinds_id_unique` (id); `kinds_world_id_name_unique` (world_id, name).

Indexes: none.

Full text: `grove_value_index`.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 | '' for primitive rows (S5) |
| name | `name` | `text_home` | b1 |  |
| description | `description` | `value_id` | b1 |  |
| lifecycle class | `lifecycle_class` | `enum:lifecycle_class` | b1 |  |
| status vocabulary with each status's role | `status_vocabulary` | `json_home` | b1 | S31: ordered list of {status, role}; each status word is a registry name |
| required fields | `required_fields` | `json_ids` | b1 |  |
| required links | `required_links` | `json_ids` | b1 |  |
| status | `status` | `enum:registry_status` | b1 | starter rows are canonical at seed (B.4) |
| origin | `origin` | `enum:registry_origin` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| worked example | `worked_example` | `id` | b1 | an evidence id |
| nearest neighbours at mint | `nearest_neighbours_at_mint` | `json_ids` | b1 | word ids, each stated distinction a value id |
| minted at | `minted_at` | `instant` | b1 |  |
| minted by | `minted_by` | `code` | b1 | the agent, the person or the seed (line 1579) |
| distinct episodes | `distinct_episodes` | `int` | b1 |  |
| uses | `uses` | `int` | b1 |  |
| first used | `first_used` | `instant` | b1 |  |
| last used | `last_used` | `instant` | b1 |  |
| primitive | `primitive` | `bool` | b1 |  |
| version | `version` | `int` | b1 |  |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |
| (none) | `propagating_fields` | `json_ids` | ruling | S25, line 1238: date fields, each with a role of start, due or end |

### meanings

`:Meaning`, node. Scope world. Restore group projection. Owner D1, D6, D8.

Keys: `meanings_id_unique` (id); `meanings_world_id_name_unique` (world_id, name).

Indexes: none.

Full text: `grove_value_index`.

S26: lifecycle class, status vocabulary, required fields, required links stay on kinds.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 | '' for primitive rows (S5) |
| name | `name` | `text_home` | b1 | a registry column of kinds (S26) |
| description | `description` | `value_id` | b1 | a registry column of kinds (S26) |
| status | `status` | `enum:registry_status` | b1 | starter rows are canonical at seed (B.4) |
| origin | `origin` | `enum:registry_origin` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| worked example | `worked_example` | `id` | b1 | an evidence id |
| nearest neighbours at mint | `nearest_neighbours_at_mint` | `json_ids` | b1 | word ids, each stated distinction a value id |
| minted at | `minted_at` | `instant` | b1 |  |
| minted by | `minted_by` | `code` | b1 | the agent, the person or the seed (line 1579) |
| distinct episodes | `distinct_episodes` | `int` | b1 |  |
| uses | `uses` | `int` | b1 |  |
| first used | `first_used` | `instant` | b1 |  |
| last used | `last_used` | `instant` | b1 |  |
| primitive | `primitive` | `bool` | b1 |  |
| version | `version` | `int` | b1 |  |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |
| from kinds | `from_kinds` | `json_ids` | b1 | kind ids, a lifecycle class or any |
| to kinds | `to_kinds` | `json_ids` | b1 | kind ids, a lifecycle class or any |
| direction | `direction` | `code` | b1 | line 3241: from the first endpoint to the second |
| cardinality and group | `cardinality` | `enum:cardinality` | b1 |  |
| cardinality and group | `cardinality_group` | `code` | b1 | absent on every starter meaning |
| carries consequences | `carries_consequences` | `bool` | b1 |  |
| downstream end | `downstream_end` | `enum:downstream_end` | b1 |  |
| decomposition | `decomposition` | `bool` | b1 |  |
| rolls up completion | `rolls_up_completion` | `bool` | b1 |  |
| temporal class | `temporal_class` | `enum:temporal_class` | b1 |  |
| default horizon | `default_horizon` | `int` | b1 | days; absent when none |
| (none) | `generic` | `bool` | ruling | S25: lets code find the generic connection meaning (lines 1596, 1630) |

### property_names

`:PropertyName`, node. Scope world. Restore group projection. Owner D8.

Keys: `property_names_id_unique` (id); `property_names_world_id_kind_name_unique` (world_id, kind, name).

Indexes: none.

Full text: `grove_value_index`.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| kind | `kind` | `id` | b1 | a kind id |
| name | `name` | `text_home` | b1 |  |
| value type | `value_type` | `code` | b1 |  |
| temporal class | `temporal_class` | `enum:temporal_class` | b1 |  |
| default horizon | `default_horizon` | `int` | b1 | days; absent when none |
| description | `description` | `value_id` | b1 |  |
| status | `status` | `enum:registry_status` | b1 |  |
| origin | `origin` | `enum:registry_origin` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| worked example | `worked_example` | `id` | b1 | an evidence id |
| counts | `distinct_episodes`, `uses` | `int` | b1 | line 1580 |
| counts | `first_used`, `last_used` | `instant` | b1 | line 1580 |
| minted at | `minted_at` | `instant` | convention | line 1579: on all registry rows |
| minted by | `minted_by` | `code` | convention | line 1579: on all registry rows |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |
| (none) | `scheduled_role` | `enum:scheduled_role` | ruling | S25, lines 832, 2067: marks a property as a scheduled time |

### aliases

`:Alias`, node. Scope world. Restore group projection. Owner D1, D7, D8.

Keys: `aliases_id_unique` (id); `aliases_world_id_normalised_phrase_target_type_target_id_unique` (world_id, normalised_phrase, target_type, target_id).

Indexes: `aliases_normalised_phrase_text` (normalised_phrase) text.

Full text: `grove_aliases_lexical`, `grove_value_index`.

B.1's target is the pair target_type, target_id. Trigram on the phrase is S17.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| phrase | `phrase` | `text_home` | b1 |  |
| normalised phrase | `normalised_phrase` | `text_home` | b1 |  |
| target type | `target_type` | `enum:alias_target_type` | b1 |  |
| target id | `target_id` | `id` | b1 |  |
| reversed | `reversed` | `bool` | b1 | S26: false on non-meaning aliases; never defaulted |
| valid from and to | `valid_from`, `valid_to` | `instant` | b1 | role aliases (D7) |
| worked example | `worked_example` | `id` | b1 | an evidence id |
| added at | `added_at` | `instant` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| (none) | `status` | `enum:registry_status` | ruling | S26, line 1572 |
| (none) | `origin` | `enum:registry_origin` | ruling | S26, line 1573 |
| (none) | `minted_at` | `instant` | ruling | S26, line 1579 |
| (none) | `minted_by` | `code` | ruling | S26, line 1579 |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### ontology_change_log

`:OntologyChange`, node. Scope world. Restore group snapshot. Owner D8.

Keys: `ontology_change_log_id_unique` (id).

Indexes: `ontology_change_log_world_id_at_idx` (world_id, at).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| operation | `operation` | `enum:ontology_operation` | b1 |  |
| rows before and after | `rows_before`, `rows_after` | `json_ids` | b1 | ids and field values of registry rows, text fields as value ids |
| ids re-pointed | `ids_repointed` | `ids` | b1 |  |
| inverse operation | `inverse_operation` | `json_ids` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| evidence ids | `evidence_ids` | `ids` | b1 |  |
| judgment ids | `judgment_ids` | `ids` | b1 | the two-model verdicts the operation cites |
| actor | `actor` | `code` | b1 |  |
| at | `at` | `instant` | b1 |  |
| undone by | `undone_by` | `id` | b1 |  |

### distinct_pairs

`:DistinctPair`, node. Scope world. Restore group snapshot. Owner D2, D10.

Keys: `distinct_pairs_world_id_item_a_item_b_unique` (world_id, item_a, item_b).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | b1 |  |
| item a | `item_a` | `id` | b1 |  |
| item b | `item_b` | `id` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| evidence id | `evidence_id` | `id` | b1 |  |
| recorded at | `recorded_at` | `instant` | b1 |  |
| recorded by | `recorded_by` | `code` | b1 |  |

### areas

`:Area`, node. Scope world. Restore group snapshot. Owner D8, D11.

Keys: `areas_id_unique` (id).

Indexes: `areas_world_id_hub_item_idx` (world_id, hub_item); `areas_world_id_dirt_score_idx` (world_id, dirt_score); `areas_world_id_last_audited_at_idx` (world_id, last_audited_at).

S28: B.1 has no membership column. The map lists this as an open point.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| hub item | `hub_item` | `id` | b1 | several areas can share a hub (line 2145) |
| partition rule | `partition_rule` | `json_ids` | b1 | meaning ids or second-level hub ids |
| status | `status` | `enum:area_status` | b1 |  |
| merged into | `merged_into` | `id` | b1 |  |
| member count | `member_count` | `int` | b1 |  |
| packet size in tokens | `packet_size_in_tokens` | `int` | b1 |  |
| dirt score | `dirt_score` | `int` | b1 |  |
| last audited at | `last_audited_at` | `instant` | b1 |  |
| audit count | `audit_count` | `int` | b1 |  |
| never audited | `never_audited` | `bool` | b1 |  |
| last findings | `last_findings` | `ids` | b1 | finding ids |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

## Sources and evidence

### connectors

`:Connector`, node. Scope world. Restore group snapshot. Owner D5, D10, D13.

Keys: `connectors_id_unique` (id).

Indexes: `connectors_world_id_type_idx` (world_id, type).

S27: parser, locator format, source-type mapping and speaker rules are code, not rows.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| type | `type` | `enum:connector_type` | b1 |  |
| auth reference | `auth_reference` | `code` | b1 |  |
| scope | `scope` | `json_ids` | b1 | an allowlist compiled into the read (D14) |
| settings | `settings` | `json_ids` | b1 | calendar log mode, calendar id, from date, days logged |
| batch rule | `batch_rule` | `code` | b1 |  |
| staleness limit | `staleness_limit` | `int` | b1 | days; absent means always fresh |
| expected read interval | `expected_read_interval` | `int` | b1 | seconds (D7) |
| watermark | `watermark` | `locator` | b1 |  |
| last attempted read | `last_attempted_read` | `instant` | b1 |  |
| last successful read | `last_successful_read` | `instant` | b1 |  |
| last outcome with its reason as a code | `last_outcome` | `enum:connector_outcome` | b1 | line 2391: denied and failed are the two cases of could not read |
| items taken in over 7 days | `items_taken_in_over_7_days` | `int` | b1 |  |
| oldest unprocessed item | `oldest_unprocessed_item` | `instant` | b1 |  |
| counters (read, held nothing, extracted, denied, failed) | `read_count`, `held_nothing_count`, `extracted_count`, `denied_count`, `failed_count` | `int` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### capabilities

`:Capability`, node. Scope world. Restore group projection. Owner D5, D11.

Keys: `capabilities_id_unique` (id).

Indexes: `capabilities_connector_type_registry_id_idx` (connector_type, registry_id); `capabilities_connector_type_selector_idx` (connector_type, selector).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 | '' for defaults (S5) |
| connector type | `connector_type` | `enum:connector_type` | b1 |  |
| a registry id | `registry_id` | `id` | b1 | absent on a selector row |
| a declared-field selector | `selector` | `json_ids` | b1 | lifecycle class, statement class, temporal class; keys sorted, so equal selectors have equal text |
| capability | `capability` | `enum:capability` | b1 |  |
| staleness limit | `staleness_limit` | `int` | b1 | days, where it differs |
| origin | `origin` | `enum:capability_origin` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### sources

`:Source`, node. Scope world. Restore group snapshot. Owner D2.

Keys: `sources_id_unique` (id).

Indexes: `sources_world_id_connector_id_external_locator_idx` (world_id, connector_id, external_locator).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| connector id | `connector_id` | `id` | b1 |  |
| external locator | `external_locator` | `locator` | b1 |  |
| received at | `received_at` | `instant` | b1 |  |
| the screened content or a pointer to it | `content` | `text_home` | b1 |  |
| the screened content or a pointer to it | `content_pointer` | `locator` | b1 |  |
| parser version | `parser_version` | `code` | b1 |  |
| untrusted | `untrusted` | `bool` | b1 |  |
| forget state | `forget_state` | `enum:forget_state` | b1 | changed in place by a forget (line 2525) |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### episodes

`:Episode`, node. Scope world. Restore group snapshot. Owner D2, D10.

Keys: `episodes_id_unique` (id); `episodes_world_id_connector_id_first_message_key_unique` (world_id, connector_id, first_message_key).

Indexes: none.

A later cut of the same window keeps the id and extends the range (line 761), so S12 gives the row a version.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| connector id | `connector_id` | `id` | b1 |  |
| source ids | `source_ids` | `ids` | b1 |  |
| first and last message key | `first_message_key`, `last_message_key` | `code` | b1 |  |
| start and end time | `start_time`, `end_time` | `instant` | b1 |  |
| cut rule | `cut_rule` | `code` | b1 |  |
| read at | `read_at` | `instant` | b1 |  |
| run id | `run_id` | `id` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### evidence

`:Evidence`, node. Scope world. Restore group projection. Owner D2, D5, D9.

Keys: `evidence_id_unique` (id); `evidence_world_id_message_key_target_id_polarity_unique` (world_id, message_key, target_id, polarity).

Indexes: `evidence_world_id_target_type_target_id_polarity_active_idx` (world_id, target_type, target_id, polarity, active); `evidence_episode_id_idx` (episode_id); `evidence_source_id_locator_idx` (source_id, locator).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| episode id | `episode_id` | `id` | b1 |  |
| source id | `source_id` | `id` | b1 |  |
| locator | `locator` | `locator` | b1 |  |
| message key | `message_key` | `code` | b1 |  |
| source time | `source_time` | `instant` | b1 |  |
| (none) | `source_offset_minutes` | `int` | ruling | S3, line 914: the source's own UTC offset |
| (none) | `person_timezone` | `code` | ruling | S3, line 914: an IANA code |
| target type | `target_type` | `enum:evidence_target_type` | b1 |  |
| target id | `target_id` | `id` | b1 | a belief's id is its item's id (S13) |
| polarity | `polarity` | `enum:polarity` | b1 |  |
| statement class | `statement_class` | `enum:statement_class` | b1 |  |
| source type | `source_type` | `enum:source_type` | b1 | derived by the writer |
| derivation | `derivation` | `enum:derivation` | b1 |  |
| premises | `premises` | `ids` | b1 | inferred rows only |
| speaker kind | `speaker_kind` | `enum:speaker_kind` | b1 |  |
| speaker item | `speaker_item` | `id` | b1 |  |
| original author | `original_author` | `id` | b1 | an item id, for forwarded content |
| modality | `modality` | `enum:modality` | b1 |  |
| active | `active` | `bool` | b1 |  |
| recorded | `recorded` | `instant` | b1 |  |
| ended at | `ended_at` | `instant` | b1 |  |
| end reason | `end_reason` | `enum:evidence_end_reason` | b1 | S15 |
| request id | `request_id` | `id` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### holds

`:Hold`, node. Scope world. Restore group snapshot. Owner D10.

Keys: `holds_id_unique` (id).

Indexes: `holds_world_id_state_expires_at_idx` (world_id, state, expires_at).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| episode id | `episode_id` | `id` | b1 |  |
| locator | `locator` | `locator` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| proposed in run | `proposed_in_run` | `id` | b1 |  |
| created at | `created_at` | `instant` | b1 |  |
| expires at | `expires_at` | `instant` | b1 |  |
| state | `state` | `enum:hold_state` | b1 |  |
| promoted to | `promoted_to` | `ids` | b1 | line 2191, promoted_to_refs[]: the item, link and evidence rows it became |
| closed at | `closed_at` | `instant` | b1 |  |
| close reason | `close_reason` | `value_id` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### unresolved_mentions

`:UnresolvedMention`, node. Scope world. Restore group snapshot. Owner D10.

Keys: `unresolved_mentions_id_unique` (id).

Indexes: `unresolved_mentions_world_id_state_expires_at_idx` (world_id, state, expires_at).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| episode id | `episode_id` | `id` | b1 |  |
| locator | `locator` | `locator` | b1 |  |
| candidate item ids with scores | `candidate_item_ids` | `json_ids` | b1 | list of {item id, score} |
| context used | `context_used` | `json_ids` | b1 | the rule that decided, with the item ids it read |
| waiting links | `waiting_links` | `ids` | b1 | op ids |
| state | `state` | `enum:mention_state` | b1 |  |
| resolved to | `resolved_to` | `id` | b1 |  |
| question id | `question_id` | `id` | b1 |  |
| expires at | `expires_at` | `instant` | b1 |  |
| (none) | `expiry_reason` | `code` | ruling | S25, lines 2004, 2105 |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### screening_receipts

`:ScreeningReceipt`, node. Scope world. Restore group snapshot. Owner D14.

Keys: `screening_receipts_world_id_source_id_unique` (world_id, source_id).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| source id | `source_id` | `id` | b1 |  |
| world_id | `world_id` | `id` | b1 |  |
| counts per class | `counts_per_class` | `json_ids` | b1 | never values |
| screened at | `screened_at` | `instant` | b1 |  |
| screened where | `screened_where` | `enum:screened_where` | b1 | S27: one reachable value |

## History and requests

### change_entries

`:ChangeEntry`, node. Scope world. Restore group snapshot. Owner D3.

Keys: `change_entries_world_id_sequence_unique` (world_id, sequence).

Indexes: `change_entries_world_id_entity_type_entity_id_sequence_idx` (world_id, entity_type, entity_id, sequence); `change_entries_change_set_id_idx` (change_set_id); `change_entries_request_id_idx` (request_id); `change_entries_run_id_idx` (run_id).

Append-only: the writer adds rows and nothing updates or deletes them (line 838).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| sequence | `sequence` | `int` | b1 | per world, allocated on :World (S10) |
| world_id | `world_id` | `id` | b1 |  |
| entity type | `entity_type` | `enum:entity_type` | b1 |  |
| entity id | `entity_id` | `id` | b1 |  |
| field | `field` | `code` | b1 |  |
| old value id | `old_value_id` | `value_id` | b1 |  |
| new value id | `new_value_id` | `value_id` | b1 |  |
| recorded at | `recorded_at` | `instant` | b1 |  |
| world time | `world_time` | `instant` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| evidence id | `evidence_id` | `id` | b1 |  |
| actor | `actor` | `code` | b1 |  |
| request id | `request_id` | `id` | b1 |  |
| run id | `run_id` | `id` | b1 |  |
| commit id | `commit_id` | `id` | b1 | UUIDv7; one commit may hold several change sets (S10) |
| change set id | `change_set_id` | `id` | b1 | UUIDv7 (S10) |
| inverse of | `inverse_of` | `id` | b1 | the change set this entry undoes |
| history only | `history_only` | `bool` | b1 | a late update that did not move the head (D1) |

### history_values

`:HistoryValue`, node. Scope world. Restore group snapshot. Owner D3, D14.

Keys: `history_values_id_unique` (id).

Indexes: none.

Full text: `grove_value_index`.

A row that holds an old value of an item's content column also has the label :PastValue (S18). No text is copied, so a forget that replaces the value clears the index.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| value | `value` | `text_home` | b1 | the value or the forgotten marker |
| forgotten at | `forgotten_at` | `instant` | b1 |  |
| (none) | `item_id` | `id` | ruling | S18: only on a row that also has the label PastValue |

### requests

`:Request`, node. Scope world. Restore group snapshot. Owner D2.

Keys: `requests_world_id_id_unique` (world_id, id).

Indexes: `requests_world_id_status_idx` (world_id, status).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | the caller's idempotency key, unique per world (S9) |
| world_id | `world_id` | `id` | b1 |  |
| caller | `caller` | `code` | b1 |  |
| source id of the request's text | `source_id` | `id` | b1 |  |
| run id | `run_id` | `id` | b1 |  |
| status | `status` | `enum:request_status` | b1 |  |
| attempts | `attempts` | `int` | b1 |  |
| created at | `created_at` | `instant` | b1 |  |
| committed at | `committed_at` | `instant` | b1 |  |
| receipt | `receipt` | `json_ids` | b1 | ids and codes; a title is read fresh when shown |
| last error code | `last_error_code` | `code` | b1 |  |

### declines

`:Decline`, node. Scope world. Restore group snapshot. Owner D2.

Keys: `declines_world_id_request_id_op_id_unique` (world_id, request_id, op_id).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | ruling | S5: line 3081 wins over the table's own list |
| request id | `request_id` | `id` | b1 | a requests id (S9) |
| op id | `op_id` | `code` | b1 |  |
| rule | `rule` | `code` | b1 |  |
| near-duplicate candidate ids with scores | `nearduplicate_candidate_ids` | `json_ids` | b1 | list of {item id, score} |
| override used | `override_used` | `bool` | b1 |  |

### jobs

`:Job`, node. Scope world. Restore group snapshot. Owner A.1, D10, D15.

Keys: `jobs_id_unique` (id).

Indexes: `jobs_world_id_lane_state_priority_run_after_idx` (world_id, lane, state, priority, run_after).

There is no SKIP LOCKED. A filter in MATCH or WHERE reads with no lock, so two runners that filter on state = queued both claim one job. A claim must check the state after it holds the node's write lock (S1): first a write to another property of the job, then WITH, then the check, then the change; or the check inside a CASE on the right side of SET, with the caller comparing the returned claimed_by. The runner lane adds a two-transaction race test with its claim.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| lane | `lane` | `enum:job_lane` | b1 |  |
| agent or job type | `agent_or_job_type` | `code` | b1 |  |
| trigger | `trigger` | `code` | b1 |  |
| input references | `input_references` | `json_ids` | b1 |  |
| priority | `priority` | `int` | b1 |  |
| state | `state` | `enum:job_state` | b1 |  |
| attempts | `attempts` | `int` | b1 |  |
| claimed by | `claimed_by` | `code` | b1 |  |
| claimed at | `claimed_at` | `instant` | b1 |  |
| run after | `run_after` | `instant` | b1 |  |

### run_records

`:RunRecord`, node. Scope world. Restore group snapshot. Owner 3.5, D12, D15.

Keys: `run_records_id_unique` (id).

Indexes: `run_records_world_id_agent_started_idx` (world_id, agent, started).

Full text: `grove_value_index`.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| agent | `agent` | `code` | b1 |  |
| trigger | `trigger` | `code` | b1 |  |
| input ids | `input_ids` | `ids` | b1 |  |
| model | `model` | `code` | b1 |  |
| effort | `effort` | `enum:effort` | b1 |  |
| tokens in, cached and out | `tokens_in`, `tokens_cached`, `tokens_out` | `int` | b1 |  |
| proposed operations | `proposed_operations` | `json_home` | b1 | a JSON string (S16); kept 30 days, then ids and counts |
| receipts | `receipts` | `json_ids` | b1 |  |
| decline ids | `decline_ids` | `ids` | b1 | op ids |
| outcome | `outcome` | `code` | b1 |  |
| token and time caps | `token_cap`, `time_cap` | `int` | b1 |  |
| started | `started` | `instant` | b1 |  |
| finished | `finished` | `instant` | b1 |  |
| scorecard measures before and after | `scorecard_before`, `scorecard_after` | `json_ids` | b1 | consolidation runs only |

### model_calls

`:ModelCall`, node. Scope world. Restore group snapshot. Owner D15.

Keys: `model_calls_id_unique` (id).

Indexes: `model_calls_world_id_day_idx` (world_id, day).

Written only by the model-call layer (S20).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| run id | `run_id` | `id` | b1 |  |
| job | `job` | `code` | b1 |  |
| model | `model` | `code` | b1 |  |
| effort | `effort` | `enum:effort` | b1 |  |
| input, cached and output tokens | `input_tokens`, `cached_tokens`, `output_tokens` | `int` | b1 | reasoning included |
| cost from the price table | `cost` | `float` | b1 |  |
| receipt reference | `receipt_reference` | `id` | b1 |  |
| at | `at` | `instant` | b1 |  |
| day | `day` | `date` | ruling | S24: a DATE from the clock provider; B.1 names it only in the index |
| (none) | `audio_seconds` | `float` | ruling | S24, line 2684: voice calls |

### judgments

`:Judgment`, node. Scope world. Restore group snapshot. Owner D2, D4, D8, D9.

Keys: `judgments_id_unique` (id).

Indexes: `judgments_world_id_subject_role_idx` (world_id, subject, role); `judgments_model_call_id_idx` (model_call_id).

Written only by the model-call layer (S20).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| model call id | `model_call_id` | `id` | b1 |  |
| role | `role` | `enum:judgment_role` | b1 |  |
| subject | `subject` | `ref` | b1 | S32: the claim, belief, word or pair judged; a word with no row yet is `value:<id>` |
| the evidence row | `evidence_id` | `id` | b1 | where there is one |
| verdict | `verdict` | `enum:verdict` | b1 |  |
| stated confidence | `stated_confidence` | `float` | b1 |  |
| correlated | `correlated` | `bool` | b1 | true when the second verdict is the same model family |
| at | `at` | `instant` | b1 |  |

### budgets

`:Budget`, node. Scope world. Restore group snapshot. Owner D15.

Keys: `budgets_world_id_day_unique` (world_id, day).

Indexes: none.

Written by the runner (S20).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | b1 |  |
| day | `day` | `date` | b1 | S24: from the clock provider |
| ceiling | `ceiling` | `float` | b1 |  |
| spent | `spent` | `float` | b1 |  |
| state | `state` | `enum:budget_state` | b1 |  |

## Truth, consequence and belief

### verifications

`:Verification`, node. Scope world. Restore group snapshot. Owner D4, D11.

Keys: `verifications_id_unique` (id).

Indexes: `verifications_world_id_claim_id_at_idx` (world_id, claim_id, at); `verifications_run_id_idx` (run_id).

Columns are D4's (line 985); every check is one appended row.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| claim | `claim_id` | `id` | b1 | an item or link id; a belief's item id (S13) |
| run | `run_id` | `id` | b1 |  |
| at | `at` | `instant` | convention | the index (world_id, claim id, at) names it; neither column list does |
| per piece of evidence: evidence row id, fetched at, time spoken for, source type, judgment id, weight | `evidence_pieces` | `json_ids` | b1 | one JSON list of those six fields: a list property cannot hold null or a map |
| second judge's judgment ids | `second_judge_judgment_ids` | `ids` | b1 |  |
| their correlation | `correlated` | `bool` | b1 |  |
| agree | `agree` | `float` | b1 | computed by the writer |
| disagree | `disagree` | `float` | b1 | computed by the writer |
| ratio | `ratio` | `float` | b1 | computed by the writer |
| band | `band` | `code` | b1 | computed by the writer; line 957 gives conditions, no names |
| load-bearing | `loadbearing` | `bool` | b1 | as the writer found it |
| disposition | `disposition` | `enum:disposition` | b1 |  |
| override reason | `override_reason` | `value_id` | b1 |  |
| re-check by before and after | `recheck_by_before`, `recheck_by_after` | `instant` | b1 |  |
| reason | `reason` | `value_id` | b1 |  |
| request id | `request_id` | `id` | b1 |  |
| commit id | `commit_id` | `id` | b1 |  |

### horizon_stats

`:HorizonStat`, node. Scope world. Restore group snapshot. Owner D4.

Keys: `horizon_stats_world_id_meaning_or_property_name_unique` (world_id, meaning_or_property_name).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | b1 |  |
| meaning or property name | `meaning_or_property_name` | `id` | b1 |  |
| checks | `checks` | `int` | b1 |  |
| moves | `moves` | `int` | b1 |  |
| days at risk | `days_at_risk` | `float` | b1 |  |
| suggested horizon | `suggested_horizon` | `int` | b1 | days |
| updated at | `updated_at` | `instant` | b1 |  |

### impacts

`:Impact`, node. Scope world. Restore group snapshot. Owner D6.

Keys: `impacts_link_id_unique` (link_id); `impacts_world_id_trigger_change_set_affected_item_unique` (world_id, trigger_change_set, affected_item).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| link id | `link_id` | `id` | b1 | the mark's link id |
| world_id | `world_id` | `id` | b1 |  |
| trigger change set | `trigger_change_set` | `id` | b1 |  |
| rule | `rule` | `enum:cascade_rule` | b1 |  |
| depth | `depth` | `int` | b1 |  |
| path | `path` | `ids` | b1 | link ids |
| stakes | `stakes` | `enum:stakes` | b1 |  |
| trigger source type | `trigger_source_type` | `enum:source_type` | b1 |  |
| state | `state` | `enum:impact_state` | b1 |  |
| proposal id | `proposal_id` | `id` | b1 |  |
| explanation | `explanation` | `value_id` | b1 |  |
| explained by | `explained_by` | `code` | b1 |  |
| resolved by | `resolved_by` | `code` | b1 |  |
| resolved at | `resolved_at` | `instant` | b1 |  |
| resolution reason | `resolution_reason` | `value_id` | b1 |  |
| affected item | `affected_item` | `id` | ruling | S25: repeats the mark's from item; B.1 names it only in the unique key |
| (none) | `flag` | `enum:impact_flag` | ruling | S25, line 1250 |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### cascade_proposals

`:CascadeProposal`, node. Scope world. Restore group snapshot. Owner D6.

Keys: `cascade_proposals_id_unique` (id).

Indexes: `cascade_proposals_world_id_state_idx` (world_id, state).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| rule | `rule` | `enum:cascade_rule` | b1 |  |
| mark ids | `mark_ids` | `ids` | b1 | impacts link ids |
| target item | `target_item` | `id` | b1 |  |
| suggested operations | `suggested_operations` | `json_ids` | b1 | text values as value ids |
| stakes | `stakes` | `enum:stakes` | b1 |  |
| state | `state` | `enum:proposal_state` | b1 |  |
| decided by | `decided_by` | `code` | b1 |  |
| decision reason | `decision_reason` | `value_id` | b1 |  |
| resulting commit | `resulting_commit` | `id` | b1 |  |
| (none) | `proposal_type` | `enum:proposal_type` | ruling | S25, lines 1252, 1264, 1265 |
| (none) | `detail` | `json_ids` | ruling | S25: ids, codes and counts |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### beliefs

`:Belief`, node. Scope world. Restore group projection. Owner D9.

Keys: `beliefs_item_id_unique` (item_id).

Indexes: none.

The statement is the item's title and body. The clocks and the horizon are the item's.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| item id | `item_id` | `id` | b1 | a belief's id is its item's id (S13) |
| world_id | `world_id` | `id` | b1 |  |
| state | `state` | `enum:belief_state` | b1 |  |
| state since | `state_since` | `instant` | b1 |  |
| origin | `origin` | `enum:belief_origin` | b1 |  |
| supporting episodes | `supporting_episodes` | `ids` | b1 | distinct episode ids |
| contradicting episodes | `contradicting_episodes` | `ids` | b1 | distinct episode ids |
| last support at | `last_support_at` | `instant` | b1 | the episode's own time |
| last contradiction at | `last_contradiction_at` | `instant` | b1 | the episode's own time |
| externally testable | `externally_testable` | `bool` | b1 |  |
| re-test how | `retest_how` | `json_ids` | b1 |  |
| held by the person | `held_by_the_person` | `instant` | b1 | when the person last decided the state |
| successor | `successor` | `id` | b1 |  |
| retrieved | `retrieved` | `int` | b1 |  |
| last retrieved | `last_retrieved` | `instant` | b1 |  |
| cited | `cited` | `int` | b1 |  |
| (none) | `end_class` | `enum:belief_end_class` | ruling | S25, line 1752 |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### contradictions

`:Contradiction`, node. Scope world. Restore group snapshot. Owner D9.

Keys: `contradictions_id_unique` (id).

Indexes: `contradictions_world_id_status_detected_at_idx` (world_id, status, detected_at).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| target | `target` | `id` | b1 |  |
| standing side | `standing_target_id` | `id` | b1 |  |
| standing side | `standing_change_id` | `int` | b1 | a change entry sequence (S10) |
| fresh side | `fresh_evidence_id` | `id` | b1 | S14: absent when the judges disagree |
| (none) | `fresh_source_id` | `id` | ruling | S14: what was read when no evidence row exists |
| (none) | `fresh_locator` | `locator` | ruling | S14: what was read when no evidence row exists |
| detected by | `detected_by` | `code` | b1 |  |
| detected at | `detected_at` | `instant` | b1 |  |
| request id | `request_id` | `id` | b1 |  |
| status | `status` | `enum:contradiction_status` | b1 |  |
| resolution | `resolution` | `value_id` | b1 |  |
| resolved at | `resolved_at` | `instant` | b1 |  |
| resolved by | `resolved_by` | `code` | b1 |  |
| question id | `question_id` | `id` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### support_changed

`:SupportChanged`, node. Scope world. Restore group snapshot. Owner D9.

Keys: `support_changed_world_id_belief_id_unique` (world_id, belief_id).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | b1 |  |
| belief id | `belief_id` | `id` | b1 | the belief's item id (S13) |
| cause change id | `cause_change_id` | `int` | b1 | a change entry sequence (S10) |
| queued at | `queued_at` | `instant` | b1 |  |
| taken at | `taken_at` | `instant` | b1 |  |

### questions

`:Question`, node. Scope world. Restore group snapshot. Owner D11.

Keys: `questions_id_unique` (id).

Indexes: `questions_world_id_state_priority_idx` (world_id, state, priority).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| subject items | `subject_items` | `ids` | b1 |  |
| ask | `ask` | `value_id` | b1 |  |
| class | `class` | `enum:question_class` | b1 |  |
| legwork | `legwork_sources` | `ids` | b1 | the sources checked |
| legwork | `legwork_reason` | `value_id` | b1 | why none settles it |
| answer options | `answer_options` | `json_ids` | b1 | item ids, fixed codes or value ids |
| raised by | `raised_by` | `code` | b1 |  |
| raised at | `raised_at` | `instant` | b1 |  |
| priority | `priority` | `int` | b1 |  |
| state | `state` | `enum:question_state` | b1 |  |
| answer source id | `answer_source_id` | `id` | b1 | an answer is a source row, never text here |
| applied at | `applied_at` | `instant` | b1 |  |
| receipt | `receipt` | `json_ids` | b1 | ids and codes; a title is read fresh when shown |
| (none) | `unblocks` | `ids` | ruling | S25, line 3197 |
| (none) | `withdraw_reason` | `value_id` | ruling | S25, line 2161 |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### area_audits

`:AreaAudit`, node. Scope world. Restore group snapshot. Owner D11.

Keys: `area_audits_world_id_run_id_unique` (world_id, run_id).

Indexes: `area_audits_area_id_idx` (area_id).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | ruling | S5: line 3081 wins over the table's own list |
| run id | `run_id` | `id` | b1 |  |
| area id | `area_id` | `id` | b1 |  |
| started | `started` | `instant` | b1 |  |
| finished | `finished` | `instant` | b1 |  |
| tests run | `tests_run` | `enum:growth_test` | b1 | a list of codes |
| finding ids | `finding_ids` | `ids` | b1 |  |
| operation ids proposed | `operation_ids_proposed` | `ids` | b1 |  |
| receipts | `receipts` | `json_ids` | b1 |  |
| question ids | `question_ids` | `ids` | b1 |  |

### walk_rotation

`:WalkRotation`, node. Scope world. Restore group snapshot. Owner D12.

Keys: `walk_rotation_world_id_area_id_unique` (world_id, area_id).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | b1 |  |
| area id | `area_id` | `id` | b1 |  |
| last explored | `last_explored` | `instant` | b1 |  |
| last finding id | `last_finding_id` | `id` | b1 |  |

## Health and privacy

### checks

`:Check`, node. Scope world. Restore group snapshot. Owner D13.

Keys: `checks_id_unique` (id).

Indexes: none.

Columns are D13's (line 2424). A check's state and value measure one world (lines 2398, 3202), so each world has its own row (S35).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | D13 calls it check id; one per check per world |
| world_id | `world_id` | `id` | ruling | S35: the real world id; one row per check per world |
| the person's question | `question` | `config` | b1 | S29, repeated on each row |
| measure | `measure` | `config` | b1 | S29, repeated on each row |
| cadence | `cadence` | `config` | b1 | S29, repeated on each row |
| bands with the reason for each | `bands` | `config` | b1 | S29, repeated on each row |
| state | `state` | `enum:check_state` | b1 |  |
| state | `state_gap` | `float` | b1 | the gap of short of target |
| value | `value` | `float` | b1 |  |
| interval | `interval_low`, `interval_high` | `float` | b1 | 95% interval |
| measured at | `measured_at` | `instant` | b1 |  |
| state since | `state_since` | `instant` | b1 |  |

### audit_runs

`:AuditRun`, node. Scope world. Restore group snapshot. Owner D13.

Keys: `audit_runs_id_unique` (id).

Indexes: `audit_runs_check_id_started_idx` (check_id, started).

Columns are D13's (line 2425).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | D13 calls it run id |
| world_id | `world_id` | `id` | ruling | S5: line 3081 wins over the table's own list |
| check id | `check_id` | `id` | b1 |  |
| seed | `seed` | `int` | b1 |  |
| strata and counts | `strata_and_counts` | `json_ids` | b1 |  |
| controls planted | `controls_planted` | `int` | b1 | S29: a count; the controls stay with the harness |
| grader model and effort | `grader_model` | `code` | b1 |  |
| grader model and effort | `grader_effort` | `enum:effort` | b1 |  |
| second grader | `second_grader` | `code` | b1 |  |
| cost | `cost` | `float` | b1 |  |
| status | `status` | `enum:audit_run_status` | b1 |  |
| started | `started` | `instant` | b1 |  |
| finished | `finished` | `instant` | b1 |  |
| (none) | `invalid` | `bool` | ruling | S25, lines 2376, 2450 |

### audit_grades

`:AuditGrade`, node. Scope world. Restore group snapshot. Owner D13.

Keys: `audit_grades_world_id_run_id_subject_unique` (world_id, run_id, subject).

Indexes: none.

Columns are D13's (line 2426). Ids and locators only.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | ruling | S5: line 3081 wins over the table's own list |
| run id | `run_id` | `id` | b1 |  |
| subject | `subject` | `ref` | b1 | S32: an item, link or question reference, or `event:<connector id>:<message key>` |
| stratum | `stratum` | `code` | b1 |  |
| grade | `grade` | `enum:audit_grade` | b1 |  |
| evidence | `evidence_connector` | `id` | b1 | the connector |
| evidence | `evidence_locator` | `locator` | b1 | the locator |
| evidence | `evidence_fetched_at` | `instant` | b1 | fetched at |
| grader | `grader` | `code` | b1 |  |
| second grade | `second_grade` | `enum:audit_grade` | b1 |  |
| agreement | `agreement` | `bool` | b1 |  |
| (none) | `rationale` | `value_id` | ruling | S25, line 2527 |

### findings

`:Finding`, node. Scope world. Restore group snapshot. Owner D13.

Keys: `findings_id_unique` (id).

Indexes: `findings_world_id_status_due_idx` (world_id, status, due).

Columns are D13's (line 2427). Ids only, never the claim's text.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | D13 calls it finding id |
| world_id | `world_id` | `id` | ruling | S5: line 3081 wins over the table's own list |
| run id | `run_id` | `id` | b1 |  |
| subject | `subject` | `id` | b1 |  |
| defect class | `defect_class` | `code` | b1 |  |
| owner | `owner` | `code` | b1 |  |
| opened | `opened` | `instant` | b1 |  |
| due | `due` | `instant` | b1 |  |
| status | `status` | `enum:finding_status` | b1 |  |
| closing run id | `closing_run_id` | `id` | b1 |  |
| (none) | `declined_reason` | `value_id` | ruling | S25, line 2427 |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### forget_ledger

`:ForgetLedger`, node. Scope world. Restore group ledger. Owner D14.

Keys: `forget_ledger_id_unique` (id).

Indexes: `forget_ledger_world_id_idx` (world_id).

The copy kept outside the dump belongs to the forget lane (S23).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | UUIDv7 text from new_id() (S4) |
| world_id | `world_id` | `id` | b1 |  |
| target type | `target_type` | `enum:forget_target_type` | b1 |  |
| internal ids and locators | `internal_ids` | `ids` | b1 | never values or titles |
| internal ids and locators | `locators` | `locator` | b1 | never values or titles |
| requested by | `requested_by` | `code` | b1 |  |
| approved at | `approved_at` | `instant` | b1 |  |
| receipt | `receipt` | `json_ids` | b1 | counts per place, never the value |
| the date after which no backup holds the value | `backups_clear_after` | `date` | b1 |  |

### oauth_clients

`:OAuthClient`, node. Scope platform. Restore group snapshot. Owner A.3, D14.

Keys: `oauth_clients_client_id_unique` (client_id).

Indexes: none.

A B.1 table read through the platform query layer: a client exists before any world consents to it. It restores with the snapshot tables (S21).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| client id | `client_id` | `locator` | b1 | S32: a UUIDv7 from dynamic registration, or the URL of a Client ID Metadata Document (lines 3036, 3148) |
| world_id | `world_id` | `id` | b1 | absent until consent (S5) |
| (none) | `user_id` | `id` | ruling | S5: absent until consent |
| host | `host` | `config` | b1 | a host's registration metadata (line 2561) |
| registration type | `registration_type` | `enum:registration_type` | b1 |  |
| redirect URIs | `redirect_uris` | `config` | b1 | a host's registration metadata (line 2561) |
| scopes granted | `scopes_granted` | `code` | b1 | a list of scope codes |
| granted at | `granted_at` | `instant` | b1 |  |
| revoked at | `revoked_at` | `instant` | b1 |  |

## Platform rows

### worlds

`:World`, node. Scope platform. Restore group platform. Owner platform; signin.

Keys: `worlds_id_unique` (id).

Indexes: `worlds_owner_user_id_idx` (owner_user_id).

The signin lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention | the world_id of every row in the world |
| (none) | `owner_user_id` | `id` | convention |  |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `next_sequence` | `int` | convention | S10: starts at 0; advanced by one direct SET |
| (none) | `seed_version` | `code` | convention |  |

### users

`:User`, node. Scope platform. Restore group platform. Owner platform; signin.

Keys: `users_id_unique` (id); `users_email_hash_unique` (email_hash).

Indexes: none.

The signin lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `email_hash` | `code` | convention | secrets and contact details stay out of the row |
| (none) | `created_at` | `instant` | convention |  |

### sessions

`:Session`, node. Scope platform. Restore group platform. Owner platform; signin.

Keys: `sessions_id_unique` (id); `sessions_token_hash_unique` (token_hash).

Indexes: `sessions_user_id_idx` (user_id).

The signin lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `user_id` | `id` | convention |  |
| (none) | `token_hash` | `code` | convention |  |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `revoked_at` | `instant` | convention |  |

### oauth_tokens

`:OAuthToken`, node. Scope platform. Restore group platform. Owner platform; hosts-a.

Keys: `oauth_tokens_id_unique` (id); `oauth_tokens_token_hash_unique` (token_hash).

Indexes: `oauth_tokens_client_id_idx` (client_id); `oauth_tokens_user_id_idx` (user_id).

The hosts-a lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `client_id` | `locator` | convention | an oauth_clients client id (S32) |
| (none) | `user_id` | `id` | convention |  |
| (none) | `world_id` | `id` | convention | S5: tokens are bound to one world (line 3038) |
| (none) | `token_hash` | `code` | convention |  |
| (none) | `token_type` | `code` | convention | access or refresh |
| (none) | `scopes` | `code` | convention | a list of scope codes |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `revoked_at` | `instant` | convention |  |

### authorization_codes

`:AuthCode`, node. Scope platform. Restore group platform. Owner platform; hosts-a.

Keys: `authorization_codes_id_unique` (id); `authorization_codes_code_hash_unique` (code_hash).

Indexes: none.

The hosts-a lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `client_id` | `locator` | convention | an oauth_clients client id (S32) |
| (none) | `user_id` | `id` | convention |  |
| (none) | `world_id` | `id` | convention |  |
| (none) | `code_hash` | `code` | convention |  |
| (none) | `code_challenge` | `code` | convention | PKCE S256 |
| (none) | `redirect_uri` | `config` | convention |  |
| (none) | `scopes` | `code` | convention | a list of scope codes |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `used_at` | `instant` | convention |  |

### email_codes

`:EmailCode`, node. Scope platform. Restore group platform. Owner platform; signin.

Keys: `email_codes_id_unique` (id).

Indexes: `email_codes_email_hash_idx` (email_hash).

The signin lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `email_hash` | `code` | convention |  |
| (none) | `code_hash` | `code` | convention |  |
| (none) | `attempts` | `int` | convention |  |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `used_at` | `instant` | convention |  |

### upload_tokens

`:UploadToken`, node. Scope platform. Restore group platform. Owner platform; ingest-a.

Keys: `upload_tokens_id_unique` (id); `upload_tokens_token_hash_unique` (token_hash).

Indexes: none.

The ingest-a lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `user_id` | `id` | convention |  |
| (none) | `world_id` | `id` | convention | an upload token serves one world |
| (none) | `token_hash` | `code` | convention |  |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `revoked_at` | `instant` | convention |  |

### migrations

`:Migration`, node. Scope platform. Restore group platform. Owner platform; foundation.

Keys: `migrations_name_unique` (name).

Indexes: none.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `name` | `code` | convention | scope/file |
| (none) | `scope` | `code` | convention | the default scope is grove |
| (none) | `checksum` | `code` | convention |  |
| (none) | `applied_at` | `instant` | convention |  |

## Full-text indexes

| Name | Labels | Properties | Analyzer |
|---|---|---|---|
| `grove_items_lexical` | `Item` | `world_id`, `title`, `summary`, `body`, `property_values`, `search_terms` | `standard-folding` |
| `grove_aliases_lexical` | `Alias` | `world_id`, `phrase`, `normalised_phrase` | `standard-folding` |
| `grove_past_values` | `PastValue` | `world_id`, `value` | `standard-folding` |
| `grove_value_index` | `Item`, `Alias`, `HistoryValue`, `Kind`, `Meaning`, `PropertyName`, `RunRecord` | `world_id`, `title`, `summary`, `body`, `property_values`, `search_terms`, `phrase`, `normalised_phrase`, `value`, `name`, `proposed_operations` | `standard-folding` |

<!-- generated:end -->
