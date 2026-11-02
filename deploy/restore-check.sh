#!/usr/bin/env bash
# Load one dump into a scratch Neo4j with no network beyond its own, and compare the fingerprint
# of what loaded with the one recorded before the dump: restore-check.sh <dump file>
#
# It never touches the live store. The scratch container, volume and network go at the end.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

[ $# -eq 1 ] || die "usage: restore-check.sh <dump file>"
dump=$1
sidecar="${dump%.dump}.fingerprint.json"
[ -s "$dump" ] || die "no dump at $dump"
[ -s "$sidecar" ] || die "no recorded fingerprint at $sidecar"
if [ -f "$dump.sha256" ]; then
    (cd "$(dirname "$dump")" && sha256sum --check --quiet "$(basename "$dump").sha256") ||
        die "$dump does not match its checksum"
fi
expected=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "$sidecar")
[[ $expected =~ ^[0-9a-f]{64}$ ]] || die "the recorded fingerprint is not a sha256"

image=$(neo4j_image)
checker="grove-api:$(deployed_sha)"
tag="grove-restorecheck-$(date -u +%Y%m%dT%H%M%SZ)-$$"

cleanup() {
    docker rm --force "$tag" >/dev/null 2>&1 || true
    docker volume rm "$tag" >/dev/null 2>&1 || true
    docker network rm "$tag" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create --internal "$tag" >/dev/null
docker volume create "$tag" >/dev/null
docker run --rm --interactive --network none --volume "$tag:/data" "$image" \
    neo4j-admin database load neo4j --from-stdin --overwrite-destination=true <"$dump"
docker run --detach --name "$tag" --network "$tag" --volume "$tag:/data" \
    --env NEO4J_AUTH=none \
    --env NEO4J_server_memory_heap_initial__size=512m \
    --env NEO4J_server_memory_heap_max__size=512m \
    --env NEO4J_server_memory_pagecache_size=256m \
    --env NEO4J_db_query_default__language=CYPHER_25 \
    "$image" >/dev/null

ready=0
for _ in $(seq "${GROVE_RESTORE_TRIES:-90}"); do
    if docker exec "$tag" wget -q -O /dev/null http://localhost:7474 >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep "${GROVE_POLL_SECONDS:-2}"
done
[ "$ready" = 1 ] || die "the scratch store did not start"

set +e
result=$(docker run --rm --network "$tag" --env "GROVE_NEO4J_URI=bolt://$tag:7687" \
    --env NEO4J_AUTH=none "$checker" python -m grove.store.dump_fingerprint --expect "$expected")
status=$?
set -e
case $status in
    0) record backup.log "restore check $(basename "$dump"): fingerprint $expected matches" ;;
    3)
        record backup.log "restore check $(basename "$dump"): MISMATCH, the loaded store has another fingerprint: $result"
        exit 3
        ;;
    *) die "restore check $(basename "$dump"): the fingerprint read failed (exit $status)" ;;
esac
