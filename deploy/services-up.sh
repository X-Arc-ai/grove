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
    docker rm --force --volumes "$id" >/dev/null
    log "removed a one-off container left by a killed run"
done
compose start neo4j
wait_healthy neo4j || die "neo4j is not healthy"
compose start api runner
# The API's health check reads the store, so a healthy API means the database answers.
wait_healthy api || die "the API is not healthy: the store does not answer"
