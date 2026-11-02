#!/usr/bin/env bash
# Run one command under the deploy lock of the operator's machine: with-deploy-lock.sh <command...>
#
# One deploy at a time across the lanes that may deploy: a mkdir lock with an owner file, and a
# detached releaser that frees the lock after a cap (1 hour) if its holder died. The releaser and
# the normal release remove the lock only while it still holds their own token.
set -euo pipefail

[ $# -ge 1 ] || {
    echo "usage: with-deploy-lock.sh <command...>" >&2
    exit 2
}
lock="${GROVE_LOCK_DIR:-$HOME/.grove/locks}/deploy.lock.d"
cap=${GROVE_LOCK_CAP_SECONDS:-3600}
[[ $cap =~ ^[1-9][0-9]*$ ]] || {
    echo "GROVE_LOCK_CAP_SECONDS must be a whole number" >&2
    exit 2
}

mkdir -p "$(dirname "$lock")"
if ! mkdir "$lock" 2>/dev/null; then
    echo "the deploy lock is held: $(grep -v '^token=' "$lock/owner" 2>/dev/null | tr '\n' ' ')" >&2
    exit 75
fi
token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
printf 'token=%s\nholder=%s\npid=%s\nsince=%s\n' "$token" "${GROVE_LOCK_HOLDER:-unnamed}" "$$" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$lock/owner"

release_if_mine() {
    if grep -qx "token=$1" "$2/owner" 2>/dev/null; then
        rm -f "$2/owner"
        rmdir "$2" 2>/dev/null || true
    fi
}

# The releaser outlives this shell, so a killed holder still frees the lock after the cap.
# A TERM to the releaser also ends its sleep, so a normal release leaves no process behind.
nohup bash -c "$(declare -f release_if_mine)"'
trap "kill \$nap 2>/dev/null; exit 0" TERM
sleep "$1" &
nap=$!
wait "$nap"
release_if_mine "$2" "$3"' releaser "$cap" "$token" "$lock" </dev/null >/dev/null 2>&1 &
releaser=$!

finish() {
    local status=$?
    release_if_mine "$token" "$lock"
    kill "$releaser" 2>/dev/null || true
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT TERM

"$@"
