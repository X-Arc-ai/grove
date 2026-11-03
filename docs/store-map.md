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

**S20. Writes outside the agents** (spec lines 2501, 3120, 3121). `model_calls` and `judgments` are written only by the model-call layer, sessions and OAuth rows by sign-in, job claims by the runner, and budgets by the model-call layer (S39 replaced "budgets by the runner"). Each such write must be a function in `server/grove/writer/`, and only its owner calls it. `models`, `signin`, `hosts-a` and `runner` build them. The writer rule stands: only `server/grove/writer/` opens write transactions, and the writer lint enforces it.

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

**S34. No script folding** (spec line 1386). Line 1386 says aliases are normalised for case, accents and script. `normalise_phrase` folds case, accents, underscores, hyphens, punctuation and spacing, and drops invisible format characters (Unicode category Cf: a zero-width space or joiner, a soft hyphen, a byte order mark, a direction mark), so two phrases that read the same are one phrase. A text of only whitespace and format characters reads blank (`reads_blank`): the writer's blank checks ask it, so a title, a reason, a `distinct_from` reason or a required field of that form is refused like an empty one. It does not fold script: `Москва` and `Moskva` stay different phrases. Transliteration needs a language model or a table that this build does not have. `writer-a` owns the resolver and any change. The resolver refuses an empty phrase (S44).

**S35. Checks are per world** (spec lines 2398, 2424, 3081, 3202). Line 3081 puts `world_id` on every table, and a check's state and value measure one world. So there is one `checks` row per check per world, with the real `world_id`. The wording (the person's question, measure, cadence, bands) is developer configuration, repeated on each row. This replaces the store-wide reading of `checks` in S5 and S29.

**S36. The world id form** (spec lines 3081, 3087, 3092). A world id is a lowercase UUID (`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`), or the test form `t_<slice>_<that uuid>` with the slice `[a-z0-9-]+`. `check_world_id` refuses any other text, and the primitive sentinel `''` stays a separate path. The full-text analyzer splits on hyphens and folds case, and with this form the token sequence of one world id never occurs inside another's unless both share the UUID. So the world phrase of S17 is exact in practice, and one world's rows cannot use up another world's `LIMIT`. This replaces the earlier pattern `[A-Za-z0-9_-]{1,80}`, under which `t_x_<h>` was a token prefix of `t_x_<h>-000`.

**S37. Receipts and short ids** (spec lines 733, 737, 2529, and S4). A stored receipt (`Receipt.to_json()`, `requests.receipt`) holds ids and codes only: symbol, entity type, entity id, field, value id, end class and flags. `render_receipt` reads titles, kind names, meaning names and values fresh, so a forget that replaces a value also changes how an old receipt reads. The receipt is built after the commit from the change entries of the batch's change set, in a new read transaction. A short id is the last 8 hex digits of the id: the random part of a UUIDv7, because ids made in the same millisecond share their first digits. One batch is one commit and one change set. The writer's public API is `server/grove/writer/README.md`.

**S38. Locators** (spec lines 739, 3046 to 3059, and S27). The writer checks that a locator resolves inside its stored source with four schemes: `line:N` or `line:N-M` (1-based lines of the content), `char:A-B` (offsets in the content), `key:<token>` (a token of 4 or more characters that occurs in the content: a record id, an event id, a commit sha) and `whole` (the whole source). Each connector type allows a fixed set of schemes in code (`grove/writer/provenance.py`), and a source that holds only a pointer resolves only `whole`. `ingest-a` may narrow a type's set when it lands its parsers.

**S39. Metering and the budget** (spec lines 2659, 2694, 3120, 3122). The `models` lane writes `model_calls` and `budgets` together: `record_model_call` in `server/grove/writer/model_calls.py` creates the `:ModelCall` and adds its cost to the day's `:Budget` in one transaction, so `spent` is always the sum of the day's recorded costs. This replaces "budgets by the runner" in S20. The operator sets a ceiling with `set_budget_ceiling`; an absent `ceiling` means no ceiling. A budget row that does not exist yet takes the ceiling of `GROVE_DAILY_BUDGET_USD`. `state` turns `reached` in the transaction where `spent` reaches `ceiling`. At `reached`, the jobs that `server/grove/models/jobs.json` marks `defer` are refused with `budget_reached` before any request leaves, and the jobs marked `continue` (triage, extraction and transcription, run in code) still run, as D15 says (line 2659). Every gateway call stops at the ceiling, whatever job its token names: the build plan says an agent call stops there, and the plan outranks D15. A pass that waits leaves its requests queued for the reset. Calls that pass the gate at the same moment can end the day above the ceiling by the cost of the calls in flight. `model_calls` gains five properties: `usage_estimated`, `route` (`meta`, `modal`), `outcome` (`ok`, `invalid_output`, `rate_limited`, `provider_error`, `client_closed`), `reasoning_tokens` (the reasoning share of `output_tokens`, line 2694) and `latency_ms`. A failed call is recorded with its outcome, so the share of calls that wrote nothing counts every call. Its tokens are zero when the provider did no work (a refusal, a 429, a 503, a connection that never opened), and an estimate of the input when it may have run the call; `usage_estimated` marks an estimate. The price table is `server/grove/models/prices.json` (S24).

**S40. Sign-in's rows** (D8, and S20). `server/grove/writer/accounts.py` holds every write of sign-in (S20). `users.email_hash` is the sha256 hex of `grove-email-v1:` and the address, trimmed and in lower case, so a Google sign-in and an email code with one address reach one user; the address itself is in no row, log or error. A first sign-in makes a world whose id is a UUID version 5 of the user id, so two racers make one world; this is the one world id that is no UUIDv7. `sessions.method` says `google` or `email`. The own item is one `person` item, made once: one transaction takes the world's lock with a direct SET on the `:World` row, reads `worlds.self_item_id` again under that lock, runs `apply_ops`, and sets `worlds.self_item_id`, so the item and the column commit together or not at all. Its evidence cites a source that holds the pointer `signin:<method>` and no text, so there is nothing to screen and the locator is `whole`. The steps before that transaction may run twice, after a crash or in a race. They leave one connector and one episode, and at most a spare source row, which says nothing. `email_codes.code_hash` is the sha256 of the row id, the email hash and the code, joined by colons.

**S41. Requests** (spec lines 748, 750, 763, 2029, 3081, 3116). A request commits exactly once, in one batch, and `saved` moves in the same transaction as that commit. There are no stored batches and no list of batches on the row. A kill before the commit rolls everything back, so the next pass extracts again; a kill after the commit leaves `saved`, so no pass runs again. So "exactly one effect" comes from atomicity, not from a model that re-reads what it did. "Applies only the remainder" (line 750) is all or nothing at this grain, and `committed_for_request` answers what a request committed. A pass with more facts for the same words mints another request id (`request_id=None`, or `<key>:2`). A backfill re-ground takes a new id for each try (`<K>:g<N>`), and the unique key on evidence still holds layer 1. A request id is `[A-Za-z0-9_.:-]{1,128}` at every entry point, because it lands on change entries and a receipt prints part of it, so `hosts-b` passes a hash of the host's idempotency key, prefixed with the OAuth client id.

The row adds these columns to B.1: `version` and `touched_at` (S12); `commit_id` and `change_set_id` (the one commit); `fingerprint` and `declined_fingerprint` (sha256 of the batch); `repairs`; `retry_at`, `lease_until` and `claimed_by`; `content_key` (what a second enqueue compares); `verbatim`, `host_message_id`, `said_at` and `about` (B.3's remember fields, because the pass that needs them runs later in another process). The constants live in `server/grove/writer/requests.py`, because Appendix C has no row for them: `MAX_ATTEMPTS` 5, `BACKOFF_SECONDS` (30, 60, 120, 240), indexed by the attempts and capped at the last, and `LEASE_SECONDS` 300. `failed` with a `retry_at` waits for a retry and reads "saving". `failed` with none is surfaced. "Saved" needs the status and a receipt in hand. The receipt attaches right after the commit, because it is a fresh read, so a saved row with no receipt is legal after a crash, and `request_state` rebuilds it from the change set with no write.

Only a row `enqueue_request` wrote has a `source_id`, and only such a row can be claimed. One predicate says what is due: `queued` with `retry_at` absent or past, `failed` with `retry_at` past, `applying` with `lease_until` absent or past. A replay runs the rules on the row twice: with no lock before the world lock, so replays of a saved request never queue behind it, and under the lock, because the first read can be stale. Saved with the same fingerprint writes nothing and reads the receipt. Saved with another fingerprint, or a finish that has none, is refused with rule `request_key_reused`. Declined with the same fingerprint writes nothing and raises the decline again. Declined with a new fingerprint is the one repair attempt (line 748) while `repairs` is 0, and is refused with rule `repair_spent` after that. A batch's transaction takes `WorldTx.lock_world()` first, a direct `SET` on the `:World` row, because the near-duplicate check, the restatement read and the slot read take no lock of their own. The statement sets and removes a marker (`SET w.lock_marker = true REMOVE w.lock_marker`) and must read no column: `SET w.next_sequence = w.next_sequence` read the counter before it waited for the lock and wrote that stale value back, so a batch that waited took a sequence in use (probed: two batches both took 51). Every batch took this lock at its first change entry already, so the added wait is small, and deadlocks among batches go away. The item and link locks stay as belts.

**S42. Declines** (spec lines 748, 764, 774, 2529). A `:Decline` adds `code` and `recorded_at` to B.1. A row holds ids and codes only: a refusal's message and details can repeat a caller's phrase, and a forget cannot reach a copy here, so neither is stored. `op_id` is `batch` for a fault of the whole batch, and an op id outside `[A-Za-z0-9_.:$-]{1,80}` is stored as `opaque:<first 16 of its sha256>`. `requests.receipt` of a declined request holds the stored decline: state, batch size, request id, and refusals of `{op_id, op, code, rule}`, with `op` only if it names an operation and `unknown` if not. `nearduplicate_candidate_ids` is JSON of `{item_id, score}`, so a replayed decline hands a repair its data. The log is one transaction under the request row's lock, after the rollback. A stale version alone is the exception: an enqueued row goes back to `queued` with a `retry_at`, and any other row keeps its status while the `:Decline` rows are written, because the next pass re-reads and proposes a new batch (line 2029). A log that fails never masks the decline. A near-duplicate that passes on a named `distinct_from` writes a row in the batch's own transaction with `override_used` true. A refusal that never reached the request is not logged: a caller with no write scope, a caller that is no queue worker, `request_key_reused`, `repair_spent`, `lease_lost`, and `not_found` from an enqueue or from `apply_request`.

**S43. What is current** (spec lines 766, 826, 832, 920, 1075, 1187, 2758). D3's `current()` is `server/grove/store/current.py`, the one liveness function that every read and every guard asks (line 2758). The writer's guard `apply._ended` calls its `has_ended`, and `TERMINAL_ROLES` (done, cancelled, ended) is defined there only. `standing(row, *, at, lifecycle_class, planned_origin, scheduled, successors)` is pure, and its rules run in this order. (1) `ended`: an end class of any kind, a terminal status role, or a stop clock. `spent` ends a record on the record axis only, so an as-of read on that axis decides which row `standing` gets. (2) `overtaken`: an active link of the primitive meaning "corrected by" or "superseded by", read by id. (3) The scheduled class decides by its evidence, and no date moves it (lines 1075, 1187). The class is a bounded period, or a record with a planned origin (an active supporting row of source type planned, which stays when a report arrives) or with a property whose `scheduled_role` is `start`. Confirmed and not inferred goes on to rule 4, and a bounded period or a record with a scheduled start also needs a known start for that: a confirmed booking does not prove the trip (line 1078), and only the presence of the start role is read, never its date. A record's claim never changes, so such a record stays `planned` for good: only a new record with a known start reads current. A planned-origin record with no scheduled start reads current on any confirmed row. Reported done and not inferred reads `reported`. Anything else reads `planned`. (4) `not_started`: a known start wholly after `at`. (5) `current`: outside the scheduled class an unknown start counts as started. A plan is work with a state, so it reads current once made (line 920). The successors and the scheduled values (`{role: value}`) ride on every state, and the state never reads a value.

`link_standing(row, *, at)` reads a link: `ended` when its active key is gone or it has an end class, unless its stop clock lies wholly after `at` (a slot link ended at a future start stood until then), which never holds for `refuted`, because a refuted link was never true; then `not_started` for a future start; else `current`. `standings(source, ids)` and `current_items(source, ids)` read head rows at the world's `clock.now`, from a `WorldQuery` or a `WorldTx`, and in a `WorldTx` they see that transaction's own writes. `distinct_episodes(source, target_ids)` in `server/grove/store/evidence_counts.py` is one count of distinct episode ids among the active supporting rows of all the targets together (line 766): the bar for a word counts across its links, and a sum per target would count one episode once per target.

**S44. The resolver** (spec lines 1386, 1587 to 1596, 1649, 2010, 2012, 3280). `server/grove/writer/resolver.py` resolves a kind, a meaning or a property name in D8's order: an exact id or name, then the normalised name, then an alias. A name wins over an alias. Only when nothing matched does a leading "is" or "are" fall away, and the same steps run again, so "is related to" reaches "related to" with no mint and no model. Present tense only: "was owned by" stays unmatched, because a link's clocks say when. A reversed alias sets `reversed`, and the writer swaps the ends. An alias counts only when its own row and its target are both usable (not retired, not split). A property name resolves per kind: only the aliases of this kind's property names count (line 1649). A phrase with two usable targets, by alias or by normalised name, is `ambiguous` and is never picked (line 1386). The strict wrappers on `Registry` refuse it with `unknown_kind`, `unknown_meaning`, or `invalid_arguments` for a property, all with rule `ambiguous_phrase` and the candidates `{id, name, reversed}` in the details. A phrase that normalises to nothing is refused with `invalid_arguments`, rule `empty_phrase`, which closes the open point of S34. Two spellings of one property in one operation (a name and its alias, or two case forms) with different values are refused with `invalid_arguments`, rule `property_named_twice`, on a create and on an update, because the last key would win and the other value would be lost.

`resolve_link_phrase` gives a resolved phrase its meaning and keeps no phrase. An unmapped or ambiguous phrase rides on the one usable generic meaning (`generic` true, no consequences) with its phrase kept and its candidates returned, never on a meaning that carries consequences (line 1596). With no such row, or with two, it returns none, and the mint path is `ontology`'s. `lexical_candidates` (layer 3 of D10's ladder, line 2010) returns every item of the kind at trigram similarity `CANDIDATE_THRESHOLD` 0.3 or more (Appendix C) over the title and the item aliases, best first, with scores rounded to 3 places. An ended item comes back flagged by `has_ended` and is never dropped, because a late fact needs its ended item. With `exclude_distinct_from`, that item and its `:DistinctPair` partners are left out (line 2012). The writer's near-duplicate gate keeps its rule: items with no end class, at `DEFAULT_THRESHOLD` 0.8, one starting value until `ingest-b` calibrates it. Both score with one helper, `title_scores`, so one pair never gets two scores. Appendix C's resolution at 0.8 also asks for a shared neighbour, which the gate does not read yet.

**S45. Consent is a grant per host and world** (spec lines 3036, 3038, 3148, and S5, S20, S32). A person's consent to a host is one `:OAuthGrant` row (`oauth_grants`) per (client, world), because a Client ID Metadata Document is one URL for every person who uses that host, so one client row cannot hold one person's consent. B.1's `world_id`, `scopes granted`, `granted at` and `revoked at` of `oauth_clients` stay absent on the client row, and so does `user_id`: the grant holds them. This replaces the part of S5 that puts `world_id` on `:OAuthClient`. A new consent from the same world finds the grant, sets its scopes and time, and clears `revoked_at`. Tokens, codes and client secrets are stored as sha256 hex hashes only (`token_hash`, `code_hash`, `client_secret_hash`). A token carries the `world_id` and `grant_id` of its grant, and `family_id`, the id of the code that began its chain. A code is used once, and a second use revokes every token of its family and sets the code's `reused_at`. The issue of the first pair locks that code before the grant and refuses a code with `reused_at`, so a second use that comes between the first use and its issue still leaves no live token. A refresh rotates: the old token gets `used_at` and `revoked_at`, and the new pair keeps the family and the grant; a second use of a rotated refresh token revokes the whole family. A revoke of the grant revokes every token of the grant in the same transaction. Each of these checks runs after a direct SET on the row it reads (the first form under "What A.5 asks, what Neo4j gives"), so two redeems of one code give one winner. A family revoke, at a reused code, a reused refresh token or `POST /revoke` of a refresh token, locks the family's grant row first and reads the family in a statement of its own; `issue_tokens` and `rotate_refresh` hold that lock when they add a pair, so a pair that a rotation adds at the same time is revoked too. A document client's row is written only at an Approve, after the csrf check and just before its grant; a fetch of a URL with no row writes nothing, so a consent page, a Deny and a refused decision leave no row, and a fetch that finds a row refreshes it. A client that no grant names is deleted by a later write of a client (a registration, that Approve, or the refresh of a row), at most 100 per write for the two types together: a dynamic client once it is 7 days old, and a `metadata_document` client once its `fetched_at` is older than `METADATA_DOCUMENT_STALE_LIMIT` (24 hours), when its copy can no longer serve without a new fetch. A world holds at most `MAX_GRANTS_PER_WORLD` (100) grant rows, counted with revoked ones, because a grant keeps its client row. An Approve reads that count before any write and refuses a client that has no grant in the world when the count is at the cap. It is a soft cap: approvals at one time can pass it by a few. `server/grove/writer/oauth.py` is the only writer of these rows (S20). Migration `0010_oauth.cypher` names this ruling S41, its number before the `writer-a2` lane took S41 to S44; an applied migration is never edited.

**S46. Upload connectors** (spec lines 2001, 2505, 3046 to 3059, 3101, 3103). `ingest-a1` builds them (`docs/ingest.md`). A connector row is one per world, type and provider: `connectors.provider` (a ruling column) holds the upload format (`claude_code`, `codex`, `chatgpt`, `claude_ai`), and `ensure_connector` makes it under the world lock. A message key is `<prefix><32 hex>`, one word, a sha256 fingerprint of the source's own ids for the message (A.4 names them per connector), and a chat's `external_locator` is the same form over the chat's id. So a key or a locator holds no name or term, and screening leaves both as they are: a rename form or a denylist term matches whole words only, so none can match the prefix, and `register` refuses a prefix whose keys screening would change. The pipeline refuses a chat whose key changed (fail closed). One upload's reads have a budget (`docs/ingest.md`, limits), and the api takes one upload at a time. One chat's new messages (the delta: keys not stored for its locator) commit as one source, its receipt and its episodes in one transaction, under the connector row's lock, which re-reads the stored keys. A delta that screening drops whole stores its empty source only for a chat with no source yet, so a re-import is idempotent. A stored message row from a connector adds `speaker_kind` and `modality`, closed codes the screener passes through. The watermark is `at:<ISO time>` of the newest message read; an episode with no `run_id` is unprocessed.

## Open points for later lanes

| Point | Spec lines | Owner lane |
|---|---|---|
| How an item's area membership is stored. | 1368, 2145, 3095 | `cleanup`, `hosts-b` |
| A value the person names inside source text has no index path. | 2518, 2525, 2560 | `forget` |
| Which bound orders `latest`, and where "unknown" sorts. Both bounds have an index. | 1390, 1487 | `hosts-b` |
| The names of the verification bands. `verifications.band` is a code until then. | 957 | `truth-clock` |
| The stakes value of an R2 mark, and a mark's clocks. | 1247, 1254, 1270 | `walk` |
| Where a search term's type and language live. `items.search_terms` holds the text of each term. | 2076, 2098 | `ingest-b` |
| The deferral count: stored, or derived from `verifications`. | 2159, 2174 | `cleanup` |
| The grade rows of controls, and findings raised by agent runs: their run id and their text. | 2198, 2376, 2427 | `evals`, `cleanup` |
| The newest-id part of the fingerprint for a table with no id, and whether a rebuild keeps `touched_at`. | 812, 922 | `writer-b` |
| Rows outside the S12 list that change after they are written carry no version: `support_changed.taken_at`, `history_values.forgotten_at`, `budgets.spent`, `horizon_stats`, `walk_rotation`, `checks`, and the finished times of `audit_runs` and `area_audits`. Each owner adds the pair when it writes the row. `requests` has it (S41); `jobs` and `run_records` have it (`runner`), and `ontology_change_log` (`ontology`, O10). | 3081 | `beliefs` (`support_changed`), `forget` (`history_values`), `models` (`budgets`), `truth-clock` (`horizon_stats`), `walk` (`walk_rotation`), `evals` (`checks`, `audit_runs`), `cleanup` (`area_audits`) |
| The 5 second wait of remember, built from `request_state`. A hash of the host's idempotency key as the request id, prefixed with the OAuth client id (S41). `to_api()` has no receipt text, so a host renders through `render_receipt`. A surfaced failure answers remember with the typed error `unavailable`. `verbatim` requires `host_message_id`, and the schema of remember checks that: the queue only stores the two. | 754, 3195, 3294 | `hosts-b` |
| The value index must gain the status words, `items.status` and `kinds.status_vocabulary` (S31). Until then, a forget of a status word renames the word in the kind. | 2560, 2561 | `forget` |
| `why` of a contradiction from `grove.cleanup.v1` (the `grove.extract.v1` form) has no value-id column in `contradictions`. | 2087, 2199, 3133 | `beliefs` |
| Migrations run from the checkout's `server/` folder, never from the installed package: the wheel does not hold them. Closed by `deploy-a`: the API image holds `server/` as in a checkout with an editable install, and a deploy runs the migrations from it (`docs/deploy.md`). | none; a build fact | `deploy-a` |
| `GROVE_ENV=production` must be set on the box, or the guard that refuses a test clock does not run. Closed by `deploy-a`: `deploy/compose.yml` sets it for `api` and `runner`, and a test checks it. | 354, 991 | `deploy-a` |
| D3's restore has not run on this store: rebuild the projections from history, replay the forget ledger, compare D3's fingerprint. The daily dump and its load into a scratch store run on the box since `deploy-a`, checked by the dump fingerprint (`docs/deploy.md`). | 806 to 808, 3065 | `writer-b` |
| The runner still reaches the host's sshd and nginx through the gateway of its internal network. Its bridge now has the fixed name `grove-runner`; the host firewall rule on that interface goes with the egress allowlist. | none; a build fact | `deploy-b`, `models` |
| The secret files of `docs/deploy.md` are on the box, except `modal.env`, which is still owed. Closed for Google's client file by `signin`: the API reads it at `GROVE_GOOGLE_OAUTH_FILE` at first use, and no file or a bad file sends the person to the card with the Google error (`docs/signin.md`). | none; a build fact | `models` |
| `/mcp` and the metadata in `server/grove/api/wellknown.py` are placeholders. uvicorn runs without `--proxy-headers`, so a redirect it makes names `http`. Closed by `hosts-a`: the OAuth authorization server (`server/grove/api/oauth.py`), the full metadata of RFC 8414 and RFC 9728 in `server/grove/api/wellknown.py`, and the MCP transport at `/mcp` (`server/grove/api/mcp.py`) replaced them, on the rows of S45. Every absolute URL they give comes from `GROVE_PUBLIC_BASE_URL`, never from the request. The redirect fact is closed too: `create_app` sets `redirect_slashes=False`, so the app makes no redirect from a path. `/mcp/` has the handler of `/mcp`, and every other trailing slash is the typed 404. | none; a build fact | `hosts-a` |
| A batch at `/mcp` has no limit on its members inside the 1 MiB cap on the body. | none; a build fact | `hosts-b` |
| A batch at `/mcp` loses the results of its earlier members when a tool raises an error other than `ApiError`: the whole POST is a 500. | none; a build fact | `hosts-b` |
| Two tools with one name fail at request time, on every POST to `/mcp`, and not when the app starts. | none; a build fact | `hosts-b` |
| `ToolContext` gives a handler the raw driver. `hosts-b` must scope every read to `ToolContext.world_id` and prove it for the real tools. | none; a build fact | `hosts-b` |
| The consent page says "until you disconnect it in Grove". That needs the Connections view on `/v1/views/connections`. | none; a build fact | `views-b` |
| The verdicts of a judge call: `grove.writer.judgments.record_judgments` is the writer (S20, O12), and the word judges call it. The verify judge of D4 must call it too, with the S32 subject of the claim. | 996, 3121 | `truth-clock` |
| The job table has no row for load-bearing verification: `verification` waits at the ceiling, and D15 defers only the low-stakes claims. | 2659 | `truth-clock` |
| A call's receipt: `ModelLayer.attach_receipt` takes a `model_call_id`. A gateway row has no id for its caller, so it joins its receipts through its run id and the run's record, whose `receipts` lists the requests the run saved (`runner`). | 2659, 3120 | `ingest-b` |
| The backfill's calls use the jobs `backfill_triage` and `backfill_extraction`, which stop at the ceiling. `extraction` continues past it. A background poll treats a 429 as a passing failure, so a long stated reset can end and cancel a run: the backfill must set its parallelism under Meta's limits. | 2688 | `ingest-b`, `film-backfill` |
| Voice Transcribe takes WAV only here, with no contact names and no language bias: the forms probed for those two fields were refused. | 2673 | `ingest-a2` |
| `brain fetch` must send through `AllowlistTransport`, which checks each redirect hop, and not call `EgressPolicy.check` and then use another client. | 2559 | `cleanup` |
| Glimmer's idle time and cold starts bill on Modal and not in `model_calls`, and the workspace has no spend limit. The server stops after one idle minute, but one call after a quiet time costs about 0.26 USD of GPU at the H100 price: a cold start of about three minutes and the idle minute. So a call every four minutes bills a GPU all day, about 95 USD, and triage continues at the ceiling. A later lane that sends Glimmer a steady stream must batch it. `modal billing report` shows the true spend. | 2659 | `ingest-b`, `deploy-b` |
| `docs/data-flow.md` had no rows for the mail provider, Google sign-in, Calendar and backups. Closed by `signin`: the rows are there, and `web/build.py` generates the table of the privacy page from them. `forget` still replaces the page with a statement from the deployed configuration. | 2540 to 2552 | `forget` |
| The person's own item. The views read an optional `World.self_item_id` and mark that item as the person's own (`self_item_id` in the session and graph answers). Closed by `signin` (S40): the `worlds` row has the column, and the first sign-in writes it in the transaction that makes the item. | none; a build fact | `signin` |
| `known_as_of` and `as_of` on the view endpoints filter the current head of each row, not its history: a row changed after the given time shows its newest values. A read of the history must replace that filter. | none; a build fact | `writer-b`, `views-c` |
| The privacy page (`web/privacy.html`, section "How to forget") says the forget control is not built yet and tells a person to write to `contact@x-arc.ai` to have a world deleted, and that a dump made before the deletion stays until its 30 days end. When the control ships, `forget` must rewrite that section to say how to use it, change "Last updated" (a constant in that file), and generate `docs/data-flow.md` from the deployed configuration, with each row's Runs or Planned as it stands then, including the Resend terms that the page says are not named yet. | none; a build fact | `forget` |
| `docs/data-flow.md` marks a path Runs or Planned, and `web/build.py` builds the privacy page's table from it. A lane whose path starts to run with a person's data changes its row to Runs in the same PR: past AI conversation uploads (Runs since `ingest-a1`), WhatsApp, voice notes, notes, files and images (`ingest-a2`), the calendar (`ingest-a3`), model calls (`ingest-b`), evidence packets (`hosts-b`). | none; a build fact | `ingest-a2`, `ingest-a3`, `ingest-b`, `hosts-b` |
| The privacy page has no statement on Google's API Services User Data Policy or its Limited Use requirements. The calendar consent asks a sensitive scope, so the lane of the calendar consent adds it before that consent goes to Google's review. | none; a build fact | `ingest-a3` |
| The session contract the views read: the cookie `grove_session`; `:Session.token_hash` is the lowercase sha256 hex of the cookie value; a session is valid while `revoked_at` is absent and `expires_at` is later than the clock's now; `:Session.user_id` leads to the oldest `:World` with that `owner_user_id`. `docs/views.md` states it in full. Closed by `signin`: `server/grove/writer/accounts.py` and `server/grove/api/auth.py` write these rows and this cookie, and `docs/signin.md` states them. | none; a build fact | `signin` |
| A `map_positions` row keeps `item_id` and its coordinates after its item is forgotten. The forget of an item must remove its map position too. | none; a build fact | `forget` |
| The views' account, sources, questions and workers still use the simulation's local browser state. A sign-in that brings another account resets that state in the tab that signs in (`signin`). A second tab that signs in as another person brings no 401, so the first tab clears nothing. | none; a build fact | `views-b` |
| The API serves the built page at `/` only when `GROVE_WEB_DIST` names `web/dist`. Closed by `deploy-a`: the API image runs `web/build.py` and sets that variable, and the deploy smoke asks for the page. | none; a build fact | `deploy-a` |
| `change_status` and `end_item` do not trigger the walk yet, and `reopen_item` does not yet send an R1 ending back as a proposal. | 3160 to 3162 | `walk` |
| The writer takes the source type of a row from the connector and the statement class only. These wait for code: an automated sender confirming its own action, a decision's named speaker, a forwarded row's original author, and confirming evidence for the start of a period that took place. Today the writer accepts a bounded period's became-true time with no confirming row in the batch, from a calendar entry or a chat line too, and line 1177's contract test has no test yet. `current()` reads such a period `planned` until a confirmation (S43), so the reads stay right. Line 1080's table of what each source can prove is not code either: a booking page can confirm a booking and not the trip, but `current()` cannot tell a booking from a receipt, so an event from a calendar entry and a booking page, with no scheduled property, reads current. | 1074, 1080, 1112, 1177 | `truth-clock` |
| A late terminal status that goes to history only stores its status entry but no `stopped_being_true` entry, because nothing clears a stop entry when a later live status moves the head. An as-of read at that time shows the status with no end time, and `change_status` refuses an explicit end time on this path (rule `stopped_on_history_only`). | 3160 | `writer-b` |
| A judgment marked correlated counts toward the two judges of a contradiction: D9 and B.2 ask only for two models. | 3165 | `beliefs` |
| No starter property name has a `scheduled_role`, so `standings` reads no scheduled time yet. The data path for "planned for 12 July" is open: a scheduled property on the claim, or the plan's date read through its link. `standings` reads only the properties of the item's own kind (S43). | 832, 920, 1078 | `truth-clock` |
| Calibrate the near-duplicate level (`NEAR_DUPLICATE_THRESHOLD` per kind is empty, so `DEFAULT_THRESHOLD` 0.8 holds) and `CANDIDATE_THRESHOLD` (0.3) per kind on the backfill's labelled pairs, successive revisions among them. Add the ladder's other joins: layer 2 (aliases, handles and contact identifiers), the shared neighbours that Appendix C's resolution at 0.8 asks for, and overlapping time. Add the registry enumeration for the extraction prompt's cached prefix (D8 step 3). `lexical_candidates` is layer 3 over titles and item aliases only (S44). | 1591, 2009, 2010, 3280 | `ingest-b` |
| The as-of reads on both axes call `standing` with rows from `item_as_of`, and `link_standing` with a link's as-of row. `spent` ends a record on the record axis only, so the record-axis as-of decides which row `standing` gets and how a spent record reads. `standings` reads head rows at `clock.now` only (S43). An as-of caller must also pass the evidence fields (`derived_source_type`, `derivation`) and `planned_origin` as they stood then: a row from `item_as_of` holds neither today, so `standing` reads a confirmed period from it as `planned`. | 814, 826, 832 | `writer-b` |
| Invisible or blank characters outside Unicode category Cf (U+3164, U+2800, control and private-use characters) stay in the normal form, because dropping them needs a whitespace exception and a list. So a title with one inside each word scores low at the gate, and a title of only such a character is saved at its first create (a repeat is refused). A title of only U+FE0F or U+034F (category Mn, invisible) normalises to nothing and is saved on a repeat too. | 2010, 3280 | `ingest-b` |
| A duplicate key of one spelling in a request's JSON never reaches the writer's `property_named_twice`: a parser keeps the last key. The layer that parses a request refuses it. | 3159 | `hosts-b` |
| A "corrected by" or "superseded by" link with a future became-true time overtakes its item at once: `standings` reads only that the link is active. The composite operations decide whether such a link can exist. | 832 | `writer-b` |
| The caller of `plan_words` and `fit_links`: a held property name's value must leave the item operation (the writer refuses an undeclared property), and an item of a record kind that carries a property whose name settled as a state (`WordPlan.words`) is a claim about now and needs a re-check by; `fit_links` fits links only. | 1585, 1597 | `ingest-b` |
| A reworded title filed under another kind passes the gate, because the gate and the band read one kind only. D10 says "a compatible kind", and no code defines which kinds are compatible. A rewording below 0.8 is shown only by `lexical_candidates`, which has no production caller yet. | 2010, 2774 | `ingest-b` |
| A title with an empty normal form (`...`) scores 0.0, so the near-duplicate gate never weighs it. | 2010 | `ingest-b` |
| The writer lint has no allowance yet for the bookkeeping tables of the model-call layer and the job runner, and must name them. | 772 | `models`, `runner` |
| `record_connector`, `record_source` and `record_episode` take no `Caller`, so the read-only refusal does not reach them. A layer that exposes them to a token checks its scope first. | 772 | `hosts-b` |
| The neo4j driver logs the parameters of every query at DEBUG, and so the screened content of each source write. Production must keep the `neo4j` logger above DEBUG, because logs hold ids, counts and operation names only. | 2534 | `deploy-b` |
| The update agent owns these coverage fields of a connector row: `held_nothing_count`, `extracted_count` and `items_taken_in_over_7_days`; it sets an episode's `run_id` when a pass takes it and moves `oldest_unprocessed_item` to the oldest episode still without one (S46, `docs/ingest.md`). | 2001, 3101 | `ingest-b` |
| An upload is parsed, screened and stored inside its request, which nginx holds for 600 s. An export too large for that needs a job of its own, with its state readable by the views. | 2041 | `ingest-b`, `views-b` |
| A message that screening drops is counted again in a later read's receipt when that read stores new messages of the same chat, because the delta is the keys not stored. | 2505 | `forget` |

A point that a later lane resolves leaves the table above and is listed here with its ruling.

| Point | Spec lines | Resolved by |
|---|---|---|
| A connector must give each chat its own `external_locator`, because two chats can otherwise store one locator, and opaque message keys, because a denylisted term in a key is stored and not counted. | 2505 | `ingest-a1`: `chat_locator` and `message_key` give opaque one-word fingerprints (S46); tests screen 3000 digit-heavy keys, and keys under a rename map and a denylist that hold each key prefix, unchanged. |
| Each service that screens needs `GROVE_RENAME_MAP` and `GROVE_DENYLIST` in its environment and the two files mounted with mode 600. With `GROVE_ENV=production` an unset variable is a refusal (SC3). | 2505 | `ingest-a1`: the api service has both variables and a read-only mount of `/etc/grove/screening`; `apply.sh` refuses a file open to group or other and hands each file to uid 10001 at 400. Absent files make uploads answer 503 and store nothing (`docs/deploy.md`). The runner screens nothing. |
| An upload route adds its own nginx location, with its body limit and `proxy_request_buffering off`. Every other path takes at most 10 MB (`deploy/nginx/grove-https.conf.template`). | none; a build fact | `ingest-a1`: `/v1/sources/upload` has its location at 256m, the API default of `GROVE_UPLOAD_MAX_BYTES`. |
| The value lists of `property_names.value_type` and `meanings.direction`. | 1583, 3241 | `ontology`, O13 |
| `ontology_change_log.undone_by` changes after the row is written and carried no version. | 3081 | `ontology`, O10 |
| Line 1689, second half: a property whose value names an existing item is refused in favour of a link, and a retired item alias no longer counts at the gate or in the band. | 1689, 2010 | `ontology`, O14 |
| The mint path after `resolve_link_phrase` finds no word, the valve of three new words a batch, and a world with no usable generic meaning or with two. | 1593, 1596 | `ontology`, O1, O6, O12 |
| The runner's driver for the due list, a credential per world for each pass, the lease of a killed pass and a worker name per pass: the API holds the driver and answers `/v1/internal/runner/*` under the runner token; each pass gets a pass token of one world; the worker name is the run id; the lease is the time cap plus the margin, and `end_pass` calls `fail_request(retryable=True)` (`docs/runner.md`). | 750, 2029, 2659 | `runner` |
| `runner.env` holds the runner token, written by `deploy/runner-secrets.sh`; the loop runs in place of the placeholder, and a stop ends its passes as `stopped`, with their requests back in the queue (`docs/runner.md`, `docs/deploy.md`). | 750, 2029, 2659 | `runner` |
| The API mints each pass's gateway run token with its `token_cap` and holds `GROVE_GATEWAY_SECRET` (`/etc/grove/gateway.env`, shared with the gateway). A pass that meets the ceiling ends `budget` and hands its requests back with a retry (`docs/runner.md`, open point there). | 750, 2029, 2659 | `runner` |
| The gateway is the compose service `gateway` on the store, runner and egress networks, at `http://gateway:18300/v1` for the runner (`docs/deploy.md`). | 750, 2029, 2659 | `runner` |

## Added by later lanes

These are not in this slice. Each owner lane adds its rows here and in the catalogue.

| Rows | Owner lane |
|---|---|
| Map positions per item: the table `map_positions` (`:MapPosition`), under "Tables of later lanes" below. `server/grove/writer/positions.py` is its only writer, and `docs/views.md` has the endpoint that fills it. | `views-a` |
| A person's consent to a host, per host and world: the table `oauth_grants` (`:OAuthGrant`), under "Tables of later lanes" below (S45). `server/grove/writer/oauth.py` is its only writer. | `hosts-a` |
| My Grove guidance | `views-b` |
| Pending exports | `views-b` |
| Eval scripts and their results | `evals` |

## Screening (lane `screening`)

D14's screening runs in code on the server, before the `sources` table. No step calls a model, and nothing in it logs. The rulings SC1 to SC20 are this lane's. S numbers stay with the other lanes.

| Name | Module | What it does |
|---|---|---|
| `Screener(config)` | `grove.screening` | `screen_source(locator, messages)` screens one source. `screen_text(text, *, sender=None, key="text")` screens one message with no locator, such as a remember request or a note. Both return a `ScreenedSource`. |
| `ScreeningConfig` | `grove.screening` | The rename map, the denylist, the person's own identifiers and the opt-in classes. `ScreeningConfig.from_environment(own_identifiers=..., sensitive_classes=...)` loads the two files. |
| `Message`, `ScreenedMessage` | `grove.screening` | One message as the connector read it (`key`, `sender`, `text`, `at`), and the same fields after screening. |
| `ScreenedSource` | `grove.screening` | `locator`, `messages` (the kept ones, in input order), `counts`, `dropped_whole`, and `content()`: the JSON array that `sources.content` stores. Only the screener makes one. |
| `GROVE_RENAME_MAP`, `GROVE_DENYLIST` | `grove.config` | The paths of `rename.json` and `denylist.txt`. Unset means empty outside production and a refusal with `GROVE_ENV=production` (SC3). The files are never in the repo. |
| `python -m grove.screening counts [--rename PATH] [--denylist PATH]` | the CLI | Prints only `{"rename_forms": n, "labels": n, "denylist_terms": n}`. A file that cannot be loaded exits 2. It writes nothing. |
| `write_screened_source(tx, screened, *, source_id, connector_id, parser_version)` | `grove.writer.sources` | Writes one `:Source` and its `:ScreeningReceipt` in the caller's transaction. |
| `add_screened_source(writer, screened, *, connector_id, parser_version, source_id=None)` | `grove.writer.sources` | The same in a transaction of its own. With no `source_id` it mints one. It returns the source id, the counts and the number of kept messages, never text. |
| `build_prompt(name, blocks, *, root=None)` | `grove.prompts` | A `Prompt`: the data rules and the named instruction file as the instruction part, the data blocks as the data part. `messages()` gives the system and the user message. |
| `render_blocks(blocks)` | `grove.prompts` | The data part alone, so the `brain` CLI labels what it returns to an agent the same way. |
| `DataBlock(kind, speaker, source_type, ref, text)` | `grove.prompts` | One labelled block. `kind` is `source`, `item`, `belief`, `question` or `receipt`; `speaker` is a `speaker_kind` code; `source_type` is a `source_type` code or none; `ref` is an id or none. Only `text` is free. |

The class codes are the enum `screening_class`. A receipt counts each class that occurred.

| Code | What screening does | Stored in its place |
|---|---|---|
| `credential` | Masks an API key, a token, a password or a private key. | `[credential]` |
| `payment_card` | Masks a card number that passes the Luhn check. | `[card]` |
| `one_time_code` | Drops the message on a strong signal. Masks the number of a weak candidate, and the message stays (SC6). | nothing, or `[code]` |
| `reset_link` | Drops the message on a strong signal. Masks the URL of a weak candidate, and the message stays (SC8). | nothing, or `[link]` |
| `contact_email` | Masks a third party's email address. | `[email N]` |
| `contact_phone` | Masks a third party's phone number. | `[phone N]` |
| `contact_handle` | Masks a third party's handle. | `[handle N]` |
| `pay_amount` | Masks a pay amount, only when the person chose "never store my pay". | `[amount]` |
| `denylisted` | Drops a message whose sender or text holds a listed term. A term in the locator keeps the whole source out. | nothing; `[kept out]` in a locator |
| `renamed` | Replaces a name form of the rename map with its label. | the label |

N is a number per distinct value within one source, so a reader can tell two people apart. The order per message is fixed: 1 the denylist, 2 one-time codes and reset links, 3 the rename map, 4 credentials, 5 cards, 6 contact identifiers, 7 pay amounts. The locator goes through steps 1, 3, 4, 5 and 6.

**SC1. A request and its source commit together** (spec lines 750, 2462, 2505). Screening is code only on the server and never waits on a model, so it finishes before the writer opens a transaction. The caller mints the source id with `new_id(world_id)`, screens, then commits the source row, its receipt and the request row in one write transaction: `enqueue_request` or `write_request_row` takes `write_source`, which calls `write_screened_source` in that transaction for a new row only (S41, `tests/writer/test_request_source.py`). A request never names a source that is not there, and the raw text is never stored first. This is why `write_screened_source` takes the caller's transaction.

**SC2. Screening settings stand apart** (spec line 2505). `Settings` is unchanged. `ScreeningSettings` and `load_screening_settings()` in `grove.config` hold the two paths, so a bad screening path never stops `load_settings()` for the store. An error names the variable or the CLI flag and a line or an entry position. It never names the path, because a home folder path can hold a name, and never the content. No OS, JSON or Unicode error travels with it as its context.

**SC3. The files fail closed** (spec line 2505). With `GROVE_ENV=production`, the value `deploy/compose.yml` sets on the box, an unset `GROVE_RENAME_MAP` or `GROVE_DENYLIST` is a `ConfigError`, because it would screen real text with an empty list and no error. Outside production an unset variable stays empty. Each of these is also a `ConfigError`: a variable that is set but blank; a path whose `~user` is unknown; a file that is missing, unreadable, not a regular file (a directory or a FIFO, which opens without blocking and is then refused) or open to group or other (`mode & 0o077`); bytes that are not UTF-8; JSON that is not one object of strings, or is nested too deep; a form, label or term with no letter or digit. A UTF-8 byte order mark is dropped first. A denylist line ends at `\n`, `\r\n` or a bare `\r`, and line numbers count those ends. Forms that differ only in case, spacing or width are one form when they share a label, and an error that names their positions when they do not.

**SC4. The comparison copies** (spec line 2505). Names match on a copy after NFKC and case folding. Detectors read an NFKC copy that keeps case, because key formats are case-sensitive. Both copies drop format characters (zero-width space, soft hyphen, joiners, byte order mark), so an invisible character cannot split a name or a card number. Both also write a curly apostrophe (U+2018, U+2019, U+02BC) as `'` and a hyphen look-alike (U+2010, U+2011, U+2012) as `-`, because NFKC leaves them alone and a phone types them by default. A symbol that NFKC spells in letters, such as the trade mark sign, gets a space before it in the copy, so it does not glue to the word before it. Out of reach: a zero-width space between two words of a name joins them in the copy, because the tested case of a zero-width space inside one word needs the opposite fold. Spans map back to the original offsets, and the stored text keeps its original form outside the replaced spans. Whitespace inside a form or a term matches any run of whitespace. A run of more than 32 marks on one character is cut into clusters of that length, because NFKC sorts the marks of a cluster in quadratic time and no writing holds that many. SC19 adds a read view to each copy.

**SC5. Cards** (spec line 2510). A candidate has 13 to 19 digits, and it must also have a card shape: one unbroken run, groups of four with a last group of one to four digits, or the 4-6-5 and 4-6-4 forms. Without the shape, an order number such as `112-3456789-1234567` would pass Luhn about one time in ten. A trailing group after a card, such as a security code, does not hide the card. Groups split by a space or a dash may mix. A dot, a tab, two spaces or ` - ` between groups also counts, when one separator holds throughout, which goes beyond the design's "single spaces or dashes" because a person writes cards that way. An unbroken run must start with 2 to 6, the first digit of a card network, so an epoch time or a snowflake id that passes Luhn by chance stays. A long decimal stays too. Out of reach: a card glued to a word, such as `card4111...`, because a word character before a digit run is how ids and hashes look.

**SC6. One-time codes** (spec line 2511). A whole-message drop loses memory, so it is for strong signals only. A strong signal is the spec's action: the message is dropped and counted. A weak candidate is masked and the message stays: the number becomes `[code]`, counted under `one_time_code`. So "the port in the code is 8000." keeps its words, "the code is 482910." becomes "the code is [code].", and no value is stored either way (spec line 2552). Only the text of a message is masked this way. A sender, a key and a locator go through steps 3 to 6.

Strong: a phrase of the design's list (verification, security, confirmation, access, login and sign-in code, one-time code, password or pin, OTP, 2FA or MFA code, passcode, "your code is", "enter the code"), "to verify your" and "do not share this code", their French forms ("code de vérification", "code de connexion") and the Arabic word for code, with a number within about 40 characters on either side. A code right after one of these, which count only so because they are common dev words: "your ... code" and "votre ... code" with up to three words between, "auth code", "authentication code", "launch code", "account code", "secure code", "guard code", 2FA, MFA, TOTP, "use code", "enter code", "use this code" and their French forms, and a label on its own line such as "Kestrel code:" with digits. A code right after a service phrase (below). "482910 is your Instagram code" and "482910 est votre code Instagram", also with login or sign-in as the last word. A service as the sender: an address, one word (`Security`) or a service name, with a number in the text. A code right after a phrase may follow `:` with no space, and may hold letters when it holds a digit (`K7X9QZ`, `ABC-123`).

Soft signals: how long it lasts ("expires in 5 minutes", "valid for 10 min"); "do not share", "do not give" or "do not forward" near a number; a verb, a code and a sign-in purpose in one sentence, in either order ("Use N to log in", "To sign in, enter N", "Enter N on the login page"); a sign-in label ("Login: N", "Verification: N", "Sign in with N", "Your login token is N", "N - Kestrel login"). One soft signal is strong for any code except a number of 4 or 5 digits, a lowercase word with a digit, or a ticket key, because those can be a port or a name, and then it is weak. Two soft signals in one message are strong. So "Use 5521 to log in to Fable. It expires in 5 minutes." drops, and "use 8080 to log in locally" becomes "use [code] to log in locally".

Weak: the bare word "code", which the design says is not enough for a drop: "code is N", "the code was N", "the code they sent me is N", "Code: N", "Code N" and "N is the code", also the French "est" and the Darija "howa". The number must end a sentence, so "the code is 1200 lines", "the code is 1200, the tests are 800" and "(the code is 1200)" stay. With words between, only a number that is not a year counts. A service phrase (registration, activation, recovery, pairing, SMS, or a service name, followed by "code") with a number further off than right after it, because dev chat says "the WhatsApp code" about the connector. Capitals right after a strong phrase (`QWERTY`, `ABC-DEF`), because a service sends them and code cannot tell them from a word. A sha shape, seven or more hex characters with a letter, right after a phrase or before "is your code". "#4821 is your code". A sender with a role word and other words ("Karim Accounts", "Kestrel Security"), because that can be a person, with a number in the text.

No candidate: a number that names something else ("PR 1234", "#4821" near a phrase, "line 1420", "port 8080", "room 1204", "invoice 48210"), also before "is your code" and from a service sender; a ticket key such as `LUX-1234`; a number with a unit or a noun on its own line ("1500 ms", "1200 lines", "8400 messages"), so "Your OTP: 482910\nTime left: 5 minutes" still drops; "Use 8080 as the port"; a year near a phrase, unless it is given as the code ("code: 2026"); a word with a number (`python3`); a name right after a phrase (`sha256`, `HS256`, `x509`).

Out of reach. A mask on dev text costs one span and stays where a guard would pass a real code: "use 3000 to log in to the local server", "the code was 1200", "Code: 1045", "the launch code freeze is at 1800", "use neo4j to verify", and a `git log` line right after a subject that ends in "OTP" or "login code", which keeps its words and loses its sha. A lowercase code with no digit ("Your verification code is qwerty") stays, because code cannot tell it from "your code is broken". A number with no phrase, verb, label or service sender stays, because nothing marks it as a code.

**SC7. Credentials by entropy** (spec line 2509). A pure hex token (a git sha, a hash) and a UUID, also one after a prefix such as `t_screening_`, need a context word right before them, with at most "is" or "was" between, because dev text puts shas next to "key", "api" and "token" all the time. Other random-looking tokens keep the same-line rule, and `authorization` is a context word too. A token with `/` must mix lower case, upper case and digits, and at least one of its parts must not read as words, a number, a capitalised word or a tag of five characters or fewer, so a path is not a key (`web/src/components/v2/TokenPanel`, `lanes/screening/DESIGN`). The context words have an open left edge, because a name glues them (`openaiKey`, `authtoken`, `AccountKey`), and `monkey` beside a random token is the cost. A count of tokens is no context word (`input_tokens`, `maxTokens`, `1200 tokens`), and `api` with a `/` on either side names a route. The id of a model API (`msg_`, `req_`, `toolu_`, `srvtoolu_`, `call_`, `resp_`, `session_`, `chatcmpl-`) takes the rule of a hex token: it stays unless a context word stands right before it. This goes past the design's same-line rule in one form: a token alone on its line, in a fence or not, is read with the nearest line above that holds text, because a person writes "here is the api key" and puts the key below it. A hex token or an id there needs the context word at the end of that line. A token right after `=` is read, so `signing_key=<token>` is masked whatever the name. A token made of words, numbers and short tags joined by `-`, `_`, `.` or camelCase is no key: a model id (`claude-opus-4-1-20250805`), a branch, a slug, a class name. A random key of 24 characters reads as words fewer than 2 times in 1000, and only this fallback and the link check read that test. Out of reach: "the idempotency key is <uuid>" is masked, because the words are the ones a real key would have. Base64 with a `/` can read as a path by chance: about 3 in 1000 such tokens of 20 characters, 2 in 10000 of 28, and none in 20000 from 40 up. It stays. A hex token under the entropy limit stays too, also right after a context word ("the token is 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"), because the limit is what keeps a fixture such as `a1a1a1...` out.

**SC8. Reset and magic links** (spec line 2511). The grades of SC6 hold: a strong signal drops the message, and a weak candidate is masked and the message stays: the URL becomes `[link]`, counted under `reset_link`.

A link word matches only as a whole component of the path, the query or the fragment, after camelCase is split: `resetPassword` matches, `verifier` and `authors` do not. The list holds the design's words, `forgot`, `recover`, `activate` and their forms, the glued forms (`verifyemail`, `emailverify`, `setpassword`, `pwreset`, `resetpw`, `magiclogin`, `loginlink`, `authenticate`, `loginwithemail`) and the French `réinitialiser`, `connexion` and `mot-de-passe`. A piece ends at `/ ? & = # ; * | \ , $`, and a URL ends at `|`, because Slack writes `<url|label>`. Only this check also reads a link with no scheme (`app.example.com/m/x`, which a phone shows as a link), unless its last host label is a file suffix.

Strong: a link word and, after it, a token: 16 or more URL-safe characters with a letter and a digit, which are no file name (`session_tokens_v1.py`) and no slug of words (`invite-link-expires-in-24h`). A phrase that settles it ("reset your password", "password reset link", "sign in with this link", "forgot your password", "link to reset", "activate your account", "réinitialisez votre mot de passe") with any URL in the message. A phrase that dev chat also says about a feature ("magic link", "login link", "verify your email", "link to log in", "one-time link", "lien magique", "lien de connexion") with a URL right after it; only `:`, `is`, `here`, `below` or `follows` may stand between. A URL that the parser refuses.

Weak: a link word and a long value that a real token can be but a page can be too: letters only or digits only (a random token of 16 characters holds no digit about 1 time in 17), a value before the word, or a long id after a slug of three or more words. A link word in the host and a long value in the path or the query (`login.example.com/<value>`). A link word and a `code`, `otp` or `pin` value in the query (`/verify?code=482910`). A phrase of the second list with a URL further off, so a long tool output loses its URL and keeps its text. A phrase in common words ("sign in to Kestrel", "confirm your email", "verify your account", "set a new password", "link expires in 15 minutes") with a URL right after it, because a tracking link has no link word of its own and only the sentence can tell.

No candidate: a page that a rule of `_PAGE_RULES` clears. The code hosts' repository pages (`blob`, `tree`, `commit`, `compare`, `releases` and the like on GitHub, GitLab and Bitbucket), a raw GitHub link with a full sha, Linear's issue pages, a Notion page (a slug and a 32-character hex id), a Google document (`/document/d/`, `/spreadsheets/d/`, `/presentation/d/`) and a Figma file (`/file/`, `/design/`). No phrase counts these pages. Each rule holds a path, so a reset page on the same host still counts. A slug of three or more words that ends in an id of fewer than 16 characters, as a blog post has (`how-to-reset-a-neo4j-password-3f2a9c1e7b4d`). An OAuth `client_id`, which is public. A bare "reset link" is no phrase, because dev chat says it next to any URL.

Out of reach. A mask on dev text costs one span and stays where a guard would pass a real link: `https://docs.example/login/passwordstrengthcheck`, `https://app.example/login/1730000000000000`, `https://www.example.com/d/<id>/Login-flow`, `auth.example.com/<hex id>`. A host rule stays narrow, because trust in a whole host would keep `https://medium.com/p/reset/<token>`. A local route such as `/v1/verify/<uuid>` drops, because it has the shape of a real verify link. A percent-encoded value after a link word (`/login?redirect=%2Fdashboard%2Fprojects`) drops, for the same reason. A short tracking link with no sentence right before it stays, because nothing marks it.

**SC9. Contact identifiers** (spec line 2513). Handles: a handle may hold hyphens, like a GitHub login, and no length limit cuts it, which goes beyond the design's form because a tail would otherwise be stored. `@name(` is a Python decorator anywhere, because a mention takes no call. An `@name` first on its line is one when it ends the line (a trailing `#` comment is allowed) and the lines below reach a `def`, `async def` or `class` through other decorators; a chat line can hold only a handle. First on its line allows a diff marker and a line number from a tool's output. Common decorators (`property`, `classmethod`, `dataclass`, `retry`, `field_validator`, `login_required`, `unit_of_work`, `pytest`, `pytest.*`, `functools.*`, `app.*`, `mcp.*`, `modal.*` and the like) are code wherever a text names them. A short list of words that never name a person is exempt (`everyone`, `here`, `channel`, CSS, TypeScript and JSDoc at-words such as `media`, `layer`, `font-face`, `ts-ignore`, `param`, `returns`, and the npm tags `latest`, `next`, `canary`, `beta`). A name of these shapes is no handle: a number of up to six digits (`@20`, `@10:30`), a time (`@3pm`, `@15h`, `@14h30`), a version of two or three short groups (`@1.2.3`), a scale (`@2x`), a file mention (`@detectors.py`, `@package.json`) and an agent (`@agent-code-reviewer`). A number of seven digits or more stays a mention, because it can be a phone (`@447700900123`). `.md`, `.js` and `.sh` also end a person's handle (`@dr.jane.md`, `@karim.js`), so these three need a stem in capitals (`@CLAUDE.md`). Out of reach: `@notes.md`, `@index.js`, `` `@mydecorator` `` and a grep line `x.py:12:@mydecorator` are masked, because `` ping `@orla` `` and `notes.txt:12:@orla` read the same way and the mask costs one span. A person's handle with a code suffix (`@tamsin.py`) and a mention with a call form (`@orla(hr)`) stay. A profile link to Telegram (also `/s/`), Instagram, X, Twitter, Facebook, `fb.com`, `fb.me`, `m.me`, Threads, TikTok, Snapchat or LinkedIn (`/in/`) holds a handle, and its name part is masked. `profile.php?id=` loses its id and keeps its path. The subdomains stay narrow per host: `www.`, `m.` and `mobile.`, a country code on LinkedIn (`ma.linkedin.com`), and `web.` and a locale (`fr-fr.`) on Facebook, because any subdomain would read `docs.x.com/overview` as a profile. A GitHub link stays, because its first path part is mostly an organisation and dev text holds repository links all the time. A service label (`IG`, `insta`, `instagram`, `telegram`, `tg`, `skype`, `snap`, `snapchat`, `discord`), then `:` and one handle-shaped word, holds a handle, and so do `skype:name` and `tg://resolve?domain=name`, which goes beyond the design's form. A word of a short prose list (`Telegram: the bot is ready`) and a number of up to six digits (`Telegram: 12 messages`) stay. Out of reach: another word after a label is masked (`Telegram: connected`, `telegram: TelegramConfig`), because code cannot tell it from a handle. Emails: a `-`, `+`, `%` or `.` before an address is no part of it, and a `-` after it does not save it, so `-orla@x.example` becomes `-[email 1]` and `orla@x.example-thanks` becomes `[email 1]-thanks`. Two forms keep that `-` as part of a host, and the address stays: a host alias with a path (`git@github.com-work:org/repo.git`) and a last label that only a host has (`prod`, `local`, `internal`, `lan`, `corp`, `intra`, `stage`, `staging`), as in `deploy@web.prod-01`. Out of reach: `deploy@web.example.com-01` becomes `[email 1]-01`. A link writes `@` as `%40`, so `?e=orla%40x.example` becomes `?e=[email 1]`, and it is one value with its plain form; double encoding (`%2540`) stays. An address that names no person stays and is not counted: `noreply@anthropic.com`, `noreply@github.com`, `git@` on `github.com`, `gitlab.com` and `bitbucket.org`, and any address at `example.com`, `example.org` and `example.net`. `<digits>+login@users.noreply.github.com` stays masked, because it holds a login, and `.example` stays masked, because tests plant a person's address there. an address whose last label is an image suffix (`icon@2x.png`) is not an email, a file suffix is trimmed from an address in a file name, and URL userinfo and an SSH remote are not emails, while `mailto:`, a label (`Email:x@y`) and a vCard field are. A remote is the user `git`, or a colon and a path that holds `/` and starts with no port (`deploy@box:/var/www`). Any other word after the colon does not save the address, so `orla@x.example:8080` becomes `[email 1]:8080`. A word there with a digit or a symbol is the password of the account: `orla@x.example:Tr0ub4dor3` becomes `[email 1]:[credential]`. With no scheme, `admin:hunter2@db.example.com:5432/app` is userinfo when the secret holds a digit or a symbol and the user is no label such as `Email` or `to`: the secret is masked and the host stays. Out of reach: a password of plain letters after an address (`orla@x.example:sunshine`), because a label reads the same way (`orla@x.example:the invoice is ready`); a password with a `/` after an address, which reads as a remote, so the address and the password both stay. `_` around an address is chat markup (SC19). Phones: a label or words that give someone's number settle that bare digits are a phone: a phone word with an optional number word and `:` or `=` (`Phone number: 4155550132`, `Tel.: ...`, `Phone no.: ...`), `Tel` with a space alone, and an owner word before a number or phone word (`my number is ...`, `my phone's ...`, `my number: ...`, `mon numéro est ...`, `call me at ...`). An unlabelled bare run, `Number: ...` and `order number ...` stay by contract. The limit on the other side is a count after a phone label (`mobile: 1234567 users`), which loses its number. A run of more than 15 digits is cut into numbers, and a list of decimals is not a phone. A number right after `word:` counts when it has a phone's shape (`tel:`, `Tel:`, a vCard `TEL` line). A `+` form counts after any mark, because a `+` is no version or decimal. A number that starts with `0`, `+` or `(` also counts after a letter and `-`, `.` or `,` (`Tel.06 12 34 56 78`, `Orla,0612345678`) and right after a phone label (`Tel0612345678`). With a digit before the mark (`v1.0612345678`, `1.0612345678`) or after `word/` (`runs/0123456789`) it stays. `@` with no word before it does not stop a phone, because WhatsApp writes a mention of an unsaved contact as `@+212 612 345 678`; `foo@1.2.3` stays. Right after a phone, `/`, `,` or `-` starts the next number (`0612345678/0698765432`). Out of reach: `release-0612345678`, `INV-0012345678` and `report-0123456789.pdf` are masked, because a name glues a number the same way and the mask costs one span. A link writes `+` as `%2B`, as WhatsApp's own redirect does, so `to=%2B212612345678` becomes `to=%2B[phone 1]`, one value with its plain form. Two forms go beyond the design's rule on the leading `+` or `0`: a place (`wa.me/<digits>`, `phone=` on any host, a vCard `waid=`, `tel:`) settles that the digits are a phone, and the US form NNN-NNN-NNNN with one separator throughout or NNN NNN-NNNN, with or without a leading `1`, is common enough to need it. A bare `4155550132` stays. A phone may be written in any script of digits. Not phones: `(` with no `)` (`(148273645 bytes)`), three or more single-digit groups (a list of ids), three or more leading zeros (a padded counter), and four dotted groups of up to three digits (an address). The person's own identifiers sort into emails (an `@` after the first character), handles (a leading `@`, or a bare word) and phones (six or more digits). Phones compare as digits with a leading `00` dropped, so `+44 ...` and `0044 ...` are one number.

**SC10. Pay amounts** (spec line 2512). A figure may carry decimals (`25,000.00`, `25 000,00`). The spec says to drop the amount from every field, and the class is opt-in, so the mask is the safe side. Code reads three kinds of sum and four grades of word.

A marked amount names its money: a symbol or an ISO code in any case (`25000 mad`, `US$4,500`) or a word after the number (euros, dollars, dirhams, pounds, dh, bucks, grand, and the Arabic word for dirham). A suffixed amount has a `k`, `K`, `m`, `M` or `bn` suffix with no currency; `5m` can be minutes. A bare figure has three or more digits and no marker. A percentage, a path segment, a `#` reference and a part of a `PO-2026-00417` number are not figures.

| Grade | Words | What counts |
|---|---|---|
| Strong | salary, salaire, wage, income, stipend, retainer, compensation, total comp, take home, remuneration, earn, gagne, paie, paye, payé, `TC`, `OTE`, `TJM` (these three in capitals only), my pay, the pay, the Arabic word for salary | A marked or suffixed amount within eight words. A bare figure up to four words after the word. |
| Period | per month, per mo, per hour, per day, per year, per annum, par mois, par an, every month, a month, an hour, a year, monthly, hourly, yearly, annual, `/month`, `/mois` and the like | A marked or suffixed amount within eight words. A bare figure right before the phrase. |
| Weak | pay, paid, paying, bonus, raise, rate, make | A marked or suffixed amount within eight words. A bare figure up to four words after the word, when no noun follows the figure. `rate limit`, `make sure` and `pay attention` are not pay words. |
| Near | base, package, net, get, offer, offered, comp, touche, prime, augmentation, proposé | Up to four words after the word only: a marked amount; a suffixed amount or a figure of four digits or more, when no noun follows it. So `base 256`, `net 443` and `the package has 300k downloads` stay. |

A noun is any word after the sum that is not a period phrase or a short word such as this, last, net, gross, and, for or in. A range leaves no end behind: across `-`, an en or em dash, to, and, et or `à`, the other number goes with the amount, so `25,000-30,000 MAD` is one `[amount]`. The far end needs a marker, a suffix or three digits, so `paid 25000 and 3 days off` keeps the 3. A figure that reads as a year stays when a preposition stands right before it or a noun follows it (`paid in 2026`, `the salary 2026 budget`, but `my salary is 2000`, `she makes 2000`). A number has at most six groups after its first, so a long table of three-digit numbers costs linear time. In a longer chain, only the last seven groups before the currency are masked.

Out of reach, with the class on: a figure with an unlisted word after it, after a weak or near word (`she makes 25000 tomorrow`), because code reads the word as its noun; a figure before its pay word (`25000 is my salary`); a figure of three digits after a near word (`he gets 800`); a figure glued to a word by `-` (`salary-25000`), which has the shape of a reference. Masked with the class on, because a mask on dev text costs one span and no guard tells these from pay: a figure right before a period (`we ingest 25000 a month`, `3000/hour`), a figure after a strong word (`salary table has 300 rows`) and a figure after a weak word with no noun (`make the timeout 3000`).

**SC11. Overlaps** (spec line 2505). Spans that overlap merge into their union, and the lower step names the class. So a rename span inside an email or a handle replaces the whole identifier with the label and counts `renamed` only: `@orla` becomes `Co-founder`.

**SC12. Keys and numbering** (spec line 2513). The message key goes through steps 3 to 6, like the locator, so all four fields of a message are screened. Contact numbers follow first appearance: the locator, then for each message its sender, its text and its key. Numbering starts again for each source.

**SC13. Only the screener makes a `ScreenedSource`** (spec line 2505). It is a slotted class whose constructor takes a module-private token. A copy, a deep copy, a pickle and `dataclasses.replace` are refused, and setting or deleting an attribute raises. `counts` is a read-only mapping in the class order. The writer refuses a subclass too: it checks the exact type. Out of reach: `object.__new__` followed by `object.__setattr__` can still forge one, which no one writes by accident.

**SC14. Slot forms** (spec line 2558). An instruction file holds no slot of any kind. The check also refuses `$1`, `$(`, the OpenCode forms `` !`command` `` and an `@` token that holds a slash or a dot, because OpenCode substitutes them in command templates. Prose that needs one of these says it in words. The walk reads every file of the instruction folder, at any depth; every `*.md` under `runner/agents/`; and every string under a key named `prompt`, `template` or `description` in `runner/opencode.json`, duplicate keys included. A config that is not plain JSON, a missing instruction folder, a symlink and a file that is not UTF-8 are findings. Out of reach: files that the `instructions` array of `runner/opencode.json` pulls into a system prompt. The walk runs as a test; `grove.prompts.slots.check` has the lint signature, and no lint registers it yet.

**SC15. The prompt** (spec line 2486). The six characters that NFKC folds to `&`, `<` or `>` are also written as `&#x...;` references, and so are the common angle look-alikes that do not fold, such as U+2039, U+3008 and U+276E, so a look-alike cannot pass for a tag. A block with no source type or ref leaves that attribute out. The data rules always come from the package, and `root` only gives the folder of the named file. `instruction_name` is the name without its version: `grove.extract.v1` gives `grove.extract` and 1. Only `build_prompt` makes a `Prompt`. It refuses an instruction file with a slot or one that is not UTF-8, and `render_blocks` checks each block again and refuses any object whose type is not exactly `DataBlock`.

**SC16. The source row and its receipt** (spec lines 2516, 3103, 3108). Text from a connector reaches `sources` only through `write_screened_source` or `add_screened_source`. `record_source` takes text as given, for the writer's own tests, and `tests/screening/test_source_path.py` fails when code outside the writer package reaches for it. The screened ops refuse a connector the world does not have, as `record_source` does. `write_screened_source` writes exactly the catalogue's properties of `sources` except `content_pointer`, because the content is inline: `untrusted = true`, `forget_state = kept`, `version = 1`. One read of `clock.now(world_id)` per transaction gives `received_at`, `touched_at` and the receipt's `screened_at`, because screening finished before the transaction (SC1). `counts_per_class` is JSON with sorted keys and only the classes that occurred. A key outside `screening_class`, a count that is not an int (a bool is refused) and a count below 1 are refused, and the error names neither. `source_id` and `connector_id` must have the form of `new_id`. `parser_version` must be a code (`[A-Za-z0-9][A-Za-z0-9._:-]{0,63}`), so free text cannot ride in it. A source kept out whole is still a row, with the content `[]`, because its receipt needs the source id and the coverage table shows what was kept out (D13). The world id is bound by the layer and never a parameter; `WorldTx` has no public read of it, so `sources.py` reads the private one (see the open points).

**SC17. What screening cannot settle** (spec line 2552). There is no local runner, so a candidate that code cannot settle is treated as sensitive: it is masked or its message is dropped, and no span goes to a hosted model. `docs/data-flow.md` states this, and the `forget` lane states the final rule.

**SC18. Credentials by name and by phrase** (spec line 2509). One plain word after a password word is a value (`the password is sunshine`, `le mot de passe est azerty`, `the password for the wifi is lemonade`), because code cannot settle it and spec line 2552 treats it as sensitive; the stop list and the endings `-ing`, `-ed` and `-ly` keep prose, and the limit is one word of a sentence (`the password is plenty`). After `API key`, `token` or `secret`, one plain word stays prose unless it is said as someone's own, because a key is never a plain word. A name may carry an environment suffix (`API_KEY_PROD`, `SECRET_KEY_BASE`, `token_v2`). A bare `*_key` or `key` is no secret name, because `cache_key` and `sort_key` are everyday code; the entropy rule reads their values instead. A word glued in front of `password`, `passwd`, `pass`, `pwd`, `token` or `secret` still says secret, in any case (`PGPASSWORD`, `authtoken`, `dbPass`, `mysqlpass`), with a list of words that only end that way (`bypass`, `compass`, `oldpwd`). `AccountKey` and `SharedAccessKey` are names too, because a connection string reads as one URL. A session cookie is named like a key (`SESSION_COOKIE`). A subscript name counts (`config["password"] = "x"`). `==`, `=>` and `::` are no separators. A bare value is no secret when its first token is a language word or a type (`None`, `await`, `string`, `SecretStr` after `:`), a reference, a call, a subscript or a generic, or the same name passed on (`token=token`). A reference is `form.value`, `process.env.X` or `Settings.API_KEY`: no digit among letters and no capital after a dot unless the part is all capitals. A dotted name also counts when its last part says what it holds (`settings.neo4j_password`, `oauth2.token`, `s3.secret`). So `hunter2.Bank1` and `rabat.Summer2026` are values. A call has a plain callee with at most one short run of digits (`uuid4(`, `sha256(`), and its text closes the bracket or runs on past the space, so `Tr0ub(4)dor3`, `abcd1234(x)` and `Summer(2024)!` are values. `$NAME` in capitals, `$name` in lower case with no digit, `${` and `$(` are references, and `$uperS3cret` and `$ecret1234` are values. A value that opens with a bracket is a placeholder (`{password}`, `<your-password>`, `[REDACTED]`, `(optional)`) unless it holds a letter and a digit (`{Tr0ub4dor3}`). A value of only marks is a redaction. The `.env` form holds no code, so only a language word, a shell reference and the same name are guards there. A bare value runs to the next space, because a password can hold any symbol: `P@ss,w0rd9` and `O'Brien1!` go whole. The next field stops it (`password=x,user=y`, `Password=x;Server=y`, a trailing `#` comment, a flag), it loses a trailing `,`, `;` or a closer that opens nowhere in it, and it is read to 256 characters at most, so a long line costs linear time. After `:`, `=`, a spaced dash or an arrow, a password or passphrase name masks to the end of the line, because a passphrase holds spaces: `wifi password: my dog rex`, `password - my dog rex`. An upper-case name with `=` in the middle of a line is a command's variable, and it ends at the space (`docker run -e DB_PASSWORD=x img`). The phrase forms take the same guards, plus `passcode`, `is:`, a spaced dash and an arrow as a separator. `pw`, `pwd` and `pass` are common words, so their value must hold a digit or a symbol and not be a number: "pass: 1200" is a test count. After a key, token or secret word, a plain word after "is" or "was" is prose ("the API key is configured"), unless the text says whose it is ("my API key is sunshine"). `login:` and `creds:` with `user / secret` or `user:secret` mask the secret, and so do `creds are x / y` and, for a value with a digit or a symbol, `log in with x / y`; the user part is read to 256 characters, as long as an email address can be, so `login=login=...` costs linear time. `user x pass y` and `u: x p: y` count too, for a value with a digit or a symbol. Up to six words can stand between a password word and its value when that value holds a digit or a symbol, or is a number of four or more digits with no unit: "the password for prod is X", "password to the admin panel: X", "I set the password to X", "password? it's X". A list goes whole: "passwords are X and Y". An unquoted `NAME=value` at the start of a line, the `.env` form, is masked to the end of the line and keeps a trailing comment, so a command after an inline variable goes too. Known formats also cover PGP private key blocks, SendGrid, Google OAuth, DigitalOcean, Atlassian, Groq, Replicate, Stripe webhook, Perplexity, Telegram and Discord bot, all Slack, xAI, Shopify, Supabase, Notion, Resend, Vercel, Sentry, Vault, Terraform, Doppler, Databricks, PyPI, LangSmith, Together, Fly, Pulumi, GitLab runner and deploy, Twilio, Mailchimp, Meta, Instagram and age keys, and the path of a Slack, Discord or Microsoft Teams webhook URL. A prefix that reads like a word (`re_`, `vercel_`, `ntn_`) asks for a capital and a digit in the tail, so a long name in code stays. `X-Amz-Signature`, `X-Amz-Security-Token` and `X-Goog-Signature` are query secrets. Forms with no separator also count: `--password`, `--pass`, `--token`, `--api-key` and `--secret` and a value with a digit or a symbol; `-p` after `sshpass`, `docker login`, `podman login`, `cypher-shell`, `mongosh` and `mongo`, `-a` after `redis-cli`, and `-p` glued to its value after `mysql`, `mysqldump` and `mariadb`, because `-p` names a port for `psql`, `redis-cli` and `docker run` and `mysql -p db` names a database; `PASSWORD 'x'` and `IDENTIFIED BY 'x'` in SQL and Cypher; `auth=("u", "x")`, `BasicAuth("u", "x")` and `login("u", "x")`; `cfg.get("password", "x")` and `setenv("DB_PASSWORD", "x")` for a value with a digit or a symbol; `<password>x</password>`; a `name:` and a `value:` field in YAML (on two lines), JSON or an HTML input; and a YAML block after `password: >`. Cookie values are read only on a `Cookie` or `Set-Cookie` line and after `curl -b` or `--cookie`. A name that says session or auth goes whatever its value, another goes when its value mixes letters and digits at 8 or more characters, and `theme=dark` and the cookie attributes stay. `session` is no general context word, because `session_` ids fill dev text. An `Authorization` line masks a value of 8 or more characters with a digit, with any scheme or none. URL userinfo takes an empty user (`redis://:x@host`), `curl -u` and `curl --user` mask the secret, and `Basic <base64>` is masked when it decodes to text with a colon or when `Authorization` is before it on the line. A `Bearer` value with a digit counts at 8 characters, a `Bearer` value with no digit needs 20 or more characters, mixed case, and no reading as words, and `Bearer:` and `bearer=` count like the space. A PIN needs no separator: `PIN 4821`, `pin4821` and `PIN-4821` are masked, so `pin 1200 of the connector` is too, and a dotted version after it is not. `cvv`, `cvc` and `csc` with 3 or 4 digits are masked, because the card beside them is. French and Darija count too: `mot de passe` and `mdp` as password words, `est`, `c'est` and `howa` as separators, `dyal` and a word between (`l mdp dyal wifi: x`), a wifi code, and `jeton` and `clé` as context words. `password x`, `pwd x` and `mdp x` with only a space need a letter and a digit or a symbol in the value, and the value is no path and no name of an algorithm: "password sha256 hashing" and `pwd /tmp/grove` stay. Out of reach: a passphrase of plain words in prose ("the password is correct horse battery staple"), because code cannot tell it from a sentence; the same words with no label or no digit ("password hunter"); `pass x` with no user before it, because "pass value2 to the call" is dev talk; a labelled passphrase whose first word is a language word ("password: new moon rising"), because `password = new Foo()` reads the same way; a password that reads as a plain call or a plain lower-case reference (`hunter(2)`, `rabat.summer`) outside the `.env` form; and a lower-case `$word`. A mask on dev text stays where a guard would pass a real value: "password = pw or default" loses its tail, and "password: see the vault" and "we renamed the password to field2" are masked.

**SC19. Forms that glue a value to a word** (spec lines 2505, 2509 to 2513). Two forms put a word character right beside a value, so every word edge misses it, in every class: `_` chat markup (`_4111 1111 1111 1111_`, `*_Orla_*`, `__Kestrel__`, `_+212 612 345 678_`) and a backslash escape in text that holds JSON as a string (`pay with\n4111...` in a tool result or a transcript, also nested two or more times). The design's word rule counts `_` as a word character, and this ruling goes past it: a run of `_` is an edge when it opens after no word character or closes before none. `acme_corp`, `x_Orla` and `Orla_x` stay no match, and `_` before `@` stays, because `tamsin_@x.example` is an address. A closing `*`, `~` or `` ` `` is an edge in the same way, because it hides the end of a label or a sentence (`*Password:* x`, `**API_KEY**: x`, `*the code is 482910.*`, `*https://x.example/magic/...*`), except after `=`, where `API_KEY=***` is a redacted value. So each comparison copy has a read view of the same length, where these forms are written as space. The escapes are `\n`, `\t`, `\r`, `\b`, `\f`, `\"`, `\'` and `\uXXXX`; a line break and a quote stay what they are, because detectors read them. The rename map, the denylist and every detector read both the copy as written and the read view, in all four fields, and their spans merge. A drop in either view is a drop. Reading only the view would cut a secret that holds such a form (`sk-proj-Ab3_-dEf...`, `abc\ndef1`) in two and store a part of it, so the view can only add spans. A text with one of these forms costs two runs of the detectors. A private name is read too: `_token=X` is masked like `token=X`. `\/`, as PHP and some APIs write a slash in JSON, has no reading of the same length that makes `https:\/\/x` a URL, so each copy drops those backslashes, as it drops format characters, and the map keeps the original offsets. A number right after it then follows the plain rule of SC9: after `word/`, only the `+` form is a phone. Out of reach: an address right after an escape keeps the escape's letter in its value as written (`n` in `\norla@x.example`), so the mask takes that letter and the address can get a number of its own.

**SC20. A file name writes a space as `_`** (spec lines 2505, 2513; goes past the design's word rule for the locator alone). Upload tools and people write `WhatsApp_Chat_with_Orla_Vance.txt`. The locator has one more view, where every `_` is a space, and offsets stay one to one. The rename map, the denylist and the phone detector read it: `kestrel_export.json` keeps the source out, and `export_0612345678.zip` becomes `export_[phone 1].zip`. So `kestrel_corp.txt` is also kept out, which is the safe side. Message text, a sender and a key keep the rule of SC19: `acme_corp` stays one word there. Credentials do not read the view, because a key format holds `_` (`ghp_...`). Emails and handles do not read it, because the view would cut an own address such as `me_x@own.example`, and the plain view already takes a whole address. Out of reach in a locator: other separators and encodings (`Brightwell-Logistics.zip` against a two-word term, `WhatsApp%20Chat%20with%20Kestrel.txt`), and a key or a card number glued to a word by `_` (`export_ghp_...`, `export_4111 1111 1111 1111.txt`).

## Ontology (lane `ontology`)

D8's floor (Appendix D, P0 item 8): provisional mints with a recorded reason, their declared fields and their two recorded judgments, the earning bar, and the change log. Merges, splits, renames, retirements and their undo are P1 item 3, so they are not built; the change log stores the inverse each one needs. The rulings O1 to O13 are this lane's. `server/grove/writer/ontology.py` holds the writer's side, `server/grove/writer/judgments.py` the judgments writer, `server/grove/models/judge.py` the two judges, and `server/grove/ontology/plan.py` the path from extraction's proposals to a batch.

**O1. `propose_word`** (spec lines 1593, 3181). Callers U, C and K, as B.2's propose. The fields are `word_type` (`kind`, `meaning`, `property_name`), `name`, `description`, `reason`, `worked_example`, `neighbours`, `declared`, `judgment_ids`, `kind` (a property name's kind, and only a property name's) and `proposed_capability`. The description, the reason and each distinction are value ids. A minted word is usable by its name in every later operation of the batch: the batch's registry learns the row at once.

**O2. Reuse first** (spec lines 1587 to 1592). Reuse first runs before every other check of a proposal (a property name's kind is resolved first, because names are per kind). A name that the resolver takes to a usable word of its type is refused with `invalid_arguments`, rule `word_exists`, and so is a name whose normal form a retired or split word holds, because `(world_id, name)` is unique and a retired word comes back by its inverse, never by a second mint. A primitive name is refused with rule `primitive_word`, also after a leading "is" or "are", because the resolver drops the copula (S44), and so is any operation that names a primitive row.

**O3. Judgment subjects** (spec lines 1594, 1667, 3121; S32). A same-word judgment has the role `same_word`, the verdict `same` or `different`, and the subject of the pair `value:<history value id>` and `<word type>:<nearest neighbour id>`, sorted and joined by `|`. The value holds the JSON `{name, reversed, word_type}`: the proposed name and the reading it was judged in (O15), so a verdict on one reading never counts for the other. A field-fit judgment has the role `field_fit`, the verdict `fits` or `does_not_fit`, and the subject `value:<history value id>`, where the value holds the JSON `{field, kind, name, value, word_type}`: the declared value and a property name's kind are in the subject, so a verdict on one declared value, or on the same property name of another kind, never counts. The writer reads the values and refuses any cited judgment that judged something else (`unrecorded_verdict`, rule `judgment_subject`), carries another role (`judgment_role`) or verdict (`judgment_verdict`), or names no recorded model call (`judgment_call`).

**O4. Two models** (spec lines 1594, 1668). A proposal with neighbours cites at least two same-word judgments from two model calls, of two models, or of one model with the second judgment marked `correlated` (the Spark fallback). Otherwise `second_judge_required`, rule `two_models`; no judgment at all is rule `judgments_required`. All of them `same` make the phrase an alias of the nearest neighbour, canonical, with origin `minted`, reversed when the proposal states it reads the neighbour from the other end (O15). Any other set mints the word provisional, because a provisional word is visible, dated and easy to merge, and a wrong alias changes every link written with it.

**O5. Disputed fields** (spec line 1595). The disputable fields are the temporal class when it is not state, the consequence flag when it is true, a kind's status-role mapping when it has statuses, and the proposed capability. A field that a cited field-fit judgment disputes, or that no field-fit judgment covers, takes the safer value: state with the declared horizon, or 30 days when none is declared (the shortest starter horizon); no consequences and no downstream end; no capability. A status-role mapping has no safer value, so it stays as declared. Each disputed field is listed in `disputed_fields` (JSON codes) for the next hygiene pass. A state property sits only on a record (D1, D4), so a property name of state on a kind that is not a record is refused at the mint, declared or settled by a dispute (`state_property_on_item`): it could sit on no item of its kind. `settle` gives what a word stores after its judgments, and the writer and the planner both call it.

**O6. The valve** (spec line 1597, Appendix C). A batch of U, C or K mints at most 3 words (outcome mint; an alias does not count), else `invalid_arguments`, rule `mint_valve`. `plan_words` takes one proposal per word (a second proposal of the same name writes with the first), holds a proposal whose judging fails on its own fault (rule `judge_failed`; a budget stop or a run cap still stops the pass), stops judging at the valve and returns the rest, and settles each judged word as the writer will (`settle`: the direction guard of an alias, O15, and a usable mint): a word that would settle unusable is held, or a meaning rides on the generic meaning. The plan checks each proposal against its own copy of the registry, which learns each planned word as the writer's batch registry does, so a later proposal of a planned word in any form the resolver takes to it (its normal form, a leading "is" or "are", an alias, another spelling of a property name's kind) is reused, and a word the plan minted is no neighbour of a later proposal (`neighbour_unknown`) and does not raise the count asked for, in the plan and in the writer alike. A word the judges set aside stays aside for the rest of the plan, and a property name of a kind the same plan mints is held (rule `kind_in_this_plan`), because its field-fit subject would bind the planned kind. The plan returns how each word settled (`words`: outcome, temporal class, horizon, disputed fields). `fit_links` resolves each link phrase with the resolver against the plan's registry: a phrase that resolves only through a planned word gets a re-check by from that word's horizon when it settled as a state (a disputed mint, an alias of a state meaning); a phrase sent to the generic meaning is rewritten to it with the phrase kept, and gets the generic meaning's horizon; a phrase that names an existing word stays as the caller wrote it. So the writer never sees the fourth mint and never declines a planned operation or a fitted link for the plan's judgments.

**O7. The worked example** (spec line 1575). `worked_example` names an observation of the batch (`{ev_id}`) or an active evidence row (`{evidence_id}`). At the end of the batch the writer stores the evidence row, written or cited in this batch with that message key, whose target uses the word: a link of the meaning (or of the neighbour, for an alias), an item of the kind, an item that carries the property. None is `invalid_arguments`, rule `worked_example_unused`. The phrase is read from the source, never copied into the registry.

**O8. Usage and the earning bar** (spec lines 1580, 1603). At the end of every batch that commits, each word that a new supporting evidence row of the batch reached gains `uses` for each item or link the batch created with it. A property name counts only where the batch wrote it: an item it created with a value for the property, or an update that sent that key with a value that differs from the properties it merged onto (the head, or the as-of properties for a late fact that goes to history only). Its episodes are only those of the supporting rows that the writing operation created: evidence that confirms an item but writes no property is no use of its property names, also in the same batch, and a late fact about one property is no use of the others. Each word gains `first_used` once, `last_used` from the batch's clock, and one `distinct_episodes` for each new episode. A provisional word keeps the episodes it counted in `earning_episodes` (JSON ids), and an episode in that list never counts again, whatever happened since: a correction that ends a row and attaches another from the same conversation, or a property removed from one item and set on another, earns nothing. A canonical word counts an episode of the batch that held no earlier supporting row, active or ended, on a target that uses the word now; that count gates nothing, and for a property name moved between items it can drift. A provisional word at 3 distinct episodes becomes canonical in the same transaction, with a change entry, a change log row with the actor `writer`, and a receipt line. A count moves no version and writes no change entry: it is bookkeeping that the rows recompute. A row ended later does not lower the count. Counts start with this lane: rows written before it hold 0.

**O9. `promote_word`** (spec lines 1603, 3181). Callers K and P. K needs the earning bar (`earning_bar`); the person's word is the strongest evidence, so P promotes at once. A starter or primitive row is refused (`primitive_word`), and so is a word that is not provisional (`already_canonical`). The writer reads the caller's kind from the stamp (`Stamp.caller_kind`).

**O10. The change log** (spec line 1584). One `:OntologyChange` per mint, alias and promotion: the operation (`propose`, `promote`), the rows before and after as JSON text with text fields as value ids, the inverse (`retire` for a mint or an alias, `promote` back to provisional for a promotion), the reason, the worked example as its evidence id, the judgment ids, the actor and the time. Every registry change also writes a change entry of `entity_type = registry_row`, so a rebuild reproduces the row. A capability minted with a word is one of those rows: it has its change entry, it is in the mint's rows after, the inverse names it under `with`, and the receipt prints it as `o capability <connector type> <capability> for <word>`. `undone_by` changes after the write, so the row carries `version` and `touched_at` (S12).

**O11. The mechanical boundary** (spec lines 1641, 1689; the Oct 2 drill). No operation of an agent writes the registry or a primitive: a registry op name is `unknown_op`, an item operation on a registry id is `not_found`, a declared field the writer sets is `writer_field`. Inside the writer package, the writer lint allows write Cypher on `:Kind` and `:Meaning` only in `ontology.py` and `worlds.py`, on `:PropertyName` and `:OntologyChange` only in `ontology.py`, and on `:Judgment` only in `judgments.py` (rule `writer/registry`). Alias rows stay outside that rule, because other operations write item aliases. The runner service holds no store credential and shares no network with the store, and the store refuses a connection without a credential. An edited seed never reaches a world that exists: `create_world` seeds once.

**O12. The judges** (spec lines 1666 to 1668; D15). `judge_word` runs in the model-call layer, before the batch: Spark first (job `word_judge`), Glimmer second (job `second_opinion`, the same question and the field fit), and, when Glimmer cannot be reached (not configured, a provider error, a rate limit, an invalid answer), a second Spark call with another prompt (job `second_opinion_fallback`) whose judgments are `correlated`. A budget or a run cap stops the pass, so its requests stay queued. The second judge never sees the first verdict. `record_judgments` (S20) stores one call's verdicts in one transaction and refuses a call that is not a model call of the world.

**O14. A value that names an item** (spec lines 1650, 1689, 2010). A text property value that is an item's id, its title (case aside, then the normal form) or a usable alias of it is refused with `invalid_arguments`, rule `value_names_item`, and the details name the item, so the caller writes a link. An update checks only the values it sends, so a stored value that a later item's title happens to match never blocks an update of another property. The item that holds the value is never the item it names, so create and update read the same. A retired or split item alias holds its history only, as a word's does: it scores nothing at the near-duplicate gate or in the candidate band.

**O15. The direction of an alias made from a mint** (spec lines 1590, 1595, 1633). A meaning proposal may state `reversed: true`: the phrase reads its nearest neighbour from the other end, as "is a prerequisite for" reads "depends on". The judges see the stated reading and compare the phrase in it; the same-word subject binds it (O3); the alias carries it, so a link written with the phrase swaps its ends. The judges' instructions say that a phrase read from the other end, when the proposal says it is not, is a different word. The writer also refuses an alias whose declared ends fit the neighbour only the other way round (`invalid_arguments`, rule `alias_direction`). What no code can catch: two models that both call a reversed phrase the same word, between two ends of any kind. A text of the proposal is at most 2,000 characters and a name at most 120 (rule `too_long`).

**O13. Value lists** (spec lines 1583, 3241). `meanings.direction` is `from_to` only: every meaning runs from its first endpoint to its second, and a reversed phrasing is a reversed alias. `property_names.value_type` is one of `text`, `number`, `boolean`, `date`, `instant`; a minted property name declares one. The writer does not yet check a property value against its type.

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
| (none) | `disputed_fields` | `json_ids` | ruling | O5, line 1595: the declared fields the second model disputed, for the next pass |
| (none) | `earning_episodes` | `json_ids` | ruling | O8, line 1603: the episodes a provisional word counted toward the earning bar |
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
| (none) | `disputed_fields` | `json_ids` | ruling | O5, line 1595: the declared fields the second model disputed, for the next pass |
| (none) | `earning_episodes` | `json_ids` | ruling | O8, line 1603: the episodes a provisional word counted toward the earning bar |
| from kinds | `from_kinds` | `json_ids` | b1 | kind ids, a lifecycle class or any |
| to kinds | `to_kinds` | `json_ids` | b1 | kind ids, a lifecycle class or any |
| direction | `direction` | `enum:direction` | b1 | line 3241: from the first endpoint to the second |
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
| value type | `value_type` | `enum:value_type` | b1 |  |
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
| (none) | `nearest_neighbours_at_mint` | `json_ids` | ruling | O1, line 1578: the two neighbours of a minted property name, as on kinds |
| (none) | `disputed_fields` | `json_ids` | ruling | O5, line 1595: the declared fields the second model disputed, for the next pass |
| (none) | `earning_episodes` | `json_ids` | ruling | O8, line 1603: the episodes a provisional word counted toward the earning bar |

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
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

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
| (none) | `provider` | `code` | ruling | the upload format of an upload connector: claude_code, codex, chatgpt, claude_ai (ingest-a1); one row per world, type and provider |
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

A request commits once, in one batch, and `saved` moves in the same transaction as that commit (S41). A filter in MATCH or WHERE reads with no lock, so a transition checks the row after it holds the row's write lock (S1).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| id | `id` | `id` | b1 | the caller's idempotency key, unique per world (S9, S41) |
| world_id | `world_id` | `id` | b1 |  |
| caller | `caller` | `code` | b1 | the actor code of the first writer |
| source id of the request's text | `source_id` | `id` | b1 | only a row with one can be claimed (S41) |
| run id | `run_id` | `id` | b1 |  |
| status | `status` | `enum:request_status` | b1 |  |
| attempts | `attempts` | `int` | b1 |  |
| created at | `created_at` | `instant` | b1 |  |
| committed at | `committed_at` | `instant` | b1 |  |
| receipt | `receipt` | `json_ids` | b1 | ids and codes; a title is read fresh when shown. A declined row holds its stored decline here (S42) |
| last error code | `last_error_code` | `code` | b1 |  |
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |
| (none) | `commit_id` | `id` | ruling | S41: the one commit of this request |
| (none) | `change_set_id` | `id` | ruling | S41: the one change set of this request |
| (none) | `fingerprint` | `code` | ruling | S41: sha256 of the committed batch |
| (none) | `declined_fingerprint` | `code` | ruling | S41: sha256 of the batch last declined |
| (none) | `repairs` | `int` | ruling | S41: 1 once the one repair attempt is spent (line 748) |
| (none) | `retry_at` | `instant` | ruling | S41: when a queued or failed row is due again |
| (none) | `lease_until` | `instant` | ruling | S41: when an applying row is due again |
| (none) | `claimed_by` | `code` | ruling | S41: the worker that holds the lease |
| (none) | `content_key` | `code` | ruling | S41: what a second enqueue under this key compares |
| (none) | `verbatim` | `bool` | ruling | S41: B.3 remember, for the later pass |
| (none) | `host_message_id` | `locator` | ruling | S41: B.3 remember, for the later pass |
| (none) | `said_at` | `instant` | ruling | S41: B.3 remember, for the later pass |
| (none) | `about` | `ids` | ruling | S41: B.3 remember, for the later pass |

### declines

`:Decline`, node. Scope world. Restore group snapshot. Owner D2.

Keys: `declines_world_id_request_id_op_id_unique` (world_id, request_id, op_id).

Indexes: none.

Ids and codes only: a refusal's message and details are never stored, and an op id that could hold text is stored by its hash (S42). `op_id` is `batch` for a fault of the whole batch.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| world_id | `world_id` | `id` | ruling | S5: line 3081 wins over the table's own list |
| request id | `request_id` | `id` | b1 | a requests id (S9) |
| op id | `op_id` | `code` | b1 |  |
| rule | `rule` | `code` | b1 |  |
| near-duplicate candidate ids with scores | `nearduplicate_candidate_ids` | `json_ids` | b1 | list of {item id, score} |
| override used | `override_used` | `bool` | b1 |  |
| (none) | `code` | `code` | ruling | S42: the typed error code of the refusal |
| (none) | `recorded_at` | `instant` | ruling | S42: when the decline was logged |

### jobs

`:Job`, node. Scope world. Restore group snapshot. Owner A.1, D10, D15.

Keys: `jobs_id_unique` (id).

Indexes: `jobs_world_id_lane_state_priority_run_after_idx` (world_id, lane, state, priority, run_after).

There is no SKIP LOCKED. A filter in MATCH or WHERE reads with no lock, so two runners that filter on state = queued both claim one job. A claim must check the state after it holds the node's write lock (S1): first a write to another property of the job, then WITH, then the check, then the change; or the check inside a CASE on the right side of SET, with the caller comparing the returned claimed_by. The runner claims a job only under the world lock (`grove.writer.runs`), and `tests/runner/test_runs.py` holds the two-transaction race test.

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
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

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
| version | `version` | `int` | ruling | S12, line 3081 |
| touched at | `touched_at` | `instant` | ruling | S12, line 3081 |

### model_calls

`:ModelCall`, node. Scope world. Restore group snapshot. Owner D15.

Keys: `model_calls_id_unique` (id).

Indexes: `model_calls_world_id_day_idx` (world_id, day).

Written only by the model-call layer (S20), with the budget it spends in the same transaction (S39).

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
| (none) | `route` | `code` | ruling | S39: meta or modal |
| (none) | `outcome` | `code` | ruling | S39: ok, invalid_output, rate_limited, provider_error, client_closed |
| (none) | `reasoning_tokens` | `int` | ruling | S39, line 2694: the reasoning share of the output tokens |
| (none) | `latency_ms` | `int` | ruling | S39, line 2694: from the request to the answer |
| (none) | `usage_estimated` | `bool` | ruling | S39: true when the tokens are an estimate |

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

Written by the model-call layer, with each model call, and by the operator's ceiling (S39).

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

A B.1 table read through the platform query layer: a client exists before any world consents to it. It restores with the snapshot tables (S21). A person's consent is an oauth_grants row (S45). Only server/grove/writer/oauth.py writes it.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| client id | `client_id` | `locator` | b1 | S32: a UUIDv7 from dynamic registration, or the URL of a Client ID Metadata Document (lines 3036, 3148) |
| world_id | `world_id` | `id` | b1 | S45: always absent; the oauth_grants row holds it |
| (none) | `user_id` | `id` | ruling | S45: always absent; the oauth_grants row holds it |
| host | `host` | `config` | b1 | the client's self-declared client_name, control characters removed, at most 100 characters; a host's registration metadata (line 2561) |
| registration type | `registration_type` | `enum:registration_type` | b1 |  |
| redirect URIs | `redirect_uris` | `config` | b1 | a list of URIs; a host's registration metadata (line 2561) |
| scopes granted | `scopes_granted` | `code` | b1 | S45: always absent; the oauth_grants row holds it |
| granted at | `granted_at` | `instant` | b1 | S45: always absent; the oauth_grants row holds it |
| revoked at | `revoked_at` | `instant` | b1 | S45: always absent; the oauth_grants row holds it |
| (none) | `token_endpoint_auth_method` | `code` | ruling | none, client_secret_post or client_secret_basic; a metadata document client is none |
| (none) | `client_secret_hash` | `code` | ruling | the sha256 hex of the client secret; absent for a public client (S45) |
| (none) | `registered_at` | `instant` | ruling | when the row was made |
| (none) | `fetched_at` | `instant` | ruling | a metadata document client's last fetch |

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
| (none) | `self_item_id` | `id` | convention | S40: the person's own item, set in the transaction that creates it; absent before |

### users

`:User`, node. Scope platform. Restore group platform. Owner platform; signin.

Keys: `users_id_unique` (id); `users_email_hash_unique` (email_hash).

Indexes: none.

The signin lane owns any further column.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `email_hash` | `code` | convention | S40: sha256 of the salted address; secrets and contact details stay out of the row |
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
| (none) | `method` | `code` | convention | S40: google or email; a planted test session has none |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `revoked_at` | `instant` | convention |  |

### oauth_tokens

`:OAuthToken`, node. Scope platform. Restore group platform. Owner platform; hosts-a.

Keys: `oauth_tokens_id_unique` (id); `oauth_tokens_token_hash_unique` (token_hash).

Indexes: `oauth_tokens_client_id_idx` (client_id); `oauth_tokens_user_id_idx` (user_id); `oauth_tokens_family_id_idx` (family_id).

The hosts-a lane owns any further column. Only server/grove/writer/oauth.py writes it.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `client_id` | `locator` | convention | an oauth_clients client id (S32) |
| (none) | `user_id` | `id` | convention |  |
| (none) | `world_id` | `id` | convention | S5: tokens are bound to one world (line 3038), the grant's world |
| (none) | `token_hash` | `code` | convention | S45: the sha256 hex of the token; the token itself is not kept |
| (none) | `token_type` | `code` | convention | access or refresh |
| (none) | `scopes` | `code` | convention | a list of scope codes |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `revoked_at` | `instant` | convention |  |
| (none) | `family_id` | `id` | convention | the id of the code the chain began with; every refresh of that chain keeps it |
| (none) | `grant_id` | `id` | convention | an oauth_grants id |
| (none) | `resource` | `config` | convention | the canonical <base>/mcp |
| (none) | `used_at` | `instant` | convention | a refresh token that was rotated |

### authorization_codes

`:AuthCode`, node. Scope platform. Restore group platform. Owner platform; hosts-a.

Keys: `authorization_codes_id_unique` (id); `authorization_codes_code_hash_unique` (code_hash).

Indexes: none.

The hosts-a lane owns any further column. Only server/grove/writer/oauth.py writes it.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention |  |
| (none) | `client_id` | `locator` | convention | an oauth_clients client id (S32) |
| (none) | `user_id` | `id` | convention |  |
| (none) | `world_id` | `id` | convention |  |
| (none) | `code_hash` | `code` | convention | S45: the sha256 hex of the code; the code itself is not kept |
| (none) | `code_challenge` | `code` | convention | PKCE S256 |
| (none) | `redirect_uri` | `config` | convention |  |
| (none) | `scopes` | `code` | convention | a list of scope codes |
| (none) | `created_at` | `instant` | convention |  |
| (none) | `expires_at` | `instant` | convention |  |
| (none) | `used_at` | `instant` | convention |  |
| (none) | `grant_id` | `id` | convention | an oauth_grants id |
| (none) | `resource` | `config` | convention | the canonical <base>/mcp; absent when the request named none |
| (none) | `redirect_given` | `bool` | convention | the authorize request named redirect_uri |
| (none) | `reused_at` | `instant` | convention | S45: the first redeem after the code was used; its family gets no more tokens |

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

## Tables of later lanes

### map_positions

`:MapPosition`, node. Scope world. Restore group snapshot. Owner views-a.

Keys: `map_positions_id_unique` (id); `map_positions_world_id_item_id_unique` (world_id, item_id).

Indexes: none.

Where the map drew an item first. Insert-only: a position is never moved, so an item keeps its place after a reload and after new items arrive. Only server/grove/writer/positions.py writes it. One row per item: the (world_id, item_id) key turns a second placement that races the first into a kept one. A view preference, not a claim about the world, so it has no history and restores with the snapshot.

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention | UUIDv7 text from new_id() (S4) |
| (none) | `world_id` | `id` | convention | the world of the item |
| (none) | `item_id` | `id` | convention | an item id of the same world |
| (none) | `x` | `float` | convention | the map's own units; finite, at most 1e7 either way |
| (none) | `y` | `float` | convention | the map's own units; finite, at most 1e7 either way |
| (none) | `placed_at` | `instant` | convention | from the clock provider, when the map first placed it |

### oauth_grants

`:OAuthGrant`, node. Scope platform. Restore group platform. Owner hosts-a.

Keys: `oauth_grants_id_unique` (id); `oauth_grants_client_id_world_id_unique` (client_id, world_id).

Indexes: `oauth_grants_world_id_idx` (world_id).

S45: a person's consent to a host, one row per (client, world). A new consent to the same host from the same world finds the row, sets its scopes and time and clears revoked_at. A revoke sets revoked_at and revokes every token of the grant in the same transaction. Only server/grove/writer/oauth.py writes it. A platform row: it restores as it was (S21).

| Spec column | Property | Type class | Origin | Note |
|---|---|---|---|---|
| (none) | `id` | `id` | convention | UUIDv7 text from new_id() (S4) |
| (none) | `client_id` | `locator` | convention | an oauth_clients client id (S32) |
| (none) | `world_id` | `id` | convention | the world the person let the host reach |
| (none) | `user_id` | `id` | convention | the person who consented last |
| (none) | `scopes_granted` | `code` | convention | a list of scope codes |
| (none) | `granted_at` | `instant` | convention | the last consent |
| (none) | `revoked_at` | `instant` | convention | absent while the grant is live |

## Full-text indexes

| Name | Labels | Properties | Analyzer |
|---|---|---|---|
| `grove_items_lexical` | `Item` | `world_id`, `title`, `summary`, `body`, `property_values`, `search_terms` | `standard-folding` |
| `grove_aliases_lexical` | `Alias` | `world_id`, `phrase`, `normalised_phrase` | `standard-folding` |
| `grove_past_values` | `PastValue` | `world_id`, `value` | `standard-folding` |
| `grove_value_index` | `Item`, `Alias`, `HistoryValue`, `Kind`, `Meaning`, `PropertyName`, `RunRecord` | `world_id`, `title`, `summary`, `body`, `property_values`, `search_terms`, `phrase`, `normalised_phrase`, `value`, `name`, `proposed_operations` | `standard-folding` |

<!-- generated:end -->
