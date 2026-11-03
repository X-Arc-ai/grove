"""The OAuth 2.1 authorization server of /mcp: clients, consent, codes and tokens. docs/hosts.md.

The rows are in `oauth_clients`, `oauth_grants`, `oauth_tokens` and `authorization_codes` (S41).
`grove.writer.oauth` is their only writer, `grove.oauth.store` reads them, and every secret is
stored as the sha256 hex of `grove.oauth.secrets.token_hash`.
"""

from datetime import timedelta

# The lifetimes.
CODE_LIFETIME = timedelta(minutes=5)
ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)

# POST /register: at most this many across the server in a sliding window, in process, on
# clock.monotonic(). A metadata document spends none of it. nginx allows one address about 260 an
# hour on /register (4 a minute and a burst of 20), so one address stays under the cap, and four or
# more addresses can still fill it.
MAX_REGISTRATIONS_PER_WINDOW = 1000
REGISTRATION_WINDOW_SECONDS = 3600.0

# A world holds at most this many grant rows, counted with revoked ones, because a grant keeps its
# client row in the store. Soft: approvals at one time can pass it by a few.
MAX_GRANTS_PER_WORLD = 100

# A Client ID Metadata Document is fetched again when its copy is older than the cache time. When
# that fetch fails, a copy younger than the stale limit still serves.
METADATA_DOCUMENT_CACHE = timedelta(hours=1)
METADATA_DOCUMENT_STALE_LIMIT = timedelta(hours=24)

# What the writer cleans up as it goes.
UNUSED_DYNAMIC_CLIENT_KEPT = timedelta(days=7)
MAX_CLIENTS_DELETED_PER_REGISTRATION = 100
EXPIRED_CODE_KEPT = timedelta(days=1)
