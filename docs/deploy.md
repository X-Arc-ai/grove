# Deploying Grove

Grove runs on one box behind one public host name. This page says what runs there, which files it
holds, how a commit is deployed, and how the store is backed up and checked. It holds no secret
value: secrets are named by their file and variable only.

## What runs on the box

The box runs Ubuntu 24.04 with 4 vCPU and 8 GB of memory. nginx and certbot run on the host. Docker
runs the compose project `grove` from `deploy/compose.yml`.

| Service | What it is | Network | Runs as |
|---|---|---|---|
| `neo4j` | Neo4j Community, the pin of `docker-compose.dev.yml`. Heap 1 GB, page cache 512 MB, Cypher 25 by default. Data in the volume `grove-neo4j-data`. | `store` (internal) | the image's user |
| `api` | FastAPI and uvicorn, `grove.api.app:create_app`. `GROVE_ENV=production`. Listens on `127.0.0.1:8000` only; nginx is the only way in, with a 10 MB limit on a request body; only `/v1/sources/upload` takes up to 256 MiB, with no request buffering (uploads, below). Read-only root file system; `/tmp` is a tmpfs of 320 MiB. A memory limit of 4 GB, with no swap past it. | `store`, `runner_v2`, `egress` | uid 10001 `grove-api` |
| `gateway` | The model gateway, `python -m grove.models.gateway` from the API's image (`docs/models.md`, the gateway). It holds the model keys and `GROVE_GATEWAY_SECRET`, and writes each call to `model_calls`. It listens on port 18300 of its networks (`GROVE_GATEWAY_HOST=0.0.0.0`, since the runner's container cannot reach a loopback address) and publishes no port; every call needs a run token signed with `GROVE_GATEWAY_SECRET`. Read-only root file system. | `store`, `runner_v2`, `egress` | uid 10001 `grove-api` |
| `runner` | The runner loop, `python -m grove.runner`: it asks the API for due work and runs each pass in OpenCode 1.18.32 against the gateway (`GROVE_GATEWAY_URL=http://gateway:18300/v1`), with the config and the agents of the repo's `runner/` folder (`/opt/grove/runner` in the image). OpenCode is checked against its release checksum, runs with `OPENCODE_DISABLE_CLAUDE_CODE=1` and HOME `/var/lib/grove-runner`, and has no plugin. | `runner_v2` (internal) | uid 10002 `grove-runner` |

An internal network has no route out. So the store reaches nothing but the API and the gateway,
and the runner reaches the API, the gateway and no address outside the box: it holds no store
credential and no model key. The runner can still open a connection to the host itself through its
network's bridge (sshd and nginx answer there). That bridge has the fixed interface name
`grove-runner`, so a host firewall rule can name it; the rule itself belongs with the runner's
egress allowlist (open point). Only the API and the gateway have a route out (`egress`): the API
for the model providers, the mail provider and Google, and to fetch client metadata documents from
any global https host (`docs/hosts.md`); the gateway for the model providers.

The public host name is configuration: `GROVE_PUBLIC_BASE_URL` in `/etc/grove/api.env`, today
`https://app.grove.x-arc.ai`. The A record is DNS only, with no proxy in front of the box. nginx has
no rule on the user agent: Meta's metadata fetcher, `meta-webindexer/1.1`, gets the same answers as
any client.

## The API on the box today

| Path | Answer |
|---|---|
| `GET /` | The homepage with no `grove_session` cookie, the built page of the views with one. The API image runs `web/build.py` and sets `GROVE_WEB_DIST`. `docs/views.md` has the view endpoints. |
| `GET /signin` | The built page of the views, which shows the sign-in card. |
| `GET /privacy` | The privacy page. `web/build.py` makes its table from `docs/data-flow.md`, the one file of `docs/` that reaches the image. |
| `/v1/auth/*` | Sign-in and logout. `docs/signin.md` has the routes. nginx limits `/v1/auth/email/` per address to 10 requests a minute with a burst of 10, and answers 429 above that. |
| `POST /v1/sources/upload` | One export of past AI conversations as the raw body, screened in code before anything is stored (`docs/ingest.md`). The session cookie or an upload-only token. 503 `unavailable` while a screening file is absent (uploads, below). `GET /v1/sources/connectors` lists the coverage of the world's connectors. |
| `GET /healthz` | `{"status": "ok", "store": "ok", "build": "<sha>"}`, or 503 with `"store": "unreachable"`. No sign-in. D13's health views are `/v1/health/person` and `/v1/health/operator`, built later. |
| `GET /.well-known/oauth-protected-resource` and `/.well-known/oauth-protected-resource/mcp` | The protected-resource metadata (RFC 9728): `resource` is `<base>/mcp`, the authorization server is `<base>`, and the scopes of A.3. |
| `GET /.well-known/oauth-authorization-server`, `/.well-known/oauth-authorization-server/mcp` and `/.well-known/openid-configuration` | The authorization server metadata (RFC 8414): the issuer `<base>` and its endpoints. |
| `/register`, `/authorize`, `/authorize/decision`, `/token`, `/revoke` | The OAuth 2.1 authorization server of `/mcp`. `/authorize` fills `consent.html` of `GROVE_WEB_DIST`. nginx limits `/register` per address to 4 requests a minute with a burst of 20, and `/token`, `/authorize` and `/authorize/decision` per address to 30 requests a minute with a burst of 20 (one count for the three), and answers 429 above that. `/revoke` has no limit of nginx. |
| `/mcp`, `/mcp/` | The MCP transport. With no token: 401 with `WWW-Authenticate: Bearer resource_metadata="<base>/.well-known/oauth-protected-resource", scope="grove.read grove.write"`. Any other path with a trailing slash is a typed 404, never a redirect. |

`docs/hosts.md` has the authorization server and the MCP transport that answer at these paths: every
endpoint, the consent page, the exact `WWW-Authenticate` headers, and how a host connects.

## Files on the box

| Path | What | Mode |
|---|---|---|
| `/opt/grove/src` | The checkout. It pulls from GitHub with the box's deploy key `/root/.ssh/grove_deploy`, which GitHub holds as read-only. | root |
| `/etc/grove/*.env` | The secret files below. | 600, root |
| `/etc/grove/google-oauth.json` | Google's Web client file, mounted read-only into the API at `/run/grove/google-oauth.json` (`GROVE_GOOGLE_OAUTH_FILE`). | 400, uid 10001 |
| `/etc/grove/screening/` | The screening files `rename.json` and `denylist.txt`. The folder is mounted read-only into the API at `/run/grove/screening` (uploads, below). | 500, files 400, uid 10001 |
| `/var/lib/grove/deployed.sha`, `previous.sha` | The sha the box runs, and the last different sha it replaced. | root |
| `/var/log/grove/deploy.log`, `backup.log` | A start line for each deploy that reaches its build, and a result line (`served, smoke ok`, `served, smoke FAILED` or `runs, services NOT healthy`) for each that reaches its service start. One line per dump, per removed dump, per failed backup, and per restore check that reaches a match, a mismatch or a named failure. | 640, root |
| `/var/backups/grove/` | `grove-<UTC stamp>.dump`, its `.fingerprint.json` and its `.dump.sha256`. | 700, files 600 |
| `/var/lib/grove-runner` | The runner's HOME. The host user `grove-runner` owns it; it has no sudo and is not in the docker group. | 700 |

## Secret files

| File on the box | Variables | Where it comes from |
|---|---|---|
| `neo4j.env` | `NEO4J_AUTH` | Generated on the box by `deploy/bootstrap.sh`, never printed. Neo4j takes it as its password on first start, and the API reads the same file. |
| `api.env` | `GROVE_PUBLIC_BASE_URL` | Written by `deploy/bootstrap.sh`. Not a secret; it names the host. |
| `model-api.env` | `MODEL_API_KEY` | The team's Meta Model API key file. |
| `resend.env` | `RESEND_API_KEY`, `MAIL_FROM` | The mail provider's send-only key. |
| `modal.env` | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | The Modal token, once it exists. Optional until then. |
| `google-oauth.json` | the Web client | The Google OAuth client file. |
| `runner.env` | `GROVE_RUNNER_TOKEN` | Generated on the box by `deploy/runner-secrets.sh`, never printed. Only the runner service reads it. |
| `runner-auth.env` | `GROVE_RUNNER_TOKEN_SHA256` | The sha256 of the runner token, computed again from `runner.env` by each run of `deploy/runner-secrets.sh`. The API reads it. |
| `gateway.env` | `GROVE_GATEWAY_SECRET` | Generated on the box by `deploy/runner-secrets.sh`, never printed. The API (it mints the gateway run token and the pass token of each pass) and the gateway read it. Without it, or without `runner-auth.env`, the API's runner routes answer 503. |
| `glimmer.env` | `GROVE_GLIMMER_BASE_URL`, `GROVE_GLIMMER_KEY` | The Modal server's URL and proxy token (`docs/models.md`). The API and the gateway read it. Optional until Glimmer is used. |
| `screening/rename.json` | the rename map; the API finds it through `GROVE_RENAME_MAP` | The operator's own file of names and their replacements. Optional for a deploy (uploads, below). |
| `screening/denylist.txt` | the denylist, one term a line; the API finds it through `GROVE_DENYLIST` | The operator's own file. Optional for a deploy (uploads, below). |

Copy a file from the operator's machine through ssh standard input, so no value is on a command line
and no file is pushed with `scp` or `rsync`:

```
ssh root@app.grove.x-arc.ai 'umask 077; cat > /etc/grove/model-api.env' < <local key file>
```

For every other ssh command, give `ssh -n`, so that ssh reads nothing from the standard input of
the shell that runs it.

## The runner's secrets

`deploy/runner-secrets.sh` makes `gateway.env`, `runner.env` and `runner-auth.env` on the box, mode
600 and root's. Initialize these files **before** the first deploy that starts the gateway;
the API must pass its health check before the runner starts.
The gateway intentionally refuses startup without a secret of at least 32 bytes; it cannot answer
`/health` until its constructor succeeds. The runner's separate unconfigured wait does not make
the gateway healthy. An absent or short secret must never be bypassed to complete a deploy.

1. Obtain an explicit request with the full target sha and refresh checkout, service, network,
   marker and lock evidence. Confirm the reviewed initializer already exists in the clean host
   checkout. If it is absent, stop for a source-delivery decision. Do not use a failed deploy to
   obtain it, and do not transfer a script to the box.
2. Hold the operator lock for the complete initialization-and-deploy sequence. Under that lock,
   run the existing initializer once with the host lock:

   ```
   ssh -n root@app.grove.x-arc.ai 'flock /run/grove/deploy.lock /opt/grove/src/deploy/runner-secrets.sh'
   ```

3. If initialization fails, retain the failure and stop. Do not retry it or proceed to deployment.
4. While the same operator lock remains held, deploy the authorized full sha once through
   `deploy/deploy.sh`, which acquires the host lock itself. Do not nest the operator lock wrapper.
   Compose must recreate the API, gateway and runner to consume the new files.
5. If deployment or smoke fails, retain the failure and stop for a new request. Require exact-sha
   public health, all public smoke checks, healthy services and configured-runner acceptance.
   Do not lengthen a smoke timeout or repeat a failed check to erase the original failure.

A recovery of an already-served sha and a later deploy of the source repair are separate requests.
The same-sha recovery preserves the previous different healthy build in `previous.sha`.
Neither a source merge nor successful initialization proves healthy services.

It prints file names with `created` or `kept`, and no value. It keeps a secret or a token that is
there, and computes `runner-auth.env` again from `runner.env` at every run, so the hash the API
holds always matches the token the runner sends. A new value goes from `python3` straight into a
temporary file in `/etc/grove`, which then replaces the file whole. It refuses to run when
`/etc/grove` does not exist (run `deploy/bootstrap.sh` first), and stops on a value there that is
under 32 characters. To change the runner token, remove its line from `runner.env`, run the script
and deploy again, so the API and the runner both start with the new pair.

`deploy/runner-secrets.sh --check` is a separate read-only prerequisite. It validates the three
plain files, current-owner private permissions, one exact field per file, secret/token minimum
lengths and the token/hash relation. Blank lines and comments are allowed; duplicate or unknown
fields, shell text, interpolation and escapes are refused. It parses data without sourcing files.
It creates nothing and changes no bytes, owner, mode, modification time or change time. Filesystem
read-access times can change on systems without `O_NOATIME`.

The check prints only `runner_configuration_ok`, or `runner_configuration_invalid` with a fixed
file name (or `configuration_directory`). Operator evidence contains only this status and file
metadata, never values, hashes, complete environments or raw configuration. Apply always checks
its own `GROVE_ETC` directory, even if a different initializer directory override exists.

## Deploy a commit

A deploy names one full sha that is on `origin/develop`. One deploy runs at a time: the operator's
machine holds a lock, and the box holds a second one, `/run/grove/deploy.lock` in a folder only root
can enter, that the backup, the restore check and `services-up.sh` also take. A deploy waits up to
30 minutes for it. From the operator's machine:

```
deploy/with-deploy-lock.sh ssh -n root@app.grove.x-arc.ai /opt/grove/src/deploy/deploy.sh <full sha>
```

`with-deploy-lock.sh` takes `~/.grove/locks/deploy.lock.d` (a `mkdir` lock with an owner file) and
starts a detached releaser in a process group of its own. The releaser frees the lock one hour
after it was taken, whether the holder still runs or not, and only while the owner file still holds
its own token. If the releaser is gone too (the machine restarted, or a kill took both), the next
caller takes over a lock that is past its cap and says so. It exits 75 when the lock is held. Give
ssh the operator's own key options; they are not part of this repo.

On the box, `deploy/deploy.sh <sha>`:

1. takes the box lock `/run/grove/deploy.lock`;
2. fetches `develop` from GitHub and refuses a sha that is not on it, and a checkout with local
   changes or ignored files;
3. checks out the sha and hands over to that commit's own `deploy/apply.sh`, which
4. runs `runner-secrets.sh --check` immediately after its lock and sha assertions, before any
   screening directory creation, permission change, host-file installation, build, migration,
   marker write or service replacement. It then checks the other secret files and screening
   files, refusing missing required files, open permissions, foreign env owners or screening
   links. It makes `/etc/grove/screening` when absent and hands existing screening files to uid 10001;
5. installs the host files of that commit (`deploy/install-host.sh`): the nginx sites, the
   certificate on first use (certbot, webroot), the backup units, the certbot reload hook and log
   rotation. If `nginx -t` refuses the new sites, the old ones go back and the deploy stops;
6. builds the `api` and `runner` images (the only build on the box; the gateway runs the `api`
   image), starts `neo4j`, runs the migrations from the image's copy of `server/`, records the sha,
   and starts `api`, `gateway` and `runner`;
7. runs `deploy/smoke.sh` against the public host, writes the result to `deploy.log`, and removes
   the images of every build except this one and the one it last replaced.

If `apply.sh` fails before compose replaces the containers, `deploy.sh` moves the checkout back to
the sha the box still runs and installs that commit's host files again. From that point on,
`deployed.sha` and the checkout name the new build, also when it does not turn healthy; `deploy.log`
says so, and a deploy of the previous sha rolls it back.

`--ref <branch>` with `GROVE_ALLOW_BRANCH_DEPLOY=1` deploys a sha of another branch, to try a
change on the box before its merge; without the variable only `develop` deploys. To roll back,
deploy the sha in `/var/lib/grove/previous.sha`; a redeploy of one sha leaves it as it is.

The smoke checks also run from any machine:

```
deploy/smoke.sh https://app.grove.x-arc.ai <sha>
```

They check that `/healthz` answers ok for that build, that `/`, `/signin` and `/privacy` each answer with a page, that both metadata paths name
`<base>/mcp` (also for `meta-webindexer/1.1`), and that an unauthenticated `/mcp` call gets 401 with
the metadata URL.

### Runner network transition

The fixed bridge `grove-runner` uses logical network key `runner_v2` and Docker network name
`grove_runner_v2`. Both identities differ from the earlier `runner` / `grove_runner` network.
A change to the old network's bridge options caused Compose to refuse its active API and runner
endpoints when it tried to create the migration container. A new Docker name alone is insufficient:
Compose can still select the old resource through its logical network label.

The separate network lets the migration container use the new topology while the old API and runner
remain attached to the legacy network. Ordinary service replacement then moves the API, gateway,
and runner to the new network. The store network, Neo4j container, data volumes, credential files,
and both deploy locks retain their existing roles. The runner network stays internal, its bridge
stays fixed, and the gateway publishes no port. Service DNS names remain `api` and `gateway`.

Keep the legacy network for rollback. Deploys do not prune it. If network creation or migration
fails before the target replaces services, the old endpoints remain connected and the existing
checkout/host-file recovery still applies, including when the old deploy entrypoint starts the
upgrade. A failure after target service replacement keeps the target marker and reports unhealthy
services; it does not claim that the old build recovered. An explicit deploy of the old revision
reuses the legacy network and removes the gateway container as an orphan when that revision has
no gateway service. A same-SHA redeploy reuses the new network and preserves `previous.sha`.

The deployment tests model network labels, names, options, active endpoints, and live container
health with synthetic state. They cover the original refusal, a name-only negative control,
pre-replacement recovery, unhealthy target services, repeated deployment, and rollback. Their
health response comes from live simulated containers, independently of the SHA marker. These tests
do not prove native Compose behavior or live host recovery; those remain deployment acceptance.

## Uploads and the screening files

`POST /v1/sources/upload` takes one export of past AI conversations as the raw body: Claude Code
sessions, Codex rollouts, ChatGPT and claude.ai exports (`docs/ingest.md` has the contract).
Screening runs in code on the box before anything is stored. It reads two files that only the box
holds:

| On the box | In the API | Variable |
|---|---|---|
| `/etc/grove/screening/rename.json` | `/run/grove/screening/rename.json` | `GROVE_RENAME_MAP` |
| `/etc/grove/screening/denylist.txt` | `/run/grove/screening/denylist.txt` | `GROVE_DENYLIST` |

compose mounts the folder read-only, not the two files, so a deploy works while a file is absent.
`apply.sh` makes the folder when it is absent. The box is in one of two states:

- **A screening file is absent.** The upload answers 503 `unavailable` and stores nothing.
  Sign-in, the views and every other path serve as before. The deploy does not fail: `apply.sh`
  logs one line, `uploads answer 503 until both screening files are placed; absent: <file names>`.
- **Both files are in `/etc/grove/screening` at 600.** `apply.sh` hands each file to uid 10001 at
  400, and the folder at 500, and uploads are screened with them. A file that is open to group or
  others, or a link, stops the deploy before any chown: place it at 600 and deploy again.

The API reads both files at each upload. A file that it cannot read, that is open to group or
others, or that does not parse, also makes the upload answer 503.

Copy a file from the operator's machine as the other secret files: through ssh standard input,
with umask 077. Write it to a new name and rename it, so that an upload never reads half a file:

```
ssh root@app.grove.x-arc.ai 'umask 077; mkdir -p /etc/grove/screening; cat > /etc/grove/screening/denylist.txt.new && mv /etc/grove/screening/denylist.txt.new /etc/grove/screening/denylist.txt' < <local denylist file>
```

Do the same for `rename.json`. The new file is root's at 600, which uid 10001 cannot read, so
uploads answer 503 until the next deploy hands it over. To hand it over now, deploy the sha in
`/var/lib/grove/deployed.sha` again.

**nginx.** `location = /v1/sources/upload` takes a body up to 256 MiB (`client_max_body_size
256m`). That is the API's own cap: `GROVE_UPLOAD_MAX_BYTES`, 268435456 bytes, which compose sets to
the code's default. `proxy_request_buffering off` streams the body to the API, so nginx keeps no
copy of it on disk. The read and send timeouts are 600 s, as a large export takes minutes to parse
and screen. Every other path keeps the 10 MB limit. To change the cap, change
`GROVE_UPLOAD_MAX_BYTES` and the nginx limit together. The API spools a body past 8 MiB to `/tmp`,
a tmpfs of 320 MiB: room for the one upload that runs at a time, at the cap. A second upload
while one runs answers 429 `rate_limited` with `retry_after_seconds`.

**Memory.** Archive expansion, lines, chats, messages and text have separate limits
(`docs/ingest.md`). Synthetic macOS parse-and-screen probes measured up to 1,409 MiB of peak
RSS growth; they did not include store writes or establish a universal maximum. compose gives
the api `mem_limit: 4g` with no swap past it, the tmpfs inside it. If an upload passes the limit,
the kernel stops the api and docker starts it again (`restart: unless-stopped`); the limit
contains that memory use to the api container.

**Upload-only tokens.** A script that uploads without the session cookie uses an upload-only token
for one world (`docs/ingest.md`). The operator mints it inside the running `api` container. compose
reads `GROVE_BUILD_SHA` in the file, so the command sets it as `deploy/lib.sh` does:

```
ssh -n root@app.grove.x-arc.ai 'GROVE_BUILD_SHA=$(cat /var/lib/grove/deployed.sha) docker compose --project-name grove --file /opt/grove/src/deploy/compose.yml exec -T api python -m grove.ingest token mint --world <world id>'
```

`token mint` prints `{id, token, world_id, expires_at}` once, as one JSON line: the raw token is
nowhere else. `--minutes N` sets its life, from 1 to 1440 minutes, 60 by default. In the same
command, `token revoke --id <id>` revokes a token by the id that mint printed, and `token list
--world <world id>` lists a world's tokens by id and time. `exec` runs in the live container, not
in a one-off container, so it needs no box lock. While the nightly backup stops the API, it fails.

## Backups

Neo4j Community has no online backup: a dump needs the server stopped. `grove-backup.timer` runs
`deploy/backup.sh` each day at 03:30 UTC, and again after the box was down at that time. The script:

1. takes the box lock, so no deploy runs during it, and removes the work folder that a killed
   backup left;
2. stops the runner, the gateway and the API, so nothing writes (the gateway writes a
   `model_calls` row for each call, and a write between the two fingerprints fails the backup);
3. starts Neo4j if a boot or a failed run left it stopped, waits until it is healthy, and records
   the dump fingerprint of the store;
4. stops Neo4j and runs `neo4j-admin database dump`;
5. starts Neo4j, checks that the fingerprint did not move, and starts the API, the gateway and
   the runner;
6. keeps the dump with its fingerprint and checksum;
7. runs `deploy/restore-check.sh` on the new dump, and only when it matches, removes dumps older
   than 30 days.

The API is down for the length of the dump: 34 seconds on the first run, with a store that held
only its schema. If a step fails, or systemd stops the script, it starts Neo4j, the API, the
gateway and the runner again before it exits. The unit's `ExecStopPost` runs `deploy/services-up.sh`
after every run, so a script that was killed outright also leaves the services running.
`grove-services.service` runs it too, at every boot and after any failed backup (`OnFailure`), and
tries again each minute until it succeeds, for a host that went down in the dump window or a store
that took long to come back. `services-up.sh` first removes a one-off container that a killed run
left, so nothing else holds the store. compose refuses to start the API while the store reports
unhealthy, so the script keeps starting the API, the gateway and the runner until the API's health
check reads the store, the gateway's health check answers and the runner runs.

Run a one-off container by hand only under the box lock
(`flock /run/grove/deploy.lock docker compose --project-name grove ... run ...`): `services-up.sh`
removes every one-off container it finds without the lock held by another run. The backup needs the
fingerprint before the dump: if that read fails, no dump is made, and the unit fails.

`deploy/restore-check.sh <dump>` refuses a dump with no checksum file or a wrong checksum, loads it
into a scratch Neo4j with its own volume and an internal network of its own, and compares the
fingerprint of what loaded with the recorded one. It never touches the live store. It removes the
scratch container, volume and network at the end, and first removes any that a killed check left.
It exits 3 on another fingerprint.

```
systemctl list-timers grove-backup.timer     # when the next backup runs
systemctl start grove-backup.service         # one backup now
journalctl -u grove-backup.service           # its output
```

**The dump fingerprint** (`python -m grove.store.dump_fingerprint`) is one sha256 over every node
with its labels and properties, every relationship with its type, its properties and the digests of
its two end nodes, and every constraint and index definition. A changed byte anywhere changes it.
It prints only the digest and counts per label and type, never a row. It is not D3's fingerprint,
which covers the projections and leaves text out so that a forget keeps it; `writer-b` builds that
one and the restore procedure of D3 (load, rebuild the projections from history, replay the forget
ledger, compare).

## First setup of a box

1. Install nginx and certbot with apt, and Docker with the compose plugin from Docker's apt
   repository. Check that repository's key first: its fingerprint is
   `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`.
2. Make the deploy key on the box (`ssh-keygen -t ed25519 -f /root/.ssh/grove_deploy`), and add its
   public half to the repository as a read-only deploy key (`gh repo deploy-key add`, with no
   `--allow-write`). Write GitHub's host keys from `gh api meta` (`.ssh_keys`) to
   `/root/.ssh/known_hosts_github`.
3. Clone `git@github.com:X-Arc-ai/grove.git` into `/opt/grove/src` with that key and
   `--branch develop`, and set the clone's `core.sshCommand` to the same key and host-key file.
4. Run `deploy/bootstrap.sh --base-url https://<host>`. It makes the folders, the `grove-runner`
   user, `neo4j.env`, `api.env` and an empty `runner.env`, then installs the host files and asks for
   the certificate. Set `GROVE_ACME_EMAIL` to give certbot a contact address for a new account; with
   no variable, it registers with no address.
5. Copy the other secret files (above), and the two screening files (uploads, above). A deploy
   also works without the screening files; uploads then answer 503.
6. Initialize the runner's three files under the operator and host locks, using the existing
   reviewed checkout, and stop if initialization fails (the runner's secrets, above).
7. Deploy the authorized full sha once under the same operator lock. The checked-in deploy takes
   the host lock and checks configuration before it changes host files or services.
