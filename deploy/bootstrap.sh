#!/usr/bin/env bash
# One-time setup of a box, as root from the checkout: bootstrap.sh --base-url https://<host>
#
# Before it: Docker with the compose plugin, nginx and certbot installed, and this checkout
# cloned with the read-only deploy key (docs/deploy.md). It makes the folders, the runner user and
# the generated secret files, and leaves every existing file as it is. Copy the other secret
# files from the secrets map, then run deploy.sh.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

for tool in docker nginx certbot openssl; do
    command -v "$tool" >/dev/null || die "$tool is not installed (docs/deploy.md, first setup)"
done

[ $# -eq 2 ] && [ "$1" = --base-url ] || die "usage: bootstrap.sh --base-url https://<host>"
base=$2
[[ $base =~ ^https://[a-z0-9.-]+$ ]] || die "the base URL must be https://<host>, with no path"

# Refuse before any change: the host name is set once, and moved only by hand.
if [ -f "$GROVE_ETC/api.env" ] && [ "$(public_base_url)" != "$base" ]; then
    die "$GROVE_ETC/api.env names another base URL; change it by hand"
fi

runner_home=${GROVE_RUNNER_HOME:-/var/lib/grove-runner}
install -d -m 700 "$GROVE_ETC" "$GROVE_BACKUP_DIR"
install -d -m 755 "$GROVE_STATE"
install -d -m 750 "$GROVE_LOG_DIR"

# The runner's user on the host owns its HOME. It has no sudo and is not in the docker group.
# uid and gid 10002, as in the runner image, so the HOME it mounts is its own on both sides.
if ! getent group grove-runner >/dev/null; then
    groupadd --system --gid 10002 grove-runner
fi
if ! getent passwd grove-runner >/dev/null; then
    useradd --system --uid 10002 --gid 10002 --home-dir "$runner_home" --no-create-home \
        --shell /usr/sbin/nologin grove-runner
fi
install -d -o 10002 -g 10002 -m 700 "$runner_home"

umask 077
if [ ! -f "$GROVE_ETC/neo4j.env" ]; then
    # Generated here and never printed. Neo4j takes it as its initial password on first start.
    printf 'NEO4J_AUTH=neo4j/%s\n' "$(openssl rand -hex 24)" >"$GROVE_ETC/neo4j.env"
    log "wrote $GROVE_ETC/neo4j.env"
fi
if [ ! -f "$GROVE_ETC/api.env" ]; then
    printf 'GROVE_PUBLIC_BASE_URL=%s\n' "$base" >"$GROVE_ETC/api.env"
    log "wrote $GROVE_ETC/api.env"
fi
if [ ! -f "$GROVE_ETC/runner.env" ]; then
    printf '# The runner token only. deploy/runner-secrets.sh writes it.\n' >"$GROVE_ETC/runner.env"
    log "wrote $GROVE_ETC/runner.env (no token yet: run deploy/runner-secrets.sh)"
fi
umask 022

"$here/install-host.sh"
log "bootstrap done; copy the secret files, then run deploy.sh <sha>"
