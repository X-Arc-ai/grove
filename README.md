# Grove

Grove builds a dated, sourced model of a person's world for AI assistants. Typed items and links
carry evidence and time fields. One writer validates changes and returns receipts.

## Current release status

This is a pre-release build. The complete judge path has **not yet passed** on the public host.
At the 2026-11-04 13:51 UTC check, public `/healthz` reported service/store OK and build
`cac4b95cbc7ce5284c04fcb684ebb2f115162144`. The source baseline for these documents is `6346a9fc7c6c9b8119a9e7310cb93944f2fb1afe`.
Later source publication does not update that deployment.

The deployed demonstration supports an exact invented Lumen upload for an ordinary account and
restricted Cedar `store`/`recall` tools for a separate prepared account. General public MCP tools are not available:
`DEFAULT_TOOLS` remains empty outside the restricted demo. General private-export processing is
not enabled on this host. Owner login/upload/Muse acceptance and actual provider consent are **NOT RUN**.
See [release evidence and open acceptance](docs/submission-status.md).

The source includes storage, the writer, corrections and restore, truth-clock checks, chat uploads,
Calendar/Git connectors, OAuth, the runner, bounded consequences, and further inactive components.
That list does not establish complete feature or public acceptance. [Architecture](docs/architecture.md)
records the boundaries. [HACKATHON.md](HACKATHON.md) records event PRs and pre-event attribution.

## Judge path for the current demonstration

Use invented content only. The two accounts below serve different purposes. An upload result
proves source retention; it does not prove graph extraction or create the prepared Cedar data.
The release owner must supply authorized access to the prepared account separately. No account
identifier, login code, or token belongs in this repository.

### 1. Open Grove and sign in

1. Open [Grove](https://app.grove.x-arc.ai/) in a fresh browser profile.
2. Use a new ordinary account you control. Select **Continue with email** and enter the code from
   its inbox. Google sign-in is also implemented; Calendar consent is separate from identity login.
3. If the welcome screen appears, select **Get Started**.
4. Confirm the five source choices: Muse, WhatsApp, ChatGPT, Claude, and Grok.

Read the [privacy page](https://app.grove.x-arc.ai/privacy) before you add content. Apple sign-in
is unavailable. If no authorized account credential or inbox access exists, stop at sign-in and
record the step as blocked. A private window does not make an existing server account empty.

### 2. Upload the Lumen example

Use the exact [Lumen example file](server/grove/demo/lumen-upload.json), which contains 361 bytes
of invented content. Save its original bytes. Do not edit, reformat, or ZIP the file.

1. Select **Claude**, then inspect the export guide and its **Pause** control.
2. Select **I’ve requested it**, then **Choose your export** and the Lumen file.
3. Select **Upload export** once and wait for the real server result.
4. On the first upload, require **Import accepted**, one source, one episode, one message read,
   one message kept, and zero failed chats.
5. Require the notice **Your graph has not changed from this upload.**
6. Upload the same file again. Require **No new messages**, zero new sources/episodes, and zero
   failed chats. This checks the exact repeat only, not arbitrary overlapping exports.

A guide reminder does not request an export from a provider. A selected filename, animation,
or retained source is not proof of graph extraction. Other upload bytes retain normal screening
requirements; the current host does not enable general private exports.

Finish setup with **Skip for now** on unavailable source steps. The app catalog, file sections,
and Muse directions remain visible. Unsupported operations must not claim success.
Log out of the ordinary account and refresh before the prepared-account exercise.

### 3. Connect Muse Code to the prepared account

Use the separate prepared account supplied by the release owner. Do not upload Lumen into the prepared account.
Do not run a seed or store command: its Cedar fact already exists. The current positive exercise
uses recall only. A queued result would not prove a saved write in any future write test.

Prerequisites: Muse Code is already installed, and its Meta API access is available. The settings
format and login command were tested with Muse Code 1.4.14 against local OAuth transport.
The current public Muse Code exercise remains **NOT RUN**. Grove login and Meta API access are separate.

For an isolated test, use a new empty directory as the child process's `HOME`. Set
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME` to directories inside it.
Set `MUSE_NO_AUTO_UPDATE=1`. Start Muse commands from an empty directory in that test HOME.
Do not copy existing settings, skills, conversations, or credentials. Supply `META_API_KEY`
through your secure environment if required; never put it in settings or command arguments.

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

From the same isolated environment, run:

```sh
muse mcp login grove --headless
```

Open the authorization URL. Sign in with the prepared account, review permissions, and select
**Approve**. Return the final loopback URL to Muse if requested. Treat that URL as a credential.
A successful OAuth login proves authorization only; require an actual tool result next.

Run a new conversation:

```sh
muse exec --model muse-spark-1.3 --no-foreign-personal-context --contributor-mode off "Use Grove. What room and time are set for the Cedar demo?"
```

Approve only `recall` if Muse requests tool permission. Require an actual successful Grove call,
then compare the answer with its returned evidence. Repeat the query in a separate new conversation.
Do not supply the expected answer or a previous transcript in either prompt. A guessed answer after
a tool error is a failure. If `store` executes, stop and report it without a seed or retry.
End the test with `muse mcp logout grove`. Remove the isolated HOME after safe evidence retention.

### Muse app alternative

In Muse Desktop, add `https://app.grove.x-arc.ai/mcp` as a remote MCP connection. Complete normal
Grove login and consent with the prepared account. Enable the connector, then use the same recall
prompt in two new conversations. Inspect the actual tool results. Exact menu labels remain
unverified. The owner's required Desktop rehearsal is separate from the isolated Code exercise;
both remain **NOT RUN** at this snapshot.

### Provider connections and unavailable features

[Pipedream](docs/pipedream.md) supports authorization and status only. It imports no emails,
Calendar events, or Notion pages. Actual provider consent and connected status remain unverified.
The prepared Cedar account refuses Pipedream and imports. Ordinary accounts can use the consent
flow where available; a provider logo or new tab is not a successful connection.

Worker activity, growth metrics, and Questions remain unavailable. Guidance, reminders, profile
edits, and settings download remain browser-local. Full history, privacy, general imports, and
other incomplete features remain acceptance work; source publication does not complete them.

## Development

The server uses Python 3.12, `uv`, FastAPI, and Neo4j Community. The web client is plain JavaScript.
Read [the store map](docs/store-map.md), [sign-in](docs/signin.md), [hosts](docs/hosts.md),
[models](docs/models.md), [runner](docs/runner.md), and [deployment](docs/deploy.md).

```sh
cd server
uv sync --frozen
uv run python -m pytest tests/submission tests/store/test_store_map.py::test_public_docs_point_to_no_private_plan -q
```

These focused checks validate document contracts, provenance, and current demo boundaries.
They do not exercise an account, import, provider, or model. CI and independent review were waived
as publication gates for this refresh; no old test result certifies the new candidate. [Apache-2.0](LICENSE).
