#!/usr/bin/env bash
# The runner's secrets, made on the box as root: deploy/runner-secrets.sh
#
# The deploy lane runs it once, before the first deploy that carries the runner loop
# (docs/deploy.md). It can run again at any time: it keeps every value that is there, and it prints
# file names only, never a value. In GROVE_ETC_DIR (default the box's /etc/grove):
#   gateway.env      GROVE_GATEWAY_SECRET, which signs the gateway run tokens and the pass tokens
#   runner.env       GROVE_RUNNER_TOKEN, the runner service's one credential
#   runner-auth.env  GROVE_RUNNER_TOKEN_SHA256, the API's copy: the token's sha256 only. Each run
#                    computes it again from runner.env, so the two never drift.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

etc=${GROVE_ETC_DIR:-$GROVE_ETC}
[ -d "$etc" ] || die "$etc does not exist; run deploy/bootstrap.sh first"
command -v python3 >/dev/null || die "python3 is not installed"
umask 077

# A new value goes from python3 straight into a temporary file in the same folder, which then
# replaces the file whole. No value is on a command line, in the environment or on the screen.
# Exit 0: the new file is on stdout. Exit 10: the file keeps its value. Exit 3: the value there is
# too short or not one plain word, so the file stays as it is and the run stops.
# read -d '' ends at the end of the text with status 1, hence the || true.
IFS= read -r -d '' add_value <<'PY' || true
import os, secrets, sys

path, name, kind, comment = sys.argv[1:5]
lines = []
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
prefix = name + "="
values = [line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
if values and values[-1]:
    value = values[-1]
    plain = value.isascii() and value.isprintable() and not set(value) & set(" \"'#")
    sys.exit(10 if plain and len(value) >= 32 else 3)
value = secrets.token_hex(32) if kind == "hex" else secrets.token_urlsafe(32)
kept = [line for line in lines if not line.startswith(prefix)] or [comment]
sys.stdout.write("\n".join([*kept, prefix + value]) + "\n")
PY

IFS= read -r -d '' token_hash <<'PY' || true
import hashlib, os, sys

source, path = sys.argv[1:3]
with open(source, encoding="utf-8") as handle:
    lines = handle.read().splitlines()
prefix = "GROVE_RUNNER_TOKEN="
values = [line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
if not values or not values[-1]:
    sys.exit(3)
digest = hashlib.sha256(values[-1].encode("utf-8")).hexdigest()
text = (
    "# The sha256 of the runner token in runner.env. deploy/runner-secrets.sh writes it.\n"
    f"GROVE_RUNNER_TOKEN_SHA256={digest}\n"
)
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        if handle.read() == text:
            sys.exit(10)
sys.stdout.write(text)
PY

tmp=""
trap '[ -z "$tmp" ] || rm -f -- "$tmp"' EXIT

# put <file> <refusal> <program> <args>: run the program with its stdout in a temporary file, then
# put that file in the place of <file>, or keep <file> as it is.
put() {
    local file=$1 refusal=$2 program=$3 status=0
    shift 3
    tmp=$(mktemp "$etc/.$file.XXXXXX")
    python3 -I -c "$program" "$@" >"$tmp" || status=$?
    case $status in
        0)
            chmod 600 "$tmp"
            mv -f -- "$tmp" "$etc/$file"
            tmp=""
            log "$file created"
            ;;
        10)
            rm -f -- "$tmp"
            tmp=""
            chmod 600 "$etc/$file"
            log "$file kept"
            ;;
        3) die "$file: $refusal" ;;
        *) die "$file: could not write it (exit $status)" ;;
    esac
}

put gateway.env \
    "its GROVE_GATEWAY_SECRET is under 32 characters or not one plain word; remove that line" \
    "$add_value" "$etc/gateway.env" GROVE_GATEWAY_SECRET hex \
    "# Signs the gateway run tokens and the pass tokens. deploy/runner-secrets.sh writes it."
put runner.env \
    "its GROVE_RUNNER_TOKEN is under 32 characters or not one plain word; remove that line" \
    "$add_value" "$etc/runner.env" GROVE_RUNNER_TOKEN urlsafe \
    "# The runner token only. deploy/runner-secrets.sh writes it."
put runner-auth.env "runner.env holds no GROVE_RUNNER_TOKEN" \
    "$token_hash" "$etc/runner.env" "$etc/runner-auth.env"
