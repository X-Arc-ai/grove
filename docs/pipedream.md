# Pipedream account connections

The connection API offers Pipedream's searchable app catalog and hosted authorization pages. Gmail, Google Calendar and Notion remain the default status rows. Other apps, including Slack, Google Drive, Microsoft apps, Linear and Dropbox, are selected by the live catalog slug rather than an invented display-name mapping. The person completes provider consent in a separate tab and then selects **Check connections** in Grove.

This component verifies account connection status only. It does not read email, calendar events or Notion pages, import sources, start sync jobs, run provider actions, or add anything to Grove memory. Connection status does not change the graph or the source import counts. Pipedream's managed OAuth clients supply provider consent; the person must review the actual provider scopes on that page.

## Server configuration

Supply these environment variables to the API service through its private configuration:

| Variable | Meaning |
|---|---|
| `PIPEDREAM_PROJECT_ID` | Existing Connect project ID, starting with `proj_`. |
| `PIPEDREAM_CLIENT_ID` | Pipedream API OAuth client ID. |
| `PIPEDREAM_CLIENT_SECRET` | Pipedream API OAuth client secret, server-side only. |
| `PIPEDREAM_ENVIRONMENT` | `production` for this component. |

Do not put these values in browser code, build arguments, committed files or logs. The module reads the process environment; it does not load an arbitrary file. A deployment owner must supply the private environment to the API process and restart that process. No change to the existing Grove Google sign-in configuration is required.

If a variable is missing or invalid, status is unavailable and Connect refuses. There is no simulated success. An upstream authentication failure, timeout or malformed response produces a typed retryable error without provider response text.

## Owner and API contract

All routes require Grove's existing session cookie. An Authorization header is refused, including when a cookie is also present. The server derives a stable opaque `external_user_id` from the authenticated Grove `user_id`. Browser-supplied user, world and account identifiers never authorize requests.

Before any token or account-status request, the server reads the persisted demo designation for the session's world through `grove.demo_policy.is_demo_world`. A designated world receives `403 scope_denied` on every route, even when demo runtime configuration is absent. The check runs in the normal threadpool; provider HTTP runs outside database transactions. Existing ordinary accounts cannot become designated accounts through the published provision path.

- `GET /v1/views/pipedream/accounts` keeps the three default provider rows. Optional `?provider=<catalog slug>` reads another app after verifying its metadata. Unknown or repeated query fields are refused. The server calls the documented `/v1/connect/{project_id}/accounts` endpoint with its own `external_user_id` filter and `include_credentials=false`. It decodes the `data` and `page_info` envelope, never a bare array. Pagination remains owner-scoped and is capped at three pages of 100 records. Incomplete, inconsistent or repeated pages refuse instead of falsely reporting `not_connected`.
- `GET /v1/views/pipedream/apps?q=<search>&cursor=<opaque cursor>` reads the official catalog. The search is at most 128 characters; the cursor is at most 1024. The server requests 30 records. The response contains `account`, `configured`, `apps`, `next_cursor`, and `total`. Each app contains only `provider`, `name`, `auth_type`, and `connectable`. OAuth and key-based apps support connection; apps with no account do not. Metadata is not HTML, connection status or an import receipt.
- `POST /v1/views/pipedream/{provider}/connect` accepts only `{}`. It verifies the selected bounded slug against the official app metadata and creates a ten-minute app-bound Connect token. It returns the hosted HTTPS URL and expiration. It does not grant proxy or action permissions. Owner consent remains on the actual provider page.
- `POST /v1/views/pipedream/{provider}/disconnect` accepts only `{"confirm":true}`. It discovers the current owner's accounts for that app and deletes only those returned IDs. The browser cannot submit an account or project ID. At most 20 accounts are removed in one request. The server checks the owned account list again before returning `status:"not_connected"` and a `disconnected` count. A partial upstream failure is an error, not a rollback or proof of disconnection. Refresh status after an error.

Both POST routes require the exact configured Origin, a JSON body, and non-cross-site Fetch Metadata. They accept no query options. All responses use `Cache-Control: no-store` and include the authenticated Grove account ID for browser stale-response checks. No route returns provider account names, account IDs, content or credentials. Callback query strings and popup closure are not evidence of a successful connection; refresh the account status.

The server limits response bytes and total request duration. No additional database table or dependency is required. API access tokens exist only for the current operation and use route-specific read, token-creation or confirmed-disconnect scopes. Tokens never enter the browser or persistent storage except the provider's short-lived hosted connection URL.

OAuth scope requests are explicit: default status uses `connect:accounts:read`; catalog uses `connect:apps:*`; selected-app status adds registry access to account read; Connect uses `connect:tokens:create connect:apps:*`; confirmed disconnect uses account read/write plus registry access. The hosted Connect token grants account read/write and registry access only. No token requests `*`, `connect:*`, proxy, action or trigger scopes. The registry scope is exactly `connect:apps:*`; Pipedream does not define a `connect:apps:read` variant.

## Verification and limits

`server/tests/views/test_pipedream.py` uses a controlled HTTP transport to check the actual documented route/envelope, owner scope, app metadata, pagination, production selection, unavailable configuration, refused identity inputs, confirmed disconnection, and upstream failures. It makes no external provider calls.

Real connection acceptance requires the private client credentials, a deployed API with that configuration, and owner consent for each provider. A local controlled check does not establish real provider acceptance.

## Official protocol references

- [Managed Auth quickstart](https://pipedream.com/docs/connect/managed-auth/quickstart)
- [Connect Link](https://pipedream.com/docs/connect/managed-auth/connect-link)
- [Create Connect token](https://pipedream.com/docs/connect/api-reference/create-connect-token)
- [List accounts](https://pipedream.com/docs/connect/api-reference/list-accounts)
- [List apps](https://pipedream.com/docs/connect/api-reference/list-apps)
- [Retrieve app](https://pipedream.com/docs/connect/api-reference/retrieve-app)
- [Delete account](https://pipedream.com/docs/connect/api-reference/delete-account)
- [API authentication and token scopes](https://pipedream.com/docs/connect/api-reference/authentication)
- [Managed OAuth clients](https://pipedream.com/docs/connect/managed-auth/oauth-clients)
