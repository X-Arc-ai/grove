#!/usr/bin/env bash
# Run one command under the deploy lock of the operator's machine: with-deploy-lock.sh <command...>
#
# One deploy at a time across the lanes that may deploy: a mkdir lock with an owner file, and a
# detached releaser that frees the lock one cap (1 hour) after it was taken. The releaser and the
# normal release remove the lock only while it still holds their own token. A lock past its cap
# whose releaser is gone too (a restart of the machine, a kill of the whole process group) is taken
# over by the next caller.
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

owner_field() {
    sed -n "s/^$1=//p" "$2/owner" 2>/dev/null | head -n 1
}

refuse() {
    echo "the deploy lock is held: $(grep -v '^token=' "$lock/owner" 2>/dev/null | tr '\n' ' ')" >&2
    exit 75
}

# Take over a lock that is past its cap. The rename is atomic, and the token check makes sure the
# folder moved is the one that was judged stale; any other folder goes back.
take_over_stale() {
    local since old_token stale
    since=$(owner_field since_epoch "$lock")
    [[ $since =~ ^[0-9]+$ ]] || return 1
    [ $(($(date +%s) - since)) -ge "$cap" ] || return 1
    old_token=$(owner_field token "$lock")
    stale="$lock.stale.$$"
    mv "$lock" "$stale" 2>/dev/null || return 1
    if [ "$(owner_field token "$stale")" != "$old_token" ]; then
        mv "$stale" "$lock" 2>/dev/null || true
        return 1
    fi
    echo "took over a deploy lock past its cap: $(grep -v '^token=' "$stale/owner" | tr '\n' ' ')" >&2
    rm -rf "${stale:?}"
    mkdir "$lock" 2>/dev/null
}

mkdir -p "$(dirname "$lock")"
if ! mkdir "$lock" 2>/dev/null; then
    take_over_stale || refuse
fi
token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
printf 'token=%s\nholder=%s\npid=%s\nsince=%s\nsince_epoch=%s\n' "$token" \
    "${GROVE_LOCK_HOLDER:-unnamed}" "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(date +%s)" \
    >"$lock/owner"

release_if_mine() {
    if grep -qx "token=$1" "$2/owner" 2>/dev/null; then
        rm -f "$2/owner"
        rmdir "$2" 2>/dev/null || true
    fi
}

# The releaser runs in a process group of its own (job control on for this one start), so a kill
# of the holder's whole group leaves it alive. A TERM to it also ends its sleep, so a normal
# release leaves no process behind.
set -m
nohup bash -c "$(declare -f release_if_mine)"'
trap "kill \$nap 2>/dev/null; exit 0" TERM
sleep "$1" &
nap=$!
wait "$nap"
release_if_mine "$2" "$3"' releaser "$cap" "$token" "$lock" </dev/null >/dev/null 2>&1 &
releaser=$!
set +m

finish() {
    local status=$?
    release_if_mine "$token" "$lock"
    kill "$releaser" 2>/dev/null || true
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT TERM

"$@"
