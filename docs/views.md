# The views API

The person's views (the graph, a node, the schema and the health panel) read the store through the API process. Every endpoint lives under `/v1/views/`, answers only a signed-in session, and returns the shapes of the simulation of the views, so the web page reads live data where the simulation read its fixture.

The code: `server/grove/api/` holds the FastAPI app, the typed errors and the session dependency. `server/grove/views/` holds the reads, as functions over `WorldQuery` and `PlatformQuery`, and writes nothing. The one write, a map position, goes through `server/grove/writer/positions.py`.

## Run it

From `server/`:

```sh
set -a; . ~/.grove/neo4j-dev.env; set +a
uv run uvicorn --factory grove.api.app:create_app --host 127.0.0.1 --port 8765
```

`create_app` opens the store driver at startup from `NEO4J_AUTH` and `GROVE_NEO4J_URI` (default `bolt://127.0.0.1:57687`) and closes it at shutdown. A caller that hands in a driver, as the tests do, owns it.

`GROVE_WEB_DIST` names the folder of the built pages, for example `GROVE_WEB_DIST=../web/dist` in a local run. `GET /` returns its `home.html` with no session cookie and its `index.html` with one; `docs/signin.md` has the page routes. In production the API image builds the pages and sets it, and nginx passes every path to this process.

### A synthetic world to look at

`grove.writer.demo` is a dev tool. It seeds a `t_` world from `web/checks/fixture.synthetic.json` (invented names) and refuses every other world id:

```sh
uv run python -m grove.writer.demo seed --fixture ../web/checks/fixture.synthetic.json --token-file <path>
uv run python -m grove.writer.demo add --world <id> --count 12
uv run python -m grove.writer.demo purge --world <id>
```

`seed` writes the session token to the token file with mode 600 and prints only the world id and the counts. Put the token in the browser's `grove_session` cookie for `127.0.0.1` to open the views of that world. The session lasts one hour. A fixture link with `invalid_at` has ended, so it gets its stopped bound and no `active_key`, and the graph leaves it out.

### The browser test

`server/tests/e2e/test_views_browser.py` drives Chromium against the real app and a seeded world: the sign-in card, the welcome, and a map that keeps every placed item after a reload and after new items arrive. It is marked `browser`, the default run leaves it out, and CI has no Playwright, so it never runs there:

```sh
GROVE_E2E_SHOTS=<folder> uv run --with playwright==1.58.0 python -m pytest -m browser tests/e2e -q -p no:cacheprovider
```

Keep `python -m pytest`: the `pytest` script in `.venv/bin` does not see the Playwright that `uv run --with` adds. A run that selects these tests with no Playwright fails and names this command, so it never looks green with nothing tested.

`docs/screens/views-a/` holds its screenshots beside the simulation's, both on synthetic data.

## The session contract

Signin owns the session rows and the cookie (`docs/signin.md`). The views only read them.

- The cookie is `grove_session`. Its value is a random token.
- `:Session.token_hash` is the lowercase sha256 hex of that token. The token itself is never stored.
- A session is valid while `revoked_at` is absent and `expires_at` is later than the clock provider's now.
- `:Session.user_id` leads to the `:World` whose `owner_user_id` is that user. When there are several, the oldest by `created_at` wins.
- `:Session.method` is `google` or `email`. A session from before that column has none.
- `:World.self_item_id`, which may be absent, names the person's own item. Signin writes it in the transaction that makes the item (S40 in `docs/store-map.md`).
- Signin must create the user's `:World` row before it sets the cookie. A valid session whose user owns no `:World` reads as signed out: it gets the same 401 as no session.

No valid session means a 401 for every endpoint, before any parameter is read.

## Errors

Every error is typed (B.3) and can never look like an empty result:

```json
{"error": {"code": "invalid_arguments", "message": "as_of must be an ISO 8601 date and time with a timezone", "details": {"field": "as_of"}, "retryable": false}}
```

| Status | Code | When |
|---|---|---|
| 400 | `invalid_arguments` | A bad `as_of` or `known_as_of`, or a bad positions body. `details.field` names the field, and the message never repeats the value. |
| 401 | `unauthenticated` | No cookie, an unknown, expired or revoked session, or a session whose user owns no `:World`. |
| 404 | `not_found` | An unknown route, or a node id that is no item of this world (or no item in the time window). |
| 500 | `internal` | A failure of the server. |
| 503 | `unavailable` | The store cannot be reached. `retryable` is true. |

Every answer under `/v1/views/` carries `Cache-Control: no-store`.

## Time parameters

`GET /v1/views/graph` and `GET /v1/views/node/{id}` take two optional parameters. Each is an ISO 8601 date and time with a timezone, such as `2026-10-01T09:00:00Z` or `2026-10-01T14:30:00+05:30` (encode the `+` in a URL). A date alone, a time with no timezone, empty text, or a time whose offset takes it past the years 1 to 9999 in UTC (`0001-01-01T00:00:00+23:59`, `9999-12-31T23:59:59-23:59`) is a 400.

- `known_as_of=T` keeps the rows recorded at or before T (`recorded <= T`). A row with no `recorded` is left out.
- `as_of=T` keeps the rows whose world interval holds T: `became_true_earliest` is absent or at or before T, and the stopped bound (`stopped_being_true_latest`, else `stopped_being_true_earliest`) is absent or after T.

Both filter the current head of each row. There is no history read yet, so a row that changed after T shows its newest values; see the open points of `docs/store-map.md`.

## Shapes

Times are UTC in the form of JavaScript's `toISOString`, `2026-11-02T09:30:00.000Z`, or `null`. A kind or a meaning shows by its name; an item whose kind row is missing has the type `unknown`, and a link whose meaning row is missing has the verb `unknown`. Kinds come from the world's rows and the primitive rows.

### A node, N

| Key | Value |
|---|---|
| `id` | The item id. |
| `label` | The title, `''` if none. |
| `type` | The kind name. |
| `summary` | The summary, `''` if none. |
| `status` | The status word, `''` if none. |
| `file_path` | Always `''`. |
| `valid_at` | `became_true_earliest`, else `became_true_latest`, else `recorded`. |
| `invalid_at` | `stopped_being_true_latest`, else `stopped_being_true_earliest`, else `null`. |
| `created_at` | `recorded`. |
| `updated_at` | `touched_at`, else `recorded`. |
| `status_since` | `status_since` or `null`. |
| `verified_at` | `last_confirmed` or `null`. |
| `version` | An integer, 1 if absent. |
| `properties` | The parsed JSON object of `properties`, `{}` if none. |
| `has_changes` | `version > 1`. |
| `x`, `y` | Only when the item has a map position. |
| `self` | `true`, only on the person's own item. |

### An edge, E

| Key | Value |
|---|---|
| `id` | The link id. |
| `source`, `target` | `from_item`, `to_item`. |
| `label`, `verb` | The meaning name. |
| `cardinality` | The meaning's cardinality, `many` if absent. |
| `canonical`, `canonical_of` | Always `false` and `null`. |
| `valid_at`, `invalid_at` | As for a node, from the link's own clock. |
| `created_at` | `recorded`. The record time, as for a node: `valid_at` can be years older. |
| `note` | Always `''`. |
| `source_field` | Always `store`. |

The graph shows a link only when both of its ends are in the node list, and only while it is active (`active_key` present). With `as_of`, an ended link also shows when its stopped bound says it held at that time.

## Endpoints

### `GET /v1/views/session`

```json
{"signed_in": true, "self_item_id": "<id>" | null, "has_items": false, "account": "<user id>", "method": "google" | "email" | null}
```

`self_item_id` is the person's own item when it names an item of this world. `has_items` is true when the world holds an item other than the own item. `account` is the user id, and `method` is the session's method. `POST /v1/auth/email/verify` answers the same object.

The page asks this once at load, and only a 401 ends its local sign-in. After any other failure, a person signed in on this device keeps the sign-in and the map, and the offline text shows over the page. A person who is not sees the sign-in card with that text, and no graph of an unknown session stays behind the card.

### `GET /v1/views/graph[?as_of=T&known_as_of=T]`

```json
{
  "elements": {"nodes": [{"data": N}], "edges": [{"data": E}]},
  "stats": {"total_nodes": 0, "total_edges": 0, "by_type": {"<kind>": 0}, "embedded": 0, "edge_verbs": ["<meaning>"]},
  "time": {"min": "YYYY-MM-DD" | null, "max": "YYYY-MM-DD" | null},
  "generated_at": "<instant>",
  "as_of": "<instant>" | null,
  "known_as_of": "<instant>" | null,
  "self_item_id": "<id>" | null
}
```

`embedded` equals `total_nodes`. `edge_verbs` is the sorted set of verbs in the edge list. `time.min` is the first `valid_at` day of the nodes and edges, and `time.max` the last `valid_at` or `invalid_at` day. `self_item_id` is set only when the own item is in the node list.

### `GET /v1/views/node/{id}[?as_of=T&known_as_of=T]`

```json
{
  "node": N + {"title": "<label>", "content": "<summary, else body, else ''>", "status_field_value": "<status>", "clocks": C},
  "required_props": [],
  "optional_props": ["<property names of the item's kind>"],
  "required_prop_names": [],
  "status_field": "status"
}
```

A 404 for an unknown id, an id of another world, or an item outside the time window.

`C` is the item's clocks in words (D4), from `clock_words` in `server/grove/store/words.py`, read at the clock provider's now. It is in the node detail only, never in the graph list. Dates read in the person's timezone when the item's creating evidence row holds `person_timezone`, else in UTC. A source is named by its connector type in words, never by a title or a locator.

```json
{
  "last confirmed": "today 09:15, against a fetched page" | "3 Mar, against a file; re-check was due 3 Jun" | "never confirmed, from a chat message of 26 Oct",
  "re-check by": "Thu 29 Oct, by checking a fetched page again" | "was due 3 Jun" | "not set",
  "became true": "unknown" | "July 2026" | "before 26 Oct" | "12 Jul",
  "recorded": "26 Oct, 22:40, from a chat message",
  "source type": "confirmed (said in a chat message; confirmed by a fetched page)" | "inferred from 3 confirmed records",
  "state": "current" | "planned" | "reported" | "not_started" | "ended" | "overtaken"
}
```

A year shows only when it is not the current year. `today` and `yesterday` are against the world's now.

### `GET /v1/views/schema`

`{"<kind name>": {"required": [], "optional": ["<property names>"], "status_field": "status", "status_vocab": ["<status words>"]}}` for every kind of the world and every primitive kind. The status words come from the kind's `status_vocabulary` in its order.

### `GET /v1/views/health`

```json
{
  "status": "healthy",
  "summary": "Your Grove is ready",
  "generated_at": "<instant>",
  "checks": {"graph": "ready", "sources": "ready"},
  "nodes": 0,
  "edges": 0,
  "by_type": {"<kind>": 0},
  "overall": {"state": "healthy"},
  "growth": [{"at": "YYYY-MM-DD", "nodes": 0, "edges": 0}],
  "additions": {"nodes": 0, "edges": 0, "since": "<instant>"}
}
```

Every number comes from stored rows of the caller's world.

- `nodes` and `by_type` count the active items: an item with no `end_class`. A done task stays active; a refuted, retired, superseded, spent or ended one does not. `edges` counts the active links: a link with an `active_key`.
- `summary` is `Your Grove is empty` when the world has no active item.
- `growth` holds the cumulative counts at the end of each UTC day, oldest first, for at most the last 30 days on which an active item or an active link was recorded. Each row sits on the day of its `recorded` column, the record time, and not on its world time. The last point equals `nodes` and `edges`. A row older than the first point counts in that point. A row that ended later counts on no day, so a point says how many of today's active rows existed then, and not how many were active then. A world with no dated row answers one point: today, with its totals.
- `additions` counts the active items and active links with `since < recorded <= now`, and `since` is 24 hours before `now`.
- `checks.graph` is `ready` when the reads worked. A failed read refuses the whole answer with a 503 `unavailable`, so the answer never holds a guessed number.
- `checks.sources` is `attention` when a connector of the world has `last_outcome` `failed`, and `ready` otherwise. It does not change `status` or `overall`.

The person view of the health checks (D13) belongs to `views-b`.

### `POST /v1/views/positions`

Body, with `Content-Type: application/json`: `{"positions": [{"id": "<item id>", "x": 0.0, "y": 0.0}]}`.

- 1 to 5000 entries. Each entry has exactly `id`, `x` and `y`. `id` is a string of 1 to 128 characters, with no lone surrogate (JSON can write `"\ud800"`, and UTF-8 cannot hold it). `x` and `y` are finite numbers, at most 1e7 either way, so an integer of 400 digits is refused like any other number past the bound. The body is at most 2 MiB, and `NaN` and `Infinity` are refused. Each of these is a 400 `invalid_arguments` that names the field.
- Answer: `{"placed": n, "kept": m, "unknown": k}`, which add up to the number of entries.
- Insert-only. An item that already has a position keeps it, and counts as kept; so does a repeat inside one call. An id that is not an item of this world counts as unknown and writes nothing. A refused body writes nothing at all.
- Two calls that race on one item end with one position: the `(world_id, item_id)` key of `map_positions` backs it.
- The JSON content type is required, so a cross-site form cannot post here without a preflight.

The positions live in the table `map_positions` (`:MapPosition`); `docs/store-map.md` has its row.

## What the production page does with the map

Every place on the map is a stored place, so nothing on the page moves one:

- A click on a legend row only hides or shows that kind. It lays nothing out again: no hub moves, a hidden hub keeps its pin, and the camera stays. A shown kind's items come back at their places, with their links.
- The map's URL settings `?classic=1`, `?dense=0` and `?orbit=0` have no effect on the production page. They stay in fixture mode, the simulation engine that only the checks run. `?hood=1` and `?hood=0` stay on both: they turn the workers view on or off and write nothing to the store.
