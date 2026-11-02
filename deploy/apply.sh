#!/usr/bin/env bash
# The second half of a deploy: deploy.sh runs this file from the commit it deploys, with the box
# lock held. Build the images, migrate, start the services, then run the smoke checks.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

[ $# -eq 2 ] || die "usage: apply.sh <sha> <ref>; run deploy.sh instead"
sha=$1
ref=$2
[ "${GROVE_LOCK_HELD:-}" = 1 ] || die "run deploy.sh, which takes the box lock"
[ "$(git -C "$GROVE_SRC" rev-parse HEAD)" = "$sha" ] || die "the checkout is not at $sha"

for file in neo4j.env api.env runner.env google-oauth.json; do
    [ -f "$GROVE_ETC/$file" ] || die "missing $GROVE_ETC/$file (docs/deploy.md, secret files)"
done
# The API runs as uid 10001 and reads this one file through a read-only mount.
chown 10001:10001 "$GROVE_ETC/google-oauth.json"
chmod 400 "$GROVE_ETC/google-oauth.json"
# POSIX -perm forms only, one per group or other bit, so GNU and BSD find agree.
open_files=$(find "$GROVE_ETC" -maxdepth 1 -type f \( -perm -0040 -o -perm -0020 -o -perm -0010 \
    -o -perm -0004 -o -perm -0002 -o -perm -0001 \))
[ -z "$open_files" ] || die "a file in $GROVE_ETC is readable by group or others"

"$here/install-host.sh"

export GROVE_BUILD_SHA=$sha
previous=$(served_sha)
record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: start"

compose build api runner
compose up --detach --wait neo4j
# The migrations run from the image's copy of the checkout's server/ folder (store-map open point).
compose_run api python -m grove.writer.migrate

# From here compose replaces the containers, so this sha is what the box runs, healthy or not. The
# build it replaced stays on record for a roll back, also when one sha is deployed twice.
mkdir -p "$GROVE_STATE"
if [ -n "$previous" ] && [ "$previous" != "$sha" ]; then
    printf '%s\n' "$previous" >"$GROVE_STATE/previous.sha"
fi
printf '%s\n' "$sha" >"$GROVE_STATE/deployed.sha"
if ! compose up --detach --wait --remove-orphans api runner; then
    record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: runs, services NOT healthy"
    exit 1
fi

if "$here/smoke.sh" "$(public_base_url)" "$sha" </dev/null; then
    record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: served, smoke ok"
else
    record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: served, smoke FAILED"
    exit 1
fi

# Keep the images of this build and of the one it replaced, for a fast roll back; drop the rest.
rollback=""
if [ -s "$GROVE_STATE/previous.sha" ]; then
    rollback=$(tr -d '[:space:]' <"$GROVE_STATE/previous.sha")
fi
keep=" $sha $rollback "
docker image ls --format '{{.Repository}}:{{.Tag}}' | while read -r image; do
    case $image in
        grove-api:* | grove-runner:*)
            case $keep in
                *" ${image#*:} "*) ;;
                *) docker image rm "$image" >/dev/null || log "could not remove $image" ;;
            esac
            ;;
    esac
done
docker image prune --force >/dev/null
