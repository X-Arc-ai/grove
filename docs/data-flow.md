# Where the data goes

This is the skeleton of the path-by-path statement. No path below runs yet, so every row is marked
planned. The `forget` lane replaces this page with a statement generated from the deployed
configuration. A lane is one work stream of this build.

| Data | Where it goes | Terms | Status |
|---|---|---|---|
| Uploads: chats, notes, files, images | Grove's server, where screening in code runs before the `sources` table. There is no local runner. | Stated from configuration by the `forget` lane | Planned |
| Screened text for extraction, verification, belief revision, audits and read-side sentences | Meta Model API, Spark 1.3, Standard mode | Stated from configuration by the `forget` lane | Planned |
| Voice notes | Meta Model API, Voice Transcribe | Stated from configuration by the `forget` lane | Planned |
| Screened text and images for triage, pairwise judgments and the second opinion | Muse Glimmer hosted on Modal | Stated from configuration by the `forget` lane | Planned |
| Items, links, history, sources | Grove's store on the box | Stated from configuration by the `forget` lane | Planned |
| Evidence packets to the AI host | The host's systems | Stated from configuration by the `forget` lane | Planned |

Every model call goes through the model-call layer (`docs/models.md`), which records it in
`model_calls`. The agents reach a model only through the internal gateway on the server, and the
layer sends nothing to a host outside its egress allowlist.

## Open

The handling of a screening candidate that code cannot settle is not fixed yet. The `forget` lane
states it here: a candidate is either dropped or read by Glimmer on Modal.
