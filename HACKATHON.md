# Hackathon record

Snapshot checked against GitHub on 2026-11-04 at 13:51 UTC, before this documentation refresh
was published. This is an interim event record, not a completed submission. Dates are UTC.
The State column records that snapshot; PR #18 includes this table and can merge only afterward.
Refresh the table after later merges and before final submission acceptance.

## Event pull requests

Every PR visible at this checkpoint is listed below. The initial repository commit precedes
these PRs. A merged PR establishes source inclusion, not deployment, CI success or user acceptance.

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
| [#15](https://github.com/X-Arc-ai/grove/pull/15) | 2026-11-03T15:00:43Z | MERGED | 2026-11-04T09:48:43Z | 38d6385da84a5061b53c23484586f537e6e35281 | truth-clock: verify, the judge, capabilities and source types (D4, D5) |
| [#16](https://github.com/X-Arc-ai/grove/pull/16) | 2026-11-03T15:01:48Z | MERGED | 2026-11-04T05:17:46Z | f1a60783934a6ef3072eab7dbb1a330afd3623cf | D3: corrections never overwrite (writer-b) |
| [#17](https://github.com/X-Arc-ai/grove/pull/17) | 2026-11-03T15:08:36Z | MERGED | 2026-11-04T04:34:00Z | 5e40ff99e3892f22767793158dab4d290082b96c | Ingest: chat-export connectors, the upload route and upload-only tokens |
| [#18](https://github.com/X-Arc-ai/grove/pull/18) | 2026-11-03T22:04:36Z | DRAFT | Not merged | None | Submission documents: judge path, architecture, and event evidence |
| [#19](https://github.com/X-Arc-ai/grove/pull/19) | 2026-11-04T04:48:14Z | MERGED | 2026-11-04T05:27:46Z | 503fae73896115ab8f1d43fc61a20c822305e736 | Preserve the legacy network during runner bridge upgrades |
| [#20](https://github.com/X-Arc-ai/grove/pull/20) | 2026-11-04T04:48:23Z | MERGED | 2026-11-04T12:27:08Z | 1e6354cee6fd6cc26985ef53a53b88910be898a2 | Read-only Calendar and Git source connectors |
| [#21](https://github.com/X-Arc-ai/grove/pull/21) | 2026-11-04T08:47:44Z | MERGED | 2026-11-04T13:43:22Z | 54486e5c3ef33e6c440a2b148c6af3f33759b7b3 | Implement authenticated erasure and restore-safe privacy controls |
| [#22](https://github.com/X-Arc-ai/grove/pull/22) | 2026-11-04T09:08:04Z | MERGED | 2026-11-04T10:39:20Z | ea2c49bd08bc414ff583ce38deddc7ff9f1a83be | Require runner configuration before gateway deployment |
| [#23](https://github.com/X-Arc-ai/grove/pull/23) | 2026-11-04T10:46:54Z | MERGED | 2026-11-04T13:38:39Z | e126c45370e3d88f95b2ead649c41867915269a0 | Apply bounded consequences inside the writer transaction |
| [#24](https://github.com/X-Arc-ai/grove/pull/24) | 2026-11-04T12:10:42Z | MERGED | 2026-11-04T12:23:05Z | f9033824bd0473f99077ba13e2b6110887792173 | Require API health before runner startup |
| [#25](https://github.com/X-Arc-ai/grove/pull/25) | 2026-11-04T12:22:36Z | MERGED | 2026-11-04T12:23:02Z | 1bc216a7a0c58ad90ef2b986266438e6246c7273 | Make Muse onboarding truthful with explicit source skips |
| [#26](https://github.com/X-Arc-ai/grove/pull/26) | 2026-11-04T12:28:57Z | MERGED | 2026-11-04T12:29:03Z | cb0d53e9bdd0c67231fcb499c021c3b7a59489cb | Connect setup to real ChatGPT and Claude uploads |
| [#27](https://github.com/X-Arc-ai/grove/pull/27) | 2026-11-04T12:33:46Z | MERGED | 2026-11-04T12:33:57Z | 2cd810ee1765fc9924d42ca76df7eb9240ee3c79 | Add restricted invented-data demo with screened seed and recall |
| [#28](https://github.com/X-Arc-ai/grove/pull/28) | 2026-11-04T12:34:19Z | MERGED | 2026-11-04T12:34:29Z | 1517e80290b87647684e2512be5354caf3f5aefa | Repair startup test database guard import |
| [#29](https://github.com/X-Arc-ai/grove/pull/29) | 2026-11-04T12:36:08Z | MERGED | 2026-11-04T12:36:14Z | 599db7e76a55703de4b19c9633dbfeda6008f805 | Add Pipedream account connection demo |
| [#30](https://github.com/X-Arc-ai/grove/pull/30) | 2026-11-04T12:39:12Z | MERGED | 2026-11-04T12:39:19Z | 664c96d122935298db8289806057bb90c1268084 | Refuse Pipedream status for designated demo worlds |
| [#31](https://github.com/X-Arc-ai/grove/pull/31) | 2026-11-04T13:39:38Z | MERGED | 2026-11-04T13:39:47Z | 586a6fb36819ae3730fae008bf494faf4d5adc2a | Refuse mixed recall and write batches before MCP dispatch |
| [#32](https://github.com/X-Arc-ai/grove/pull/32) | 2026-11-04T13:40:38Z | MERGED | 2026-11-04T13:40:44Z | cac4b95cbc7ce5284c04fcb684ebb2f115162144 | Restore guided setup and defer public UI assets |
| [#33](https://github.com/X-Arc-ai/grove/pull/33) | 2026-11-04T13:48:57Z | MERGED | 2026-11-04T13:49:09Z | 7bb03c026b30bf099c437f77fc9e7db1c6ec044b | Publish internal text and media ingest components |
| [#34](https://github.com/X-Arc-ai/grove/pull/34) | 2026-11-04T13:49:53Z | MERGED | 2026-11-04T13:50:10Z | 6346a9fc7c6c9b8119a9e7310cb93944f2fb1afe | Add inactive authority validation and file leases |

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

See [the acceptance record](docs/submission-status.md) and [architecture](docs/architecture.md).
Source publication proceeds under an explicit CI/review waiver; failed and cancelled checks are
not passes. The owner login/upload/Muse rehearsal, full feature scope, final film, final-candidate
scans and public release remain separate work. The film uses invented data only.
