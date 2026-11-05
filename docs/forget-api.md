# Forget API

The `/forget` page requires a signed-in person. It provides a review step, a cancel action,
and a separate **Confirm and forget** action. Every operation needs a fresh approval.
The server refuses the operation unless `GROVE_FORGET_LEDGER_DIR` names the private,
durable ledger location. Setting that variable alone is not a deployment or durability proof.

## Request sequence

1. `POST /v1/forget/approve` with the person's `grove_session` cookie and JSON
   `{"target": TARGET, "confirm": true}`. A browser must use the configured public origin.
   Any Authorization header is refused here, including a sensitive-write host token.
2. The response contains `approval_token`, `expires_at`, and `one_use: true`.
   The opaque token expires after five minutes and binds to the exact target, user and world.
   Store it only for the immediate request; do not place it in a URL or a log.
3. `POST /v1/forget` with JSON `{"target": TARGET, "approval_token": TOKEN}`.
   Use the person's session, or an OAuth token for this resource with
   `grove.sensitive_write`. A host scope does not replace the person's approval.
4. The receipt contains `receipt_id`, counts per table, `backups_clear_after`, and
   `downloaded_exports: "out_of_reach"`. It does not repeat the selected text.

Both POST routes require `Content-Type: application/json` and cap bodies at 16 KiB.
Responses use `Cache-Control: no-store`. A network failure can leave the outcome unknown;
the browser does not retry automatically.

## Delete my Grove

`POST /v1/world/delete` removes the signed-in person's whole world and leaves them signed in to a
new empty one. It acts on the world of the session and takes no world id. The body is exactly
`{"confirm":"DELETE"}` as `application/json`, at most 1 KiB, and the route accepts no query field.
The answer sets a new `grove_session` cookie, uses `Cache-Control: no-store`, and reads:

```json
{"deleted": true, "backups_clear_after": "YYYY-MM-DD", "signed_in": true}
```

| Status | Code | When | What was removed |
|---|---|---|---|
| 400 | `invalid_arguments` | Any other body, content type or a query field | Nothing |
| 401 | `unauthenticated` | No valid session | Nothing |
| 403 | `scope_denied` | Any Authorization header, or a demo world | Nothing |
| 403 | `forbidden_origin` | A foreign `Origin`, or `Sec-Fetch-Site: cross-site` | Nothing |
| 409 | `busy`, retryable | An upload or a file add of this world is running | Nothing |
| 502 | `unavailable`, retryable | A Pipedream call failed. No ledger record is written yet | Nothing of the world. Some connected apps may be gone |
| 503 | `unavailable`, not retryable | `GROVE_FORGET_LEDGER_DIR` is unset or unsafe, seen before any step | Nothing |
| 503 | `unavailable`, retryable | The ledger, the files or the store failed after the first step | Perhaps a part: send it again |

The steps run in this order, and each one can run again, so a retry finishes the job:

A delete refuses while an upload or a file add of this world runs, and none starts while it runs.
The new world keeps the old id, so such a request would land the old content in the new world. The
guard is in the memory of the one API process (`grove.api.world_activity`). A second API process
needs a store-level guard first.

1. **Pipedream.** Every account of this person goes, for any app, with the calls of disconnect,
   and the list is read again. With no Pipedream settings this step makes no call. This step comes
   before the ledger row, because a restore acts on that row: an attempt that stops here removed
   nothing of the world, and the person goes on using it.
2. **Ledger.** An fsynced `started` row goes to the forget ledger, with ids and times only.
3. **Files.** `GROVE_AGENT_FILES_DIR/<world id>` goes. A link inside it is unlinked and its target
   stays. With the variable unset this step is skipped. A root that is set and absent is a 503.
4. **Store.** The world's lock is held from the first batch to the commit, so no pass writes
   between two batches. Every node with this `world_id` goes, in batches, also the platform rows
   `OAuthGrant`, `OAuthToken`, `AuthCode` and `UploadToken`. A `finished` row goes to the ledger.
   One commit then removes the `:World` row and every `:Session` of the user, so other devices
   sign out. Until that commit the person's cookie still works for a retry.
5. **New world.** `ensure_account` makes the empty world and its own item, and a new session
   starts. The `:User` row, its `EmailCode` rows and the login stay. `:OAuthClient` stays too: it
   belongs to the host, not to a person (S45). The own item reads the neutral title.

After step 4 a failed step 5 answers 503 and the old cookie reads 401. The next sign-in makes the
same empty world, so a page that gets a 401 sends the person to sign in. The ledger lock is held
for all of step 4, as material work takes it before any World lock, so a forget waits for a delete.

The rows sit in the world's own `<sha256 of the world id>.json`, as `target_type: "world"` with
`state`, `user_id`, `erased_at` and `backups_clear_after`, because the deploy preflight refuses
any other entry in that folder. The file stays append-only. A `finished` row ends every forget row
before it: the world they name is gone and the new world has the same id, so `read_ledger` and the
material checks read only the rows after it. A `started` row ends nothing, because that attempt
can fail with the world still in place.

## Exact target shapes

| Target | JSON shape |
|---|---|
| An item | `{"type":"item","item_id":"OPAQUE_UUID"}` |
| One content field | `{"type":"field_value","item_id":"OPAQUE_UUID","field":"summary"}` |
| A standalone history value | `{"type":"field_value","value_id":"OPAQUE_UUID"}` |
| A named value | `{"type":"named_value","value":"exact text"}` |
| A source span | `{"type":"source_span","source_id":"OPAQUE_UUID","locator":"exact locator"}` |
| A whole source | `{"type":"source_span","source_id":"OPAQUE_UUID"}` |

Extra keys are refused. Field names are `title`, `summary`, `body`, `properties`,
`search_terms`, and `status`. Named values are nonempty strings up to 512 characters.
Locators are nonempty strings up to 4096 characters. Control characters and surrogate
code points are refused. Identifiers use the canonical UUID representation.
The standalone history selector retains the writer's refusal for a whole create-row value.

Approval storage contains hashes and metadata, never raw approval tokens or target text.
The writer consumes approval within its cascade transaction. A transaction rollback leaves
the approval available until its original expiry. Concurrent requests cannot consume it twice.

## Privacy statement

`server/grove/privacy.py` defines the data-flow statement. `web/build.py` generates
`docs/data-flow.md` and embeds every table row in the public privacy page.
The page remains readable without JavaScript or an available API.
`GET /v1/privacy` supplies only a fixed vocabulary and configuration-presence status.
It never returns environment values, secret paths or custom endpoint URLs.
“Settings present” does not prove a successful connection or completed operation.
Calendar and media integration remain pending until their owning lanes merge.


## Restore limits

Approved plans retain opaque normalized length/digest predicates so an older snapshot cannot
restore a different sentence containing the selected value. They contain no selected plaintext.
If restore discovers an affected source that the original plan did not know, it preserves the
unrelated stored messages and blocks future import of that whole source. An affected historical
Source image without a matching live Source locator refuses safely before erasure or replay.
The latest external ledger is never rewritten to remove or change an earlier approved intent.

`default_world_id` is deterministic, so a new world has the id of the deleted one, and only the
ledger tells them apart. `take_snapshot` records `taken_at`. `restore` and `restore_loaded` call
`replay_world_erasures` for their world before anything else, and refuse with no ledger folder:
pass `ledger_dir`, or set `GROVE_FORGET_LEDGER_DIR`. If the newest erasure row of a world is later
than the snapshot, the restored rows of that world, its `:World` row and the user's sessions go,
nothing is made, and the answer is `{"erased": true, "forgotten": []}`. The person's next sign-in
makes the new world. A snapshot newer than the row restores as usual. A snapshot with no
`taken_at` counts as older than every row.

A `started` row with no `finished` row counts as well, as a D14 intent does: the person asked for
the erasure, so a restore from before the attempt does not bring the world back, even if the
attempt failed and the person went on using the world. The `finished` row covers a dump taken
during the delete: it is newer than that dump, so what the dump caught goes.

A dump holds every world, so a physical restore also runs the store-wide step in
[deploy.md](deploy.md#restore-a-dump). A logical restore names one world, because the rest of the
store is live and a live world with a newer erasure holds the person's new data.
