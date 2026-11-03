# The runner

The runner starts Grove's four OpenCode agents. Code decides when a pass runs (decision 19); an
agent only reads and proposes, through the `brain` CLI, and the writer decides (3.5 rule 1).

```
 runner service (user grove-runner, no store or model credential)
   | runner token: GET /v1/internal/runner/due, POST .../passes, POST .../passes/{run}/end
   v
 api ---- claims requests, writes episodes, run records and jobs, mints the pass's two tokens
   ^
   | pass token: /v1/internal/brain/*            gateway run token: /v1/chat/completions
   |                                                        |
 brain CLI <-- grove-brain-shell (SHELL) <-- OpenCode run --agent grove --> gateway --> Meta
```

## Credentials

| Credential | Holder | Where | What it can do |
|---|---|---|---|
| Runner token | the runner service | `/etc/grove/runner.env` (`GROVE_RUNNER_TOKEN`); the API holds only its sha256 (`GROVE_RUNNER_TOKEN_SHA256`, `/etc/grove/runner-auth.env`) | list due work, start and end passes. It reads no world data. |
| Pass token `p1.` | one OpenCode run | its environment (`GROVE_PASS_TOKEN`) | the `brain` routes of one world, as one agent, with `grove.read` and `grove.propose`. It expires with the pass's time cap, and the API refuses it once the run record has ended. |
| Gateway run token `g1.` | one OpenCode run | its provider key (`GROVE_RUN_TOKEN`, read by `runner/opencode.json`) | model calls of one world, one run and one job, up to the pass's token cap. |

The API mints both pass tokens with `GROVE_GATEWAY_SECRET`; the pass token's key is derived from
it under its own label, so neither token passes as the other. OpenCode's environment is built
from nothing (`grove.runner.loop.opencode_env`): it never holds the runner token.

## A pass

1. `due_work` lists each world with work and no live run. Due requests come first (the Learner,
   the interactive lane), then due jobs (the Curator, then the Connector). A world whose budget
   for today is reached is not listed: every pass job waits at the ceiling, so its requests stay
   queued and spend no attempt until the reset. A pass that meets the ceiling mid-way ends
   `budget`, and its requests wait in the same way.
2. `start_pass` opens a `running` run record under the world lock: one pass at a time per world.
   A run that still reads `running` past its time cap and the margin died with no end; it is
   marked `abandoned` first. The Learner claims up to 5 requests with the run id as the worker
   name (unique per pass) and a lease of the time cap plus the margin, and the episode of each
   claimed source is written before any model call (D10). The Curator and the Connector claim
   their job.
3. The runner runs `opencode run --agent grove --format json "<pass message>"` in its own
   process group. `grove` runs `brain status`, hands the pass to the named subagent with the task
   tool, and returns its summary. A watchdog kills the group at the time cap.
4. `end_pass` hands back each claimed request the pass did not settle: `fail_request` with a
   retry (code `not_proposed`, or `pass_<outcome>`), so the next pass applies it once. The writer
   keys a commit on the request id, so a request that committed before a kill is saved and is
   never applied again. The run record gets its tokens from `model_calls` and its outcome.
5. After a Learner pass that changed items, a Curator job is queued on those items. The
   Connector's job is queued on Appendix C's trigger: 10 changed items since its last pass, or a
   contradiction detected since then.

A runner that is killed with no time to end its passes leaves its requests `applying` until their
leases pass; then they are due again. A stop signal (the nightly backup stops the service) ends
each running pass as `stopped`.

| Setting | Default | Where |
|---|---|---|
| Time cap (watchdog) | 600 s | `PassSettings.time_cap_seconds` |
| Lease margin | 120 s | `PassSettings.lease_margin_seconds` |
| Token cap per pass | 300,000 | `PassSettings.token_cap` |
| Requests per Learner pass | 5 | `PassSettings.learner_requests` |
| Poll | 5 s | `GROVE_RUNNER_POLL_SECONDS` |
| Passes at once (all worlds) | 2 | `GROVE_RUNNER_MAX_PASSES` |

## The brain CLI

`brain` calls `GROVE_API_URL` with `GROVE_PASS_TOKEN`. Every answer is one JSON object; stored
text is only in its `data` field, in labelled blocks (D14). A refusal is a JSON object too, with
exit status 1.

| Verb | Route | What it answers |
|---|---|---|
| `brain status` | `GET /v1/internal/brain/status` | the pass, its agent and inputs, and the world's counts |
| `brain pass` | `GET /v1/internal/brain/pass` | the input packet: the requests with their source, episode, locators and messages; the job; the kinds and meanings with the kinds each end takes; the statement classes; a filled batch form and a field guide |
| `brain recall TEXT` | `GET /v1/internal/brain/recall` | a lexical lookup over titles, summaries and search terms, no model call. `hosts-b` replaces it with D7's recall |
| `brain get ITEM` | `GET /v1/internal/brain/items/{id}` | one item, its active links and its active evidence rows |
| `brain propose [--request ID]` | `POST /v1/internal/brain/propose` | the batch from stdin (a heredoc) or `--file`; saved with a receipt, or declined with the writer's reasons. A Learner proposes under one of its requests; one repair of a decline is allowed |
| `brain finish ID` | `POST /v1/internal/brain/finish` | a request of the pass with nothing to save: saved with no commit |

`brain history` and `brain changed` (section 4) are not built yet; `brain fetch` is the
`cleanup` lane's.

## Permissions (R10)

Two guards, and either one alone stops the R10 forms it covers:

- OpenCode's own: `runner/opencode.json` and each agent file deny every tool (`"*": "deny"`) and
  allow bash only for `brain *`; `grove` may also hand a pass to `learner`, `curator` or
  `connector` with the task tool. On 1.18.32 this refuses chained, `&&`, piped, substituted,
  backtick and two-line commands, and a call to write, edit, webfetch or read (those tools are not
  offered). It lets `brain get x > file` and `brain propose < file` through.
- `grove-brain-shell`, which the runner sets as SHELL (OpenCode runs each call as
  `$SHELL -c "<command>"`). No shell runs: it splits the words and starts `brain` by its full
  path, refusing every shell character outside single quotes, every second line, and
  `brain propose --file`. The one other form is a `brain propose` heredoc.

`server/tests/runner/test_opencode.py` runs each form through the pinned binary against a scripted
local model; `test_shell.py` runs each form through the shell.

## Files

| Path | What it is |
|---|---|
| `runner/opencode.json` | OpenCode's only config: the gateway as the only provider, the permissions |
| `runner/agents/*.md` | `grove`, `learner`, `curator`, `connector`. Each pins its model and carries the data rules. The Curator's and the Connector's prompts are first versions: the `cleanup` and `beliefs` lanes write theirs, and `ingest-b` writes the Learner's real one |
| `server/grove/runner/` | the loop (`loop.py`), the API side (`passes.py`, `brain.py`), the tokens, the shell |
| `server/grove/api/internal.py` | the `/v1/internal` routes |
| `server/grove/writer/runs.py` | run records and jobs |
| `server/grove/cli/` | the `brain` CLI |

## Open points

| Point | Owner lane |
|---|---|
| The runner network still reaches the host through its bridge (`grove-runner`). A host firewall rule on that interface goes with the egress allowlist. | `deploy-b` |
| OpenCode tries to install `@opencode-ai/plugin` from npm into its config folder at first run. On the box the runner network has no internet, so the install fails; no plugin is configured. | `deploy-b` |
| The Learner's speaker for a block: `person` for a host's verbatim words and for the views, `ai_host` for a host's account, `third_party` for every connector source. A chat's own sender needs the connector's mapping. | `ingest-b` |
| `brain recall` is a full-text lookup, not D7's recall. | `hosts-b` |
