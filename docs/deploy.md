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
| `api` | FastAPI and uvicorn, `grove.api.app:create_app`. `GROVE_ENV=production`. Listens on `127.0.0.1:8000` only; nginx is the only way in, with a 10 MB limit on a request body. The lane that builds an upload route adds its own nginx location, with its limit and `proxy_request_buffering off`. Read-only root file system. | `store`, `runner`, `egress` | uid 10001 `grove-api` |
| `runner` | OpenCode 1.18.32, checked against its release checksum, with `OPENCODE_DISABLE_CLAUDE_CODE=1` and HOME `/var/lib/grove-runner`. A placeholder process runs until the `runner` lane lands its loop. | `runner` (internal) | uid 10002 `grove-runner` |

An internal network has no route out. So the store reaches nothing but the API, and the runner
reaches the API and no address outside the box: it holds no store credential and no model key. The
runner can still open a connection to the host itself through its network's gateway (sshd and
nginx answer there). A host firewall rule for the runner's bridge belongs with its egress list. Only the API has a
route out (`egress`), for the model providers, the mail provider and Google, and to fetch client
metadata documents from any global https host (`docs/hosts.md`).

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
| `runner.env` | the runner token only | Written by the `runner` lane. Only the runner service reads it. |

Copy a file from the operator's machine through ssh standard input, so no value is on a command line
and no file is pushed with `scp` or `rsync`:

```
ssh root@app.grove.x-arc.ai 'umask 077; cat > /etc/grove/model-api.env' < <local key file>
```

For every other ssh command, give `ssh -n`, so that ssh reads nothing from the standard input of
the shell that runs it.

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
4. refuses to go on if a secret file is missing or readable by group or others, if an env file has another owner, or if `/etc/grove` is open to group or others;
5. installs the host files of that commit (`deploy/install-host.sh`): the nginx sites, the
   certificate on first use (certbot, webroot), the backup units, the certbot reload hook and log
   rotation. If `nginx -t` refuses the new sites, the old ones go back and the deploy stops;
6. builds the `api` and `runner` images (the only build on the box), starts `neo4j`, runs the
   migrations from the image's copy of `server/`, records the sha, and starts `api` and `runner`;
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

## Backups

Neo4j Community has no online backup: a dump needs the server stopped. `grove-backup.timer` runs
`deploy/backup.sh` each day at 03:30 UTC, and again after the box was down at that time. The script:

1. takes the box lock, so no deploy runs during it, and removes the work folder that a killed
   backup left;
2. stops the runner and the API, so nothing writes;
3. starts Neo4j if a boot or a failed run left it stopped, waits until it is healthy, and records
   the dump fingerprint of the store;
4. stops Neo4j and runs `neo4j-admin database dump`;
5. starts Neo4j, checks that the fingerprint did not move, and starts the API and the runner;
6. keeps the dump with its fingerprint and checksum;
7. runs `deploy/restore-check.sh` on the new dump, and only when it matches, removes dumps older
   than 30 days.

The API is down for the length of the dump: 34 seconds on the first run, with a store that held
only its schema. If a step fails, or systemd stops the script, it starts Neo4j, the API and the
runner again before it exits. The unit's `ExecStopPost` runs `deploy/services-up.sh` after every
run, so a script that was killed outright also leaves the services running.
`grove-services.service` runs it too, at every boot and after any failed backup (`OnFailure`), and
tries again each minute until it succeeds, for a host that went down in the dump window or a store
that took long to come back. `services-up.sh` first removes a one-off container that a killed run
left, so nothing else holds the store. compose refuses to start the API while the store reports
unhealthy, so the script keeps starting both writers until the API's health check reads the store
and the runner runs.

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
5. Copy the other secret files (above).
6. Deploy a sha.
