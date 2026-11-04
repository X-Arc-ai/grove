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
| Runner token | the runner service | `/etc/grove/runner.env` (`GROVE_RUNNER_TOKEN`); the API holds only its sha256 (`GROVE_RUNNER_TOKEN_SHA256`, `/etc/grove/runner-auth.env`) | list due work, start and end passes. It reads no world data itself, but a pass it starts gets a pass token for that world, so a copy of `runner.env` can read and propose in any world with due work: keep it as `gateway.env` is kept. |
| Pass token `p1.` | one OpenCode run | its environment (`GROVE_PASS_TOKEN`) | the `brain` routes of one world, as one agent, with `grove.read` and `grove.propose`. It expires with the pass's time cap, and the API refuses it once the run record has ended. |
| Gateway run token `g1.` | one OpenCode run | its provider key (`GROVE_RUN_TOKEN`, read by `runner/opencode.json`) | model calls of one world, one run and one job, up to the pass's token cap. |

The runner reaches the API on the runner network (`http://api:8000`), and nginx answers 404 for
`/v1/internal/`, so no request from outside the box reaches these routes. The API mints both pass
tokens with `GROVE_GATEWAY_SECRET`; the pass token's key is derived from
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
   tool, and returns its summary. A watchdog kills the group at the time cap. OpenCode's data,
   state and temporary folders are in the pass's own folder (`passes/<run id>/`), which is
   removed when the pass ends, also when it fails: its session store holds every tool output,
   so the person's words, and nothing of a pass stays in the runner's HOME, where no forget
   reaches. A runner killed before that removes every pass folder when it starts again, and it
   rebuilds OpenCode's config folder from nothing at each start. The watchdog kills OpenCode's
   process group; OpenCode starts each bash call in a session of its own, so a `brain` call
   already sent finishes on its own (one HTTP call), and the writer keeps it to one effect.
4. `end_pass` hands back each claimed request the pass did not settle: `fail_request` with a
   retry (code `not_proposed`, or `pass_<outcome>`), so the next pass applies it once. The writer
   keys a commit on the request id, so a request that committed before a kill is saved and is
   never applied again. The run record gets its tokens from `model_calls` and its outcome.
5. After a Learner pass that changed items, a Curator job is queued on those items. The
   Connector's job is queued on Appendix C's trigger: 10 changed items since its last pass, or a
   contradiction detected since then.

The interactive lane comes first (spec 2327, 3008) while one pass runs per world: when a request
is due and a Curator or Connector pass of the world runs, the due list says so (`preempt`), the
runner stops that pass (outcome `preempted`), its job goes back to the queue at once with no
attempt spent, and the Learner starts on a later poll.

A runner that is killed with no time to end its passes leaves its requests `applying` until their
leases pass; then they are due again. Its run reads `running` until the time cap plus the margin
has passed; then the due list marks it `abandoned` and puts the job it held back in the queue. A
job put back joins a queued job of the same agent when there is one, so a world holds one queued
job per agent. A stop of the service ends a Curator or Connector pass as `stopped`, and its job
goes back with no attempt spent. A stop signal (the nightly backup stops the service) ends
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
| `brain recall TEXT` | `POST /v1/internal/brain/recall` (the text in the body, never in a URL an access log keeps) | a lexical lookup over titles, summaries and search terms, no model call. `hosts-b` replaces it with D7's recall |
| `brain get ITEM` | `GET /v1/internal/brain/items/{id}` (an id of the row id form only) | one item, its active links and its active evidence rows |
| `brain propose [--request ID]` | `POST /v1/internal/brain/propose` | the batch from stdin (a heredoc) or `--file`; saved with a receipt, or declined with the writer's reasons. A Learner proposes under one of its requests; one repair of a decline is allowed |
| `brain finish ID` | `POST /v1/internal/brain/finish` | a request of the pass with nothing to save: saved with no commit |

### Learner source eligibility

The Learner packet uses each retained message's key, locator, time, speaker and modality.
It orders eligible messages by their own instants. The source read time never fills an absent
message time. An untimed host or views request may use its explicit `Request.said_at`.
The host has the person's voice only when both its verbatim flag and host message ID exist.

`input_complete`, `held_messages` and `input_reasons` describe unavailable source work.
Held messages have no data block. With no eligible message, the packet has no example batch.
The Learner must leave an incomplete request pending. The writer also checks current input
eligibility before completion, including the no-op `finish_request` path.

Typed export sources remain unadmitted. They do not fall through to legacy time or speaker
inference. These checks do not activate the new source adapters, model extraction dispatcher,
durable source holds, or backfill queue. The bounded extraction component and its remaining
integration requirements are in [extraction.md](extraction.md).

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
| Resolve held source identities and unknown source times through approved source facts. The packet already preserves retained speakers and refuses unsupported typed input. Durable hold resolution and requeue remain open. | `ingest-b` |
| `brain recall` is a full-text lookup, not D7's recall. | `hosts-b` |

## Added files

The files and notes a person adds in setup go to `/var/lib/grove-runner/worlds/<world id>/` through `grove.api.agent_files`. Notes are in its `notes/` folder. Once that folder exists, the runner starts each pass of the world in it, so it is the working folder of OpenCode. The API owns the folder and the runner's group reads it. OpenCode's session store and logs stay in the pass folder, which goes when the pass ends. The permission contract still denies OpenCode's `read` tool.
