# Grove

Grove gives AI assistants a dated, sourced memory of projects, decisions, and plans.
It represents that memory as typed items and links. A single writer validates changes,
preserves evidence and history, and returns receipts.

The repository is **PRIVATE**. This is a pre-release build: the complete user journey has
**not yet passed**. Start with [the recorded status](docs/submission-status.md) for the
source baseline, deployed service revisions, and open acceptance.

The documented source snapshot at 2026-11-04 19:04 UTC is `c145cf8b450f3ae7ab43989cf07969ee5b493057`.
It includes the published UI repair in PR #60, materials in PR #61, and the separate
source helper in PR #63. Published source does not prove deployed acceptance.

Release evidence update: 2026-11-04 at 19:04:34 UTC. Request19 closure and separate
verification PASSED. The recorded API and `deployed.sha` match PR #60; the runner/gateway
revisions remain distinct. Request18 remains FAILED. This was operational closure with no
rebuild, restart, migration, seed, or runtime data write, not a new code deployment.
See [the dated receipt facts](docs/submission-status.md#request19-release-evidence-update) and remaining acceptance limits.

## What is here

- A Python/FastAPI API, Neo4j store, world-scoped reads, and a validating writer.
- A browser graph, sign-in, export guides, screened chat retention, and source controls.
- OAuth and MCP transport, a separate restricted demo, and model/runner components.

General public MCP tools are not available: `DEFAULT_TOOLS` remains empty outside the
restricted demo. Source retention does not prove graph extraction. Files remain `held_provenance` with
no file Request. Notes use ordinary Requests; local synthetic proof does not establish
deployed processing. Complete worker behavior and full privacy/restore acceptance remain incomplete.
See [architecture and capability limits](docs/architecture.md).

## Local setup

Requirements: Git access to this private repository, Python 3.12, `uv`, and Docker Compose.
The web client uses plain JavaScript and a standard-library Python build; no npm install is required.

From the repository root, install the locked server dependencies:

```sh
uv sync --project server --frozen
```

Load `NEO4J_AUTH` securely into your shell as `neo4j/<your-password>`. Do not put the value
in a command, tracked file, or log. The checked-in Compose file pins Neo4j Community and binds
Bolt to `127.0.0.1:57687`. It uses one persistent local development store. If that store already
runs, reuse it. Run migrations only on your development store, with other writers stopped.

```sh
: "${NEO4J_AUTH:?Load the development database credential securely first}"
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
```

Wait for the database to report healthy. Then build the browser pages, apply migrations,
and start the API:

```sh
python3 web/build.py
cd server
uv run python -m grove.writer.migrate
GROVE_WEB_DIST=../web/dist GROVE_PUBLIC_BASE_URL=http://127.0.0.1:8000 \
  uv run uvicorn --factory grove.api.app:create_app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The anonymous root redirects to `/login`.
The build also regenerates `docs/data-flow.md` from its source; review any resulting diff.

Materials require private `GROVE_FORGET_LEDGER_DIR` configuration and migration
`0019_material_revisions.cypher`. Material note processing requires coordinated API/runner
source and canonical external ledger equality. The recorded deployment excludes this component.
Use the [material boundary](docs/architecture.md#browser-material-boundary) before a separate materials release.

This starts the API and UI, not the model gateway or runner. Email sign-in needs secure
`RESEND_API_KEY` and `MAIL_FROM` configuration. Google sign-in needs `GROVE_GOOGLE_OAUTH_FILE`
and a registered callback for the configured origin. Apple sign-in is unavailable.
Missing provider settings do not create a working login. See the [sign-in contract](docs/signin.md),
[runner guide](docs/runner.md), and [deployment guide](docs/deploy.md) for those separate services.

For document checks only, run this from the repository root after dependency installation:

```sh
uv run --project server python -m pytest server/tests/submission -q
```

No database, provider, or model is required for these document checks. This preparation did
not execute the local service setup. See [contribution and test guidance](CONTRIBUTING.md).

## Demo workflow

Use invented content only. The ordinary upload account and separate prepared account serve
different purposes. The release owner supplies authorized access separately. Read the
[privacy page](https://app.grove.x-arc.ai/privacy) before you add content.

### 1. Open Grove and sign in

Open [Grove](https://app.grove.x-arc.ai/) in a fresh browser profile. Use **Continue with email**
with an account you control, or configured Google sign-in. Complete the real login flow.
Do not put account identifiers, codes, tokens, or consent URLs in the repository or video.
The current owner login/upload/Muse rehearsal remains **NOT RUN** and is paused.

### 2. Upload the Lumen example

Use the small invented [Lumen example](server/grove/demo/lumen-upload.json) with **Claude**
and **Choose your export**. Do not edit, reformat, or ZIP this reproducible demo file.
Submit it once and inspect the server receipt. Repeat it to check **No new messages**.
Require the notice **Your graph has not changed from this upload.**

The accepted source also supports ordinary ChatGPT/Claude JSON and ZIP exports through
mandatory screening. That repair is in the recorded PR60 API; the paused owner flow remains unverified. The upload
retains screened sources; it does not create the prepared graph. A reminder does not request
an export from a provider. Log out before the prepared-account exercise.

### 3. Connect Muse to the prepared account

Do not upload Lumen into the prepared account. Do not run a seed or store command:
the synthetic Cedar and project memories already exist. Use recall only for this rehearsal.

In Muse, add `https://app.grove.x-arc.ai/mcp` as a remote MCP connection. Complete Grove login
and consent, then enable the connector. The exact Desktop menu labels remain unverified.
Set this instruction once:

```text
Use Grove as your main memory. Check it when I ask about our projects, decisions, or plans.
```

Ask natural questions without the expected answers:

- Where and when is the Cedar demo?
- What did we decide for Willow Dapple Cove’s kitchen pilot?
- What do we need to check when the seed trays arrive at Moss Bramble Reach?

Require an actual successful Grove tool result and compare its evidence with the answer.
These natural questions and automatic Grove use remain **NOT RUN** through Muse.
Instruction persistence across conversations is unverified. A correct guess after a tool
error is not a successful recall. The upload scene did not create this prepared memory.

### Isolated Muse Code check

Muse Code and secure `META_API_KEY` access are separate prerequisites. Use a new empty
test directory as the child process's HOME, with XDG config/data/state/cache directories
inside it. Keep existing settings, skills, conversations, and credentials out of that directory.
Set `MUSE_NO_AUTO_UPDATE=1`. Create `.config/muse/settings.json` inside the test directory:

```json
{
  "schema_version": 1,
  "mcpServers": {
    "grove": {
      "type": "http",
      "url": "https://app.grove.x-arc.ai/mcp"
    }
  }
}
```

From that isolated environment, run `muse mcp login grove --headless`. Complete consent with
the prepared account and select **Approve**. Treat the returned loopback URL as a credential.
The settings shape was tested with Muse Code 1.4.14; current public acceptance remains NOT RUN.

```sh
muse exec --model muse-spark-1.3 --no-foreign-personal-context --contributor-mode off "Where and when is the Cedar demo?"
```

Approve only `recall`. Inspect the actual tool result in two new conversations.
If `store` executes, stop and record it without a retry. Finish with `muse mcp logout grove`.

## Further reading

[Pipedream](docs/pipedream.md) supports authorization and status only for provider data;
it imports no content. The catalog and confirmed disconnect source are in the recorded PR60 API.
Real provider consent remains unverified.

- [Architecture](docs/architecture.md) and [store map](docs/store-map.md)
- [Submission status](docs/submission-status.md) and [operator checklist](docs/submission-checklist.md)
- [Event history and pre-event attribution](HACKATHON.md)
- [Contributing](CONTRIBUTING.md) and [Apache-2.0 license](LICENSE)
