#!/usr/bin/env bash
# Start the store, then its writers, where any is stopped. The backup unit runs this after every
# run, and grove-services.service at every boot, so a backup killed in its dump window or a host
# that went down in it never leaves the services down. A no-op when they run.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

maintenance_guard
take_lock 30
maintenance_guard
if [ ! -s "$GROVE_STATE/deployed.sha" ]; then
    log "nothing is deployed yet"
    exit 0
fi
# A one-off container of a killed backup (a dump, a fingerprint read) can outlive it and hold the
# store's lock. The box lock is held, so no live deploy or backup owns one now.
for id in $(docker ps --all --quiet --filter "label=com.docker.compose.project=$GROVE_PROJECT" \
    --filter "label=com.docker.compose.oneoff=True"); do
    # Cutover evidence is retained even after success. It is never a stale backup.
    name=$(docker inspect --format '{{.Name}}' "$id")
    case "$name" in /grove-migrate-*) continue ;; esac
    if docker rm --force --volumes "$id" >/dev/null; then
        log "removed a one-off container left by a killed run"
    else
        log "could not remove one-off container $id"
    fi
done

tries=${GROVE_SERVICES_TRIES:-300}
compose start neo4j || log "could not start neo4j yet"
wait_healthy neo4j "$tries" || log "neo4j is not healthy yet"

# compose refuses to start the writers while the store reports unhealthy, and the store can turn
# healthy after any try, so each try starts them again. The API's health check reads the store, so
# a healthy API means the database answers.
output=""
for _ in $(seq "$tries"); do
    maintenance_guard
    output=$(compose start api gateway runner 2>&1) || true
    # The runner has no health check, so it counts when it runs.
    if is_healthy api && is_healthy gateway &&
        [ -n "$(compose ps --status running --quiet runner 2>/dev/null || true)" ]; then
        log "the store and its writers run"
        exit 0
    fi
    sleep "${GROVE_POLL_SECONDS:-2}"
done
die "the services are not all healthy; compose said: $output"
