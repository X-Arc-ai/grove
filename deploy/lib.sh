# shellcheck shell=bash
# What the box scripts share. Source it; never run it. Every path can be moved by its variable,
# which is how the tests run these scripts against a scratch tree with fake commands.

GROVE_SRC=${GROVE_SRC:-/opt/grove/src}
GROVE_ETC=${GROVE_ETC:-/etc/grove}
GROVE_STATE=${GROVE_STATE:-/var/lib/grove}
GROVE_LOG_DIR=${GROVE_LOG_DIR:-/var/log/grove}
GROVE_BACKUP_DIR=${GROVE_BACKUP_DIR:-/var/backups/grove}
# In a folder only root can write: anyone who can open the lock file can hold it.
GROVE_LOCK_FILE=${GROVE_LOCK_FILE:-/run/grove/deploy.lock}
GROVE_NGINX_DIR=${GROVE_NGINX_DIR:-/etc/nginx}
GROVE_SYSTEMD_DIR=${GROVE_SYSTEMD_DIR:-/etc/systemd/system}
GROVE_LETSENCRYPT_DIR=${GROVE_LETSENCRYPT_DIR:-/etc/letsencrypt}
GROVE_ACME_ROOT=${GROVE_ACME_ROOT:-/var/www/grove-acme}
GROVE_PROJECT=grove

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
    log "error: $*" >&2
    exit 1
}

# The sha the box runs, or nothing before the first deploy.
served_sha() {
    if [ -s "$GROVE_STATE/deployed.sha" ]; then
        tr -d '[:space:]' <"$GROVE_STATE/deployed.sha"
    fi
}

# The deployed sha names the images, so every compose call needs it, a stop as much as a build.
deployed_sha() {
    [ -s "$GROVE_STATE/deployed.sha" ] || die "no deployed sha in $GROVE_STATE: deploy first"
    served_sha
}

compose() {
    local overlay=()
    if [ -e "$GROVE_STATE/cutover.json" ] || [ -L "$GROVE_STATE/cutover.json" ]; then
        [ -f "$GROVE_STATE/cutover-compose.json" ] || die "maintenance override is absent"
        overlay=(--file "$GROVE_STATE/cutover-compose.json")
    fi
    GROVE_BUILD_SHA=${GROVE_BUILD_SHA:-$(deployed_sha)} docker compose \
        --project-name "$GROVE_PROJECT" --file "$GROVE_SRC/deploy/compose.yml" "${overlay[@]}" "$@"
}

maintenance_guard() {
    if [ -e "$GROVE_STATE/cutover.json" ] || [ -L "$GROVE_STATE/cutover.json" ]; then
        die "cutover maintenance is active; explicit recovery is required"
    fi
}

# `compose run` keeps stdin open, so a script fed on stdin (ssh host 'bash -s' <<EOF) would lose
# the rest of itself to the container. Every one-off container reads /dev/null instead.
compose_run() {
    compose run --rm --no-deps -T "$@" </dev/null
}

# One deploy or backup at a time on the box. The lock lives on fd 9, so child processes keep it.
take_lock() {
    local wait_seconds=$1
    install -d -m 700 "$(dirname "$GROVE_LOCK_FILE")"
    exec 9>"$GROVE_LOCK_FILE"
    flock -w "$wait_seconds" 9 || die "another deploy or backup holds $GROVE_LOCK_FILE"
    export GROVE_LOCK_HELD=1
}

# The public origin, from the one file that names it.
public_base_url() {
    local value
    value=$(sed -n 's/^GROVE_PUBLIC_BASE_URL=//p' "$GROVE_ETC/api.env" | tail -n 1)
    [[ $value =~ ^https://[a-z0-9.-]+$ ]] || die "GROVE_PUBLIC_BASE_URL in $GROVE_ETC/api.env must be https://<host>"
    printf '%s\n' "$value"
}

public_host() {
    local base
    # exit, not die: set -e is off inside a command substitution, and an empty host must not pass.
    base=$(public_base_url) || exit 1
    printf '%s\n' "${base#https://}"
}

neo4j_image() {
    local image
    image=$(compose config --images | grep -E '^neo4j:' | head -n 1)
    [ -n "$image" ] || die "no neo4j image in the compose file"
    printf '%s\n' "$image"
}

is_healthy() {
    local id state
    id=$(compose ps --quiet "$1" 2>/dev/null || true)
    [ -n "$id" ] || return 1
    state=$(docker inspect --format '{{.State.Health.Status}}' "$id" 2>/dev/null || true)
    [ "$state" = healthy ]
}

# Returns 1, never exits, so an EXIT trap can call it.
wait_healthy() {
    local service=$1 tries=${2:-90}
    for _ in $(seq "$tries"); do
        is_healthy "$service" && return 0
        sleep "${GROVE_POLL_SECONDS:-2}"
    done
    log "error: $service is not healthy" >&2
    return 1
}

# A state file, written whole: an empty deployed.sha would read as nothing deployed.
write_state() {
    mkdir -p "$GROVE_STATE"
    printf '%s\n' "$2" >"$GROVE_STATE/$1.tmp"
    mv "$GROVE_STATE/$1.tmp" "$GROVE_STATE/$1"
}

# A log line that also goes to one of the box's log files.
record() {
    local file=$1
    shift
    mkdir -p "$GROVE_LOG_DIR"
    [ -f "$GROVE_LOG_DIR/$file" ] || install -m 640 /dev/null "$GROVE_LOG_DIR/$file"
    log "$*" | tee -a "$GROVE_LOG_DIR/$file"
}
