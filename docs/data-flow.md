# Where the data goes

This is the path-by-path statement of where a person's data goes. A row marked Runs is live on the
server today. A row marked Planned does not run yet: no data of a person takes that path. The `forget`
lane replaces this page with a statement generated from the deployed configuration. A lane is one work
stream of this build. The table below is the table on the privacy page: `web/build.py` builds it from
this file.

| Data | Where it goes | Terms | Status |
|---|---|---|---|
| Your email address and the sign-in code, when you ask for a code | Resend, the mail provider, region eu-west-1, to send the mail | Used to send that mail. Resend's data terms are not named here yet. | Runs |
| The Google sign-in request, when you choose Google | Google gets the request. Grove gets your name, your address and a flag that says Google verified the address. | Scopes `openid`, `email` and `profile` only. | Runs |
| Items, links, history, sources | Grove's store on the server | Private per person; access by identity. | Runs |
| Backups of the store | A daily offline dump on the same server, kept 30 days | Stays on the server; no outside party. | Runs |
| Calendar events, when you connect the calendar | Google Calendar to Grove's server, read-only, in its own consent | Scope `calendar.readonly`, asked only when you connect the calendar. | Planned |
| Past AI conversation exports you upload: Claude Code sessions, Codex rollouts, ChatGPT and claude.ai exports | Grove's server, where screening in code runs before the `sources` table. The uploaded file is not kept. There is no local runner. | Stays on the server at this step; no outside party. | Runs |
| Uploads: WhatsApp chats, voice notes, notes, files and images | Grove's server, where screening in code runs before the `sources` table. There is no local runner. | Stays on the server at this step; no outside party. | Planned |
| Screened text for extraction, verification, belief revision, audits and read-side sentences | Meta Model API, Spark 1.3, Standard mode | Standard mode content "is not used to train Meta models". Retention for text models is not stated in the developer docs. | Planned |
| Voice notes | Meta Model API, Voice Transcribe | Meta lists zero data retention. | Planned |
| Screened text and images for triage, pairwise judgments and the second opinion | Muse Glimmer hosted on Modal | Modal's data terms are not checked yet, so this path carries synthetic data only until they are named here. | Planned |
| Evidence packets to the AI host | The host's systems | Per host. Muse trains on conversation and tool-call trajectories after PII sanitisation by default, with an opt-out. Meta AI "uses your interactions with AI tools to improve the quality of AI at Meta", and training on connector payloads is not published. Muse Code subscriptions default to Contributor mode. | Planned |

Every model call goes through the model-call layer (`docs/models.md`), which records it in
`model_calls`. The agents reach a model only through the internal gateway on the server, and the
layer sends nothing to a host outside its egress allowlist.

## Open

Screening runs in code on Grove's server and calls no model. Today it drops or masks every
candidate in code. A candidate that code cannot settle is treated as sensitive: it is masked, or
its message is dropped. No screened span goes to Glimmer or to any other model. The `forget` lane
states the final rule here.
