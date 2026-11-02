#!/usr/bin/env bash
# Start the store, then its writers, where any is stopped. The backup unit runs this after every
# run, so a backup killed in its dump window never leaves the services down. A no-op when they run.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

take_lock "${GROVE_SERVICES_WAIT_SECONDS:-600}"
if [ ! -s "$GROVE_STATE/deployed.sha" ]; then
    log "nothing is deployed yet"
    exit 0
fi
compose start neo4j
wait_healthy neo4j || die "neo4j is not healthy"
compose start api runner
