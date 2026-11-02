# Sign-in

A person signs in with Google or with a code sent by email. Apple shows "Not available yet" on the card and has no route. Either method leads to one account: the account is keyed on a hash of the address, so one address has one user, one world and one own item, whichever method it uses.

The code: `server/grove/api/auth.py` holds the routes, `server/grove/api/google.py` the Google steps, `server/grove/api/mail.py` the mailer and the address rule. `server/grove/writer/accounts.py` holds every write (S20), and ruling S40 of `docs/store-map.md` states the reasons. `docs/views.md` has the session contract that the views read.

## The flows

### An email code

1. `POST /v1/auth/email/start {email}` checks the address, takes a slot of the server's window, and stores a hash of a new 6-digit code. The new row ends every earlier unused code of the address, so an address has one live code. The mailer sends the code.
2. `POST /v1/auth/email/verify {email, code}` tries the newest live code. On a match it runs the sign-in below.

### Google

1. `GET /v1/auth/google/start?next=<path>` makes `state`, `nonce` and a PKCE verifier, keeps them and `next` in the state cookie, and redirects to Google with the scopes `openid email profile`. There is no `access_type=offline` and no calendar scope: `ingest-a` asks for the calendar later, in its own consent, at `/v1/connectors/google/callback`.
2. `GET /v1/auth/google/callback` checks the state, exchanges the code at Google's token endpoint with the verifier, and checks the id token's claims: `iss`, `aud`, `azp` when present, `exp`, `nonce`, `email_verified`, `email` and `sub`. On success it runs the sign-in below and redirects to `next`. The signature is not checked, because the token comes straight from Google over TLS (OIDC Core 3.1.3.7 step 6). The token is the usual three parts split by dots, and the middle part is read.

### The sign-in

`ensure_account` finds or makes the user, the user's oldest world (with its starter set) and the own item. A new session follows, then the cookie. A new sign-in also revokes the session that the incoming cookie names. The `:World` row exists before the cookie is set.

The own item is one `person` item with the title from Google's `name` claim, with control characters removed and cut to 120 characters, or `You` with no name and for an email code. It cites a source that holds a pointer (`signin:<method>`) and no text. Logout keeps the world, and a second sign-in by either method resumes it.

## The routes

Every answer is `Cache-Control: no-store`. Every POST needs `Content-Type: application/json`, so a cross-site form cannot post here without a preflight. A body is at most 4 KiB. Errors are typed (`docs/views.md`) and never repeat a value.

| Route | Answer |
|---|---|
| `POST /v1/auth/email/start` | 200 `{"sent": true}`, the same for a new and a known address. 400 `invalid_arguments` naming `email`: more than 254 characters, not exactly one `@`, whitespace or a control character, no dot in the domain. 429 `rate_limited` with `details.retry_after_seconds`. 503 `unavailable`, retryable: the mail failed, there is no mail config, or the store is down. |
| `POST /v1/auth/email/verify` | 200 with the session answer of `docs/views.md`, and the session cookie. 400 for a code that is not exactly 6 digits, which uses no try. 401 `unauthenticated`, "the code does not match", the same for a wrong, expired, used or absent code. 503 with no cookie for a store failure. |
| `GET /v1/auth/google/start?next=<path>` | 303 to Google, with the state cookie. 303 to `/signin?signin_error=google` for no client file or a bad one. A `next` that is not a same-origin path becomes `/`: it must start with one `/`, hold no backslash and no control character, be ASCII, and be at most 2048 characters (an `/authorize` URL of `docs/hosts.md` comes back whole). |
| `GET /v1/auth/google/callback` | 303 to `next`, with the session cookie and `Referrer-Policy: no-referrer`. Any failure is 303 to `/signin?signin_error=google` with no session. Both clear the state cookie. |
| `POST /v1/auth/logout` | 200 `{"signed_in": false}`, and it clears the cookie. It revokes the session that the cookie names, when there is one, and changes nothing else. With the store down it answers 503 and still clears the cookie. |
| `GET /` | With `GROVE_WEB_DIST` set: `home.html` with no `grove_session` cookie, `index.html` with one (no store read: the page asks the session, and shows the card on a 401). `Cache-Control: no-store`, `Vary: Cookie`. |
| `GET /signin` | `index.html`, `no-store`. After a sign-in by email code or Google, the page goes back to a `next` query value only when it starts with `/authorize?` (the consent page, `docs/hosts.md`); any other value goes to `/`. |
| `GET /privacy` | `privacy.html`. |

A missing page file is the typed 404.

## The cookies

| Cookie | Holds | Flags |
|---|---|---|
| `grove_session` | A random token. The store keeps its sha256 only. | HttpOnly, SameSite=Lax, Path=/, Max-Age 30 days, Secure when `GROVE_PUBLIC_BASE_URL` is https. |
| `__Host-grove_google` on an https base, `grove_google` on a loopback http base | `state`, `nonce`, the verifier and `next`, as base64url JSON. | HttpOnly, SameSite=Lax, Path=/, Max-Age 600, Secure on https. |

The scheme and the host never come from a request, because uvicorn runs without proxy headers. They come from `GROVE_PUBLIC_BASE_URL`. The Google callback is `<GROVE_PUBLIC_BASE_URL>/v1/auth/google/callback`, and only the public host's callback is registered at Google.

## The starting values

Constants at the top of `server/grove/api/auth.py`.

| Value | Start |
|---|---|
| A session | 30 days |
| An email code | 6 digits, 10 minutes, 5 tries |
| Codes per address | At most 5 in 15 minutes |
| Codes across the server | At most 300 in an hour |
| The Google state cookie | 10 minutes |

The limit across the server is an in-process sliding window on the clock provider's monotonic time. One API process runs, a restart resets it, and it reads no store. A start that stores no code gives its slot back, so one busy address does not stop the others. nginx also limits `/v1/auth/email/` per client address.

## The configuration

| Name | What |
|---|---|
| `GROVE_PUBLIC_BASE_URL` | The origin. See `docs/deploy.md`. |
| `GROVE_GOOGLE_OAUTH_FILE` | Google's client file: the `web` key with `client_id`, `client_secret`, `auth_uri` and `token_uri`. It is read at first use. A failed read is not kept, so a file that lands later works with no restart. |
| `RESEND_API_KEY`, `MAIL_FROM` | The mail provider. Read at the first send. |

## Run it locally

Use a synthetic client file and a fake mailer, so no default run reads a real secret or sends a mail. The fake Google below is a small local server of your own, reached through the client file; `server/tests/e2e/test_signin_browser.py` has a working one.

```json
{"web": {"client_id": "local", "client_secret": "local", "auth_uri": "http://127.0.0.1:9000/auth", "token_uri": "http://127.0.0.1:9000/token"}}
```

Save this as `local_app.py` in `server/`, and keep it out of the commit:

```python
from uuid import NAMESPACE_URL, uuid5

from grove.api.app import create_app


class PrintMailer:
    def send_code(self, to: str, code: str) -> None:
        print("the code:", code)


def app():
    return create_app(
        mailer=PrintMailer(),
        world_id_for=lambda user_id: f"t_local_{uuid5(NAMESPACE_URL, user_id)}",
    )
```

From `server/`:

```sh
set -a; . ~/.grove/neo4j-dev.env; set +a
python3 ../web/build.py
GROVE_WEB_DIST=../web/dist GROVE_GOOGLE_OAUTH_FILE=<the client file> \
  uv run uvicorn --factory local_app:app --host 127.0.0.1 --port 8000
```

With no `GROVE_PUBLIC_BASE_URL`, a local run uses `http://127.0.0.1:8000`, so open that exact host. The `world_id_for` above gives the world a `t_` id, so `purge_test_world` can remove it from the shared dev store after the look, and `purge_test_account` removes the user, the sessions and the codes. Without it, a run makes a world that no test helper removes.

## The tests

`server/tests/auth/` runs on the real store with a capture mailer and a fake Google behind `httpx.MockTransport`, with no network. Run it from `server/` with `GROVE_SLICE_ID=<slice> uv run pytest tests/auth tests/api -q`. `purge_test_account` removes the user, the sessions and the codes of an address, and `purge_test_world` removes the world.
