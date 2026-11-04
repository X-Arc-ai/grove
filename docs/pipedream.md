# Pipedream account connections

The Sources → Add → Synced apps panel offers Gmail, Google Calendar and Notion through Pipedream's hosted authorization page. The person completes provider consent in a separate tab and then selects **Check connections** in Grove.

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

Both routes require Grove's existing session cookie. The server derives a stable opaque `external_user_id` from the authenticated Grove `user_id`. Browser-supplied user, world and account identifiers never authorize requests.

Before any token or account-status request, the server reads the persisted demo designation for the session's world through `grove.demo_policy.is_demo_world`. A designated world receives `403 scope_denied` on both routes, even when demo runtime configuration is absent. The check runs in the normal threadpool; provider HTTP runs outside database transactions. Existing ordinary accounts cannot become designated accounts through the published provision path.

- `GET /v1/views/pipedream/accounts` reads the authenticated owner's accounts, filtered separately to each of the three providers. It requests `include_credentials=false` and returns only fixed provider names and `connected`, `not_connected`, `attention` or `unavailable` status. The account must be healthy and not dead to count as connected.
- `POST /v1/views/pipedream/{provider}/connect` accepts only an empty JSON object. It creates a ten-minute Connect token with the selected app, the configured Grove origin, and account/app permissions. It returns the hosted HTTPS URL. It does not grant proxy or action permissions.

All responses use `Cache-Control: no-store`. Status responses also include the authenticated Grove account ID so the browser can reject a response from a different session. Neither route returns provider account names, account IDs, content or credentials. Callback query strings are not used as evidence. The UI checks the accounts API again after consent.

The server limits response bytes and request duration. No additional database table or dependency is required. API access tokens are used only for the current operation; they are not stored in the browser or persisted.

## Verification and limits

`server/tests/views/test_pipedream.py` uses a controlled HTTP transport to check owner scope, app scope, production selection, unavailable configuration, refused identity inputs, and upstream failures. It makes no external provider calls.

Real connection acceptance requires the private client credentials, a deployed API with that configuration, and owner consent for each provider. A local controlled check does not establish real provider acceptance.

## Official protocol references

- [Managed Auth quickstart](https://pipedream.com/docs/connect/managed-auth/quickstart)
- [Connect Link](https://pipedream.com/docs/connect/managed-auth/connect-link)
- [Create Connect token](https://pipedream.com/docs/connect/api-reference/create-connect-token)
- [List user accounts](https://pipedream.com/docs/connect/api-reference/list-accounts-for-external-user)
- [API authentication and token scopes](https://pipedream.com/docs/connect/api-reference/authentication)
- [Managed OAuth clients](https://pipedream.com/docs/connect/managed-auth/oauth-clients)
