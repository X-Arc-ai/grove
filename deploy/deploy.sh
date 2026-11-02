#!/usr/bin/env bash
# Deploy one named commit on the box: deploy.sh <full sha> [--ref <branch>]
#
# The commit must be on origin/<branch>: develop, unless --ref names another branch and
# GROVE_ALLOW_BRANCH_DEPLOY=1 says that is meant (a try on the box before a merge). The checkout pulls
# from GitHub with the box's read-only deploy key; nothing is pushed to the box. This script takes
# the box lock, moves the checkout, and hands over to that commit's own apply.sh.
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

take_lock "${GROVE_DEPLOY_WAIT_SECONDS:-600}"

git -C "$GROVE_SRC" fetch --quiet --prune origin "+refs/heads/$ref:refs/remotes/origin/$ref"
git -C "$GROVE_SRC" merge-base --is-ancestor "$sha" "origin/$ref" ||
    die "$sha is not on origin/$ref"
[ -z "$(git -C "$GROVE_SRC" status --porcelain)" ] || die "the checkout in $GROVE_SRC has local changes"
git -C "$GROVE_SRC" checkout --quiet --detach "$sha"

exec "$GROVE_SRC/deploy/apply.sh" "$sha" "$ref"
