#!/usr/bin/env bash
# The second half of a deploy: deploy.sh runs this file from the commit it deploys, with the box
# lock held. Build the images, migrate, start the services, then run the smoke checks.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

[ $# -eq 2 ] || die "usage: apply.sh <sha> <ref>; run deploy.sh instead"
sha=$1
ref=$2
[ "${GROVE_LOCK_HELD:-}" = 1 ] || die "run deploy.sh, which takes the box lock"
[ "$(git -C "$GROVE_SRC" rev-parse HEAD)" = "$sha" ] || die "the checkout is not at $sha"
maintenance_guard

# Check the existing durable journal before any host-file or service mutation.
# This never initializes or repairs it; database equality is a separate release gate.
if ! python3 "$here/materials_preflight.py"; then
    # Reserved for this pre-mutation refusal. The parent must not run its installer fallback.
    exit 78
fi
# A later tool can also exit 78. Keep that a normal failure, never an early-refusal signal.
trap 'code=$?; if [ "$code" -eq 78 ]; then exit 1; fi' EXIT

# Gateway construction refuses absent configuration before it can answer health checks.
# Validate the files this deploy will consume, before any host-file or service mutation.
GROVE_ETC_DIR="$GROVE_ETC" "$here/runner-secrets.sh" --check

for file in neo4j.env api.env runner.env google-oauth.json; do
    [ -f "$GROVE_ETC/$file" ] || die "missing $GROVE_ETC/$file (docs/deploy.md, secret files)"
done
# The screening files may be absent: uploads then answer 503 and the rest serves (docs/deploy.md,
# uploads and the screening files). A present one is checked here, before any chown, because a
# chmod 400 later would hide a file that was placed open to group or others.
screening=$GROVE_ETC/screening
if [ -L "$screening" ] || { [ -e "$screening" ] && [ ! -d "$screening" ]; }; then
    die "the screening folder is not a plain folder"
fi
[ -d "$screening" ] || mkdir -m 700 "$screening"
absent=""
for name in rename.json denylist.txt; do
    path=$screening/$name
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        absent="$absent $name"
        continue
    fi
    # chown and chmod follow a link, so only a plain file goes to the API user.
    if [ -L "$path" ] || [ ! -f "$path" ]; then
        die "the screening file $name is not a plain file"
    fi
    open=$(find "$path" -maxdepth 0 \( -perm -0040 -o -perm -0020 -o -perm -0010 \
        -o -perm -0004 -o -perm -0002 -o -perm -0001 \))
    [ -z "$open" ] || die "the screening file $name is open to group or others: place it at 600"
done
# The API runs as uid 10001 and reads this one file through a read-only mount.
chown 10001:10001 "$GROVE_ETC/google-oauth.json"
chmod 400 "$GROVE_ETC/google-oauth.json"
# POSIX -perm forms only, one per group or other bit, so GNU and BSD find agree.
open_files=$(find "$GROVE_ETC" -maxdepth 1 -type f \( -perm -0040 -o -perm -0020 -o -perm -0010 \
    -o -perm -0004 -o -perm -0002 -o -perm -0001 \))
[ -z "$open_files" ] || die "a file in $GROVE_ETC is readable by group or others"
# The env files are root's, the user who runs this, and the folder is root's alone.
foreign_files=$(find "$GROVE_ETC" -maxdepth 1 -type f -name '*.env' ! -user "$(id -u)")
[ -z "$foreign_files" ] || die "an env file in $GROVE_ETC has another owner"
open_folder=$(find "$GROVE_ETC" -maxdepth 0 \( -perm -0040 -o -perm -0020 -o -perm -0010 \
    -o -perm -0004 -o -perm -0002 -o -perm -0001 \))
[ -z "$open_folder" ] || die "$GROVE_ETC is open to group or others"
# The API reads the screening folder through a read-only mount as uid 10001: its files at 400 and
# the folder at 500, so the API user alone can enter it. The check above stays one level deep;
# the screening files had their own check before the first chown.
for name in rename.json denylist.txt; do
    [ -f "$screening/$name" ] || continue
    chown 10001:10001 "$screening/$name"
    chmod 400 "$screening/$name"
done
chown 10001:10001 "$screening"
chmod 500 "$screening"
if [ -n "$absent" ]; then
    log "uploads answer 503 until both screening files are placed; absent:$absent"
fi

"$here/install-host.sh"

# setgid: every world folder and file the API makes here takes the runner's group (10002).
install -d -o 10001 -g 10002 -m 2750 /var/lib/grove-runner/worlds

export GROVE_BUILD_SHA=$sha
previous=$(served_sha)
record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: start"

export GROVE_CUTOVER_PUBLIC_BASE_URL
GROVE_CUTOVER_PUBLIC_BASE_URL=$(public_base_url)
# The protocol owns migration, target creation and smoke. Markers alone never
# authorize recovery. Its durable barrier also covers the old deploy parent.
baseline_args=()
if [ -n "${GROVE_CUTOVER_BASELINE_MANIFEST:-}${GROVE_CUTOVER_BASELINE_SHA256:-}" ]; then
    baseline_args=(--baseline-manifest "${GROVE_CUTOVER_BASELINE_MANIFEST:-}"
        --baseline-manifest-sha256 "${GROVE_CUTOVER_BASELINE_SHA256:-}")
fi
if python3 "$here/cutover.py" run "$sha" "$previous" "${baseline_args[@]}"; then
    record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: served, smoke ok"
else
    record deploy.log "deploy $sha ref=$ref previous=${previous:-none}: cutover FAILED; inspect maintenance state"
    exit 1
fi
# Retained images stay available. Compatibility, not a tag, authorizes recovery.
