# Hackathon record

Snapshot checked against GitHub on 2026-11-03. This is an interim event record, not a final
submission claim. Dates are UTC. Open PRs are listed separately from completed merges in the
State column. Refresh this table after later merges and before final submission acceptance.

## Event pull requests

Every PR visible at this checkpoint is listed below. The initial repository commit precedes
these PRs. A merged PR establishes repository inclusion, not public deployment or user acceptance.

| PR | Created (UTC) | State | Merged (UTC) | Merge SHA | Contribution |
|---|---|---|---|---|---|
| [#1](https://github.com/X-Arc-ai/grove/pull/1) | 2026-11-01T21:17:53Z | MERGED | 2026-11-01T23:53:11Z | d3ac2eecabc48abff1346ac71fdb529fbb540989 | Foundation: store map, migrations, world-scoped query layer, clock, seed, lints, Validate |
| [#2](https://github.com/X-Arc-ai/grove/pull/2) | 2026-11-02T00:01:15Z | MERGED | 2026-11-02T12:23:40Z | c1c07d9e9363edff42fb1cf7dbd7a3c4e586c207 | Runner lint and the grove-ci pool: every job falls back to GitHub-hosted runners |
| [#3](https://github.com/X-Arc-ai/grove/pull/3) | 2026-11-02T00:33:30Z | MERGED | 2026-11-02T13:35:42Z | ac93c7ed012d28c877e45876c57bd278363bcc70 | deploy-a: the box, its deploy, the daily dump and the restore check |
| [#4](https://github.com/X-Arc-ai/grove/pull/4) | 2026-11-02T00:46:58Z | MERGED | 2026-11-02T13:06:16Z | 8931462f0696eb56810693993d32b21e0d063c47 | views-a: the views on the API, map positions per item, real sign-in screens |
| [#5](https://github.com/X-Arc-ai/grove/pull/5) | 2026-11-02T00:49:04Z | MERGED | 2026-11-02T12:58:51Z | 6f310501a2915048e5407e819d96aeae9d1103ef | Models: the metered model-call layer, Glimmer on Modal, and the gateway for OpenCode |
| [#6](https://github.com/X-Arc-ai/grove/pull/6) | 2026-11-02T02:14:06Z | MERGED | 2026-11-02T12:52:22Z | ec4a49a27daf3acceae5b638c5f4324801072ea5 | Writer library: B.2 operations for items, links and evidence, with receipts from a post-commit read |
| [#7](https://github.com/X-Arc-ai/grove/pull/7) | 2026-11-02T12:14:07Z | MERGED | 2026-11-02T16:44:31Z | 8ff93b6940d14b283712c43d8590282320bc8688 | Screening: D14 on arrival, the rename map and denylist, one prompt builder |
| [#8](https://github.com/X-Arc-ai/grove/pull/8) | 2026-11-02T14:39:56Z | MERGED | 2026-11-02T15:24:47Z | 169df8c1e2f46645e8d383f947c6e6811d08d3b7 | Sign-in: Google and an email code, sessions, the own item, a homepage and a privacy page |
| [#9](https://github.com/X-Arc-ai/grove/pull/9) | 2026-11-02T17:10:47Z | MERGED | 2026-11-03T03:27:48Z | 8afd00fd3fbd253d95711527efc895d28c75e3dc | writer-a2: the durable request queue, declines, current() and the reuse-first resolver |
| [#10](https://github.com/X-Arc-ai/grove/pull/10) | 2026-11-02T17:48:22Z | MERGED | 2026-11-02T18:18:29Z | 621b1b2f29fa748644cbcc65502bf9dc849100d4 | Models: give Modal a python in the Glimmer image |
| [#11](https://github.com/X-Arc-ai/grove/pull/11) | 2026-11-02T18:11:27Z | MERGED | 2026-11-03T04:20:42Z | 9a5cb23ba2935a66f0091e72ccc7e38c54695722 | Hosts: the OAuth 2.1 server, the consent page and the MCP transport |
| [#12](https://github.com/X-Arc-ai/grove/pull/12) | 2026-11-02T18:35:55Z | MERGED | 2026-11-02T18:55:06Z | 0a852958ca90a87b6e0170390972cfcb72db1aa2 | Glimmer on Modal: a one-minute idle window, and the facts of the first boot |
| [#13](https://github.com/X-Arc-ai/grove/pull/13) | 2026-11-03T06:04:47Z | MERGED | 2026-11-03T19:56:52Z | 57da012f05eecfd0641773e12e4f3a0912bff5cb | Ontology: provisional mints, two recorded judgments and the change log (D8 floor) |
| [#14](https://github.com/X-Arc-ai/grove/pull/14) | 2026-11-03T14:58:28Z | MERGED | 2026-11-03T17:20:15Z | dd6b3a57c41b100e9f8cacc08503d5694406db04 | Runner: OpenCode passes with Grove's four agents, the brain CLI, the loop, and the gateway on the box |
| [#15](https://github.com/X-Arc-ai/grove/pull/15) | 2026-11-03T15:00:43Z | OPEN | Not merged | None | truth-clock: verify, the judge, capabilities and source types (D4, D5) |
| [#16](https://github.com/X-Arc-ai/grove/pull/16) | 2026-11-03T15:01:48Z | OPEN | Not merged | None | D3: corrections never overwrite (writer-b) |
| [#17](https://github.com/X-Arc-ai/grove/pull/17) | 2026-11-03T15:08:36Z | DRAFT | Not merged | None | Ingest: chat-export connectors, the upload route and upload-only tokens |
| [#18](https://github.com/X-Arc-ai/grove/pull/18) | 2026-11-03T22:04:36Z | DRAFT | Not merged | None | Submission documents: judge path, architecture, and event evidence |

## Pre-event material

The pre-event Grove material, the simulation of the views (the sim), supplied the export-guide screens, graph visualizer, worker
renderer, and their associated presentation assets. Their import is recorded in
[PR #4](https://github.com/X-Arc-ai/grove/pull/4) and
[the import commit](https://github.com/X-Arc-ai/grove/commit/9478035eeb6783b53958ede3dd343a38ffd05f2b).
The imported code is not claimed as event work. Later changes are visible in subsequent diffs.

- Graph and shell: `web/native-app.js`, `web/style.css`, `web/index.html`.
- Worker renderer and styles: `web/agents.js`, `web/agents.css`.
- The vendored public font CSS for Inter and JetBrains Mono: `web/vendor/fonts.css`.
- Export-guide screens from the same simulation.

This attribution records provenance; the import PR's date does not change the material's age.
The source simulation's personal fixture data is not part of this attribution or the demonstration.

## Prior work

The maintainers' earlier Grove research and architecture decisions motivated this build. Those
private source documents and their historical experiments are background references, not files
included here. No historical benchmark score is presented as a result of this repository.
The repository's own public-safe provenance record is the import history above and this event
PR table. No independent public citation for the private experiments is supplied.

## Release evidence

See [the current acceptance record](docs/submission-status.md) and
[the architecture](docs/architecture.md). The final film, complete public judge path, fresh
candidate scans, and approved public release remain separate gates. The film uses invented data.
