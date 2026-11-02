#!/usr/bin/env bash
# Start the store, then its writers, where any is stopped. The backup unit runs this after every
# run, and grove-services.service at every boot, so a backup killed in its dump window or a host
# that went down in it never leaves the services down. A no-op when they run.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

take_lock "${GROVE_SERVICES_WAIT_SECONDS:-600}"
if [ ! -s "$GROVE_STATE/deployed.sha" ]; then
    log "nothing is deployed yet"
    exit 0
fi
# A one-off container of a killed backup (a dump, a fingerprint read) can outlive it and hold the
# store's lock. The box lock is held, so no live deploy or backup owns one now.
for id in $(docker ps --all --quiet --filter "label=com.docker.compose.project=$GROVE_PROJECT" \
    --filter "label=com.docker.compose.oneoff=True"); do
    if docker rm --force --volumes "$id" >/dev/null; then
        log "removed a one-off container left by a killed run"
    else
        log "could not remove one-off container $id"
    fi
done

# Each step runs whatever the last one did: this script is the last thing that starts them.
failed=0
compose start neo4j || failed=1
wait_healthy neo4j "${GROVE_SERVICES_TRIES:-300}" || failed=1
compose start api runner || failed=1
# The API's health check reads the store, so a healthy API means the database answers.
wait_healthy api "${GROVE_SERVICES_TRIES:-300}" || failed=1
[ "$failed" = 0 ] || die "the services are not all healthy"
