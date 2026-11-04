#!/usr/bin/env bash
# Deploy one named commit on the box: deploy.sh <full sha> [--ref <branch>]
#
# The commit must be on origin/<branch>: develop, unless --ref names another branch and
# GROVE_ALLOW_BRANCH_DEPLOY=1 says that is meant (a try on the box before a merge). The checkout pulls
# from GitHub with the box's read-only deploy key; nothing is pushed to the box. This script takes
# the box lock, moves the checkout, and runs that commit's own apply.sh. If apply.sh fails before
# the new build serves, the checkout goes back to the sha the box serves, so the nightly backup
# never runs new scripts against old services.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

usage() {
    echo "usage: deploy.sh <40-character sha> [--ref <branch>]" >&2
    exit 2
}

[ $# -ge 1 ] || usage
sha=$1
shift
ref=develop
while [ $# -gt 0 ]; do
    case $1 in
        --ref)
            [ $# -ge 2 ] || usage
            ref=$2
            shift 2
            ;;
        *) usage ;;
    esac
done

[[ $sha =~ ^[0-9a-f]{40}$ ]] || die "give the full 40-character sha"
[[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "not a branch name: $ref"
# Another branch's apply.sh runs as root here, before any review of it.
if [ "$ref" != develop ] && [ "${GROVE_ALLOW_BRANCH_DEPLOY:-}" != 1 ]; then
    die "only develop deploys; set GROVE_ALLOW_BRANCH_DEPLOY=1 to try $ref on the box"
fi

# Longer than services-up.sh can hold the lock, so a fix or a roll back gets through.
maintenance_guard
take_lock 30
maintenance_guard

git -C "$GROVE_SRC" fetch --quiet --prune origin "+refs/heads/$ref:refs/remotes/origin/$ref"
git -C "$GROVE_SRC" merge-base --is-ancestor "$sha" "origin/$ref" ||
    die "$sha is not on origin/$ref"
# --ignored too: a gitignored file in server/ would reach the image with no commit that holds it.
[ -z "$(git -C "$GROVE_SRC" status --porcelain --ignored)" ] ||
    die "the checkout in $GROVE_SRC has local changes or ignored files"
git -C "$GROVE_SRC" checkout --quiet --detach "$sha"

# bash keeps reading this file from the inode it opened, so the checkout cannot change the lines
# still to run.
status=0
"$GROVE_SRC/deploy/apply.sh" "$sha" "$ref" || status=$?
if [ "$status" -eq 78 ]; then
    log "the journal prerequisite refused before apply mutation; no installer fallback"
    exit "$status"
fi
if [ "$status" != 0 ]; then
    # The old parent's fallback may still run on the first upgrade. Installed
    # drop-ins outlive it; this parent also avoids replacing the target source.
    maintenance_guard
    served=$(served_sha)
    if [ -n "$served" ] && [ "$served" != "$sha" ]; then
        git -C "$GROVE_SRC" checkout --quiet --detach "$served"
        # apply.sh may have installed the failed commit's sites and units; the served commit's go
        # back, so no unit names a script this checkout does not hold.
        if "$GROVE_SRC/deploy/install-host.sh" </dev/null; then
            log "the deploy of $sha failed before it served; the checkout and host files are back at $served"
        else
            log "the deploy of $sha failed before it served; the checkout is back at $served, but its host files did not install: run deploy/install-host.sh"
        fi
    fi
fi
exit "$status"
