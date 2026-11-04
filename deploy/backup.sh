#!/usr/bin/env bash
# The daily offline backup (A.5; Neo4j Community has no online backup): stop the runner, the
# gateway and the API, record the dump fingerprint, stop Neo4j, dump, start everything again, load
# the new dump into a scratch store and compare its fingerprint, then remove dumps older than 30
# days.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

keep_days=${GROVE_BACKUP_KEEP_DAYS:-30}
[[ $keep_days =~ ^[1-9][0-9]*$ ]] || die "GROVE_BACKUP_KEEP_DAYS must be a whole number of days"

maintenance_guard
take_lock 30
maintenance_guard
GROVE_BUILD_SHA=$(deployed_sha)
export GROVE_BUILD_SHA

stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="grove-$stamp"
work="$GROVE_BACKUP_DIR/.work-$stamp"
services_down=0

# Whatever failed, the store and its writers run again, in this order. A stop from systemd
# (TERM) also comes through here; a KILL does not, and the unit's ExecStopPost covers that.
finish() {
    local status=$?
    if [ "$services_down" = 1 ]; then
        log "starting neo4j, api, gateway and runner again"
        compose start neo4j || true
        wait_healthy neo4j || true
        if [ ! -e "$GROVE_STATE/cutover.json" ] && [ ! -L "$GROVE_STATE/cutover.json" ]; then
            compose start api gateway runner || true
        fi
    fi
    rm -rf "${work:?}"
    [ "$status" = 0 ] || record backup.log "backup $name FAILED (exit $status)"
    exit "$status"
}
trap finish EXIT
trap 'exit 143' TERM INT

fingerprint_of() {
    python3 -c 'import json, sys; print(json.loads(sys.argv[1])["fingerprint"])' "$1"
}

install -d -m 700 "$GROVE_BACKUP_DIR"
# A backup killed outright leaves its work folder with a partial dump. The lock is held, so no
# other backup owns one now.
find "$GROVE_BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '.work-*' -exec rm -rf {} +
log "backup $name: stopping the runner, the gateway and the API"
services_down=1
# Every writer of the store stops before the first fingerprint. The gateway writes a model_calls
# row for each call, and a write between the two fingerprints fails the backup with 'the store
# changed across the dump'.
compose stop runner gateway api
# A catch-up run after a boot can come before the store is up, or after a backup left it stopped.
compose start neo4j
wait_healthy neo4j "${GROVE_SERVICES_TRIES:-300}" || die "neo4j is not healthy, so no dump"
before=$(compose_run api python -m grove.store.dump_fingerprint)
fingerprint=$(fingerprint_of "$before")

log "backup $name: stopping neo4j and dumping"
compose stop neo4j
install -d -o 7474 -g 7474 -m 700 "$work"
compose_run --volume "$work:/backups" neo4j neo4j-admin database dump neo4j --to-path=/backups
[ -s "$work/neo4j.dump" ] || die "the dump wrote no file"

compose start neo4j
wait_healthy neo4j || die "neo4j did not come back after the dump"
# The store must be what was fingerprinted: nothing wrote while the services were down.
compose_run api python -m grove.store.dump_fingerprint --expect "$fingerprint" >/dev/null ||
    die "the store changed across the dump"
maintenance_guard
compose start api gateway runner
services_down=0
log "backup $name: services running again"

mv "$work/neo4j.dump" "$GROVE_BACKUP_DIR/$name.dump"
chown root:root "$GROVE_BACKUP_DIR/$name.dump"
chmod 600 "$GROVE_BACKUP_DIR/$name.dump"
(umask 077 && printf '%s\n' "$before" >"$GROVE_BACKUP_DIR/$name.fingerprint.json")
(cd "$GROVE_BACKUP_DIR" && umask 077 && sha256sum "$name.dump" >"$name.dump.sha256")
record backup.log "backup $name: dumped, fingerprint $fingerprint"

# The new dump must load and match before any old dump goes: a failed check keeps them all.
"$here/restore-check.sh" "$GROVE_BACKUP_DIR/$name.dump" </dev/null

# Keep every dump younger than keep_days. The new dump is the youngest, so it always stays.
find "$GROVE_BACKUP_DIR" -maxdepth 1 -type f -name 'grove-*.dump' -mtime +"$((keep_days - 1))" |
    sort | while read -r old; do
    rm -f -- "$old" "${old%.dump}.fingerprint.json" "$old.sha256"
    record backup.log "backup: removed $(basename "$old"), older than $keep_days days"
done
