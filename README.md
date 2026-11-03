# Grove

Grove builds a dated, sourced model of a person's world for AI assistants. Typed items and links
carry evidence and time fields. One writer validates changes and returns receipts. The planned
experience connects an assistant to that world through two MCP tools: `store` and `recall`.

## Current release status

This is a pre-release build. The complete judge path below has **not yet passed** on the public
host. As checked on 2026-11-03, public MCP tools are not available in the merged implementation.
The public host serves `9a5cb23ba2935a66f0091e72ccc7e38c54695722`; the documentation baseline is
`57da012f05eecfd0641773e12e4f3a0912bff5cb`. A healthy server does not prove upload, extraction,
recall, or a completed connection. See [release evidence and open gates](docs/submission-status.md).

The implemented parts include the store and writer, request queue, source screening, model-call
layer, sign-in, OAuth transport, initial graph views, runner infrastructure, and ontology floor.
The full feature scope remains open. [HACKATHON.md](HACKATHON.md) separates merged work, open PRs,
and pre-event material. [Architecture](docs/architecture.md) explains the data paths and their status.

## Judge path

Use invented content for this demonstration. The upload and assistant steps are the acceptance
script for the completed release; do not interpret a simulated screen as a successful import.

### 1. Open Grove and sign in

1. Open [Grove](https://app.grove.x-arc.ai/) in a fresh browser profile.
2. Select **Sign in**, then use Google or the email-code option with an account you control.
3. For email sign-in, enter the six-digit code from your inbox. For Google, finish its consent flow.
4. Confirm that Grove opens your world. Sign out and sign in again to confirm that the world remains.

Google sign-in requests identity scopes. Calendar access is a separate consent step. Apple sign-in
is not available. Read the [privacy page](https://app.grove.x-arc.ai/privacy) before you add content.
If no account credential or email code is available, stop at sign-in; no completed connection exists.

### 2. Add one export

**Pending release acceptance:** the merged snapshot has no upload endpoint, and some source
screens still use simulation state. A selected filename or a progress animation is not a receipt.

After the upload and source views ship:

1. Make a short AI conversation with invented names, such as a plan for Kestrel Studio on 12 November.
2. Export that conversation in a supported format: a Claude Code JSONL session, a Codex rollout,
   a ChatGPT export, or a claude.ai export. Use the matching provider in Grove's chat-source screen.
3. Select the export and submit it. Wait for a saved result from the server.
4. Open the resulting item and inspect its source evidence. Confirm that the invented plan is present.
5. Import the same export again. Confirm that it does not create duplicate items.

Record a failure if the upload has no server receipt or no resulting evidence. The final UI labels
and this procedure require verification after the source-view release.

### 3. Connect Muse Code

Prerequisites: Muse Code is already installed, and its Meta API access is available. The settings
format and login command below were tested with Muse Code 1.4.14 against local OAuth transport.
The public end-to-end exercise is still pending. Grove login and Meta API access are separate.

For an isolated test, use a new empty directory as the child process's `HOME`. Set
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME` to directories inside it.
Set `MUSE_NO_AUTO_UPDATE=1` to keep the installed launcher from updating during the test.
Start all Muse commands from an empty directory in that test HOME. Do not copy existing settings,
skills, conversations, or credentials into the directory. Supply `META_API_KEY` through your secure
environment if Muse requires it; do not put it in the settings file or a command argument.

Create `.config/muse/settings.json` inside the isolated HOME:

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

From that same isolated environment, run:

```sh
muse mcp login grove --headless
```

Open the displayed authorization URL in the fresh browser. Sign in to Grove if asked. Check the
requested permissions, then select **Approve**. Return the final loopback redirect URL to the
waiting Muse command if it asks. Treat that URL as a credential; do not publish it. A successful
OAuth login proves authorization only. It does not prove that either MCP tool exists.

When the release exposes both tools, run:

```sh
muse exec --model muse-spark-1.3 --no-foreign-personal-context --contributor-mode off   "Use Grove recall. What changed this week, and what does it break?"
```

Then run:

```sh
muse exec --model muse-spark-1.3 --no-foreign-personal-context --contributor-mode off   "Use Grove store to remember: Kestrel Studio plans a rehearsal on 12 November."
```

Confirm that the first answer cites Grove evidence or states what it does not know. Confirm that
the second result reaches **saved**, has a receipt, and appears in Grove's views. A queued result
is not saved. If Muse reports that a Grove tool is unavailable, this test fails. End test access with
`muse mcp logout grove` and remove the isolated test HOME when its evidence is safely retained.

### Muse app alternative

The intended connection URL is `https://app.grove.x-arc.ai/mcp`. Add it as a remote MCP connection
in the Muse app, complete Grove sign-in and consent, then use the two prompts above. The app's
current menu labels and this public connection remain unverified. This alternative is not a
completed acceptance result; the Muse Code path above is the primary reproducible procedure.

## Development

The server uses Python 3.12, `uv`, FastAPI, and Neo4j Community. The web client is plain JavaScript.
Read [the store map](docs/store-map.md), [sign-in](docs/signin.md), [hosts](docs/hosts.md),
[models](docs/models.md), [runner](docs/runner.md), and [deployment](docs/deploy.md) for their contracts.
The local OAuth example in `docs/hosts.md` uses synthetic tools; it does not substitute for public acceptance.

```sh
cd server
uv sync --frozen
uv run python -m pytest tests/submission -q
```

These document checks parse the Muse settings, check the required login and model flags, retain
the empty-tool warning, and resolve document links. They do not exercise an account or a live import.
The existing package suites and `Validate` cover product code. [Apache-2.0](LICENSE).
