# Read-only source connections

Google Calendar and configured local Git clones use the screened ingestion pipeline in
`docs/ingest.md`. They are not upload formats. A caller cannot upload arbitrary JSON and label
it as a Calendar or Git artifact. All routes below require the current Grove session.

## Calendar

`POST /v1/connectors/google/start` accepts JSON with `calendar_id` (default `primary`), optional
`log_from` (ISO date), and `logged_days` (ISO dates). It returns `authorization_url` and sets a
short encrypted HttpOnly state cookie. The browser opens that URL. Consent requests
`calendar.readonly` and `openid`: the latter separates Google accounts that both have a primary
calendar. It requests offline access. Sign-in keeps its existing `openid email profile` scope.

Google returns to the configured public origin plus `/v1/connectors/google/callback`. State
binds to the current Grove session and world, expires after ten minutes, and can be used once.
The callback never signs in a different Grove account. Missing Calendar scope, a missing
refresh token, an invalid ID token, or a changed session refuses the connection. The browser
receives only a success/refusal redirect, never a provider credential.

The reader requests only the selected calendar. It expands recurring entries with
`singleEvents=true`, includes cancellations, reads all pages, and then stores `nextSyncToken`.
Later reads reuse that token with the same query configuration. HTTP 410 restarts one full
read. Existing source history remains; event revision keys make the replay idempotent.
The provider cursor moves only after every event commits. A partial write can replay safely.

One entry revision is one episode. Keys include the calendar, event ID and etag. A deletion
without an etag uses a stable tombstone key tied to the last known revision. Repeated deletion
reads and a 410 reset retain that key; a restored event starts a new revision cycle. The source
retains the title, description, location, start/end with timezone, organiser, attendees, status,
recurrence identity, and last change. Code compares field fingerprints and records changed
field names without a model call. The Calendar status `confirmed` describes the schedule;
it does not prove that the event occurred. An event's scheduled time and its update time
remain separate. A cancellation without a start has an unknown event time.

Limits per read: 100 pages, 4 MiB per page, and 10,000 event identities. A read beyond a limit
fails explicitly and keeps the previous cursor. It never reports an incomplete calendar as
an empty successful read.

## Git clones

The operator supplies `GROVE_GIT_REPOSITORIES_FILE`, a regular private JSON file, mode 600,
with this shape (paths are examples):

```json
{"<world-id>": {"demo": "/var/lib/grove/repos/demo"}}
```

`POST /v1/connectors/git` accepts `{"repository":"demo"}`. Only an alias allowed for the current
world is accepted. A session never supplies a filesystem path. Reads inspect a snapshot of
the clone's HEAD and its commit objects. They do not fetch, run hooks, read worktree files,
or call a model. One commit or merge is one episode. The source keeps its authors, times,
parents and message. A changed history replays reachable commits with the same identity
keys. A clone proves that a commit exists; it does not prove a feature is deployed. Full pull
request metadata requires a separate API connector and is not inferred from commit subjects.

## Connection operations and polling

- `GET /v1/connectors` returns connection IDs, provider, state and coverage counts.
- `POST /v1/connectors/<id>/sync` accepts `{}` and reads the connection now.
- `POST /v1/connectors/<id>/settings` updates Calendar `log_from` and `logged_days`.
- `POST /v1/connectors/<id>/disconnect` clears local provider credentials and stops reads.
  It retains source history and does not revoke other calendars through Google's shared grant.

`GROVE_CONNECTOR_POLL=1` starts one serial poll loop in the API. The default is disabled until
the operator provisions the source configuration. Every thirty seconds it selects one due connection. The initial
read interval is five minutes. Reads use a database lease. API shutdown waits for an active
poll before it closes the database driver. Failed and denied reads retain the previous cursor
and successful-read time. Coverage never treats denial as an empty success.

## Credentials and deployment

`GROVE_CONNECTOR_KEY_FILE` names a regular private file containing a base64-encoded 32-byte
key. The operator creates it with mode 600 and mounts it read-only for the API. No key is
generated or printed by a request. An absent or open-permission key refuses Calendar consent.
The existing `GROVE_GOOGLE_OAUTH_FILE` supplies the same OAuth client as sign-in.

Provider tokens are AES-256-GCM ciphertext on the connector row. Associated data binds the
ciphertext to its world and connector ID. Public connection reads omit it. Keep the key outside
the database and retain it for database restoration. Replacing it without re-encryption requires
new consent. A refresh response that omits a replacement refresh token retains the old one.

Deployment needs the key mount and, for Git, the private allowlist and read-only clone mounts.
The connector PR alone does not establish a live deployment or a completed human consent.
Calendar remains `Planned` in the public data-flow table until those steps run.

## Verification

The D5 writer integration remains open until the truth-clock controls merge and consume the
connector's exact event facts. Parser provenance alone does not establish that a logged event
can confirm an occurrence or that a Git artifact cannot confirm deployment. The lane must pass
those writer tests before it declares acceptance.

`GROVE_SLICE_ID=ingest-a3 uv run --project server python -m pytest server/tests/connectors`
uses a real loopback fake Google server and synthetic Git repositories. It covers pagination,
incremental reads, 410 reset, replay, partial commit recovery, denial, refresh, consent state,
world isolation, credential binding and automatic polling. No test reads a person's data.
