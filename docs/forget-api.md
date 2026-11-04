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
