"""First-upgrade controls execute the preserved baseline parent and installer.

The baseline strings are repository source from cac4b95, not a simulated new parent.
Command doubles isolate all host actions. No real systemd or Docker operation runs.
"""

# Preserve the baseline shell source verbatim, including its original long lines.
# ruff: noqa: E501

import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from tests.deploy.conftest import OTHER_SHA, SHA

BASELINE_PARENT = r"""#!/usr/bin/env bash
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
take_lock "${GROVE_DEPLOY_WAIT_SECONDS:-1800}"

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
if [ "$status" != 0 ]; then
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
"""

BASELINE_INSTALLER = r"""#!/usr/bin/env bash
# Install the host files of this checkout: the nginx sites, the certificate on first use, the
# backup units, the certbot hook and log rotation. Safe to run again; apply.sh runs it each deploy.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

command -v nginx >/dev/null || die "nginx is not installed"
host=$(public_host)
sites="$GROVE_NGINX_DIR/sites-available"
enabled="$GROVE_NGINX_DIR/sites-enabled"
certificate="$GROVE_LETSENCRYPT_DIR/live/$host/fullchain.pem"
saved=$(mktemp -d)
restored=0
reloaded=0
checked=0

# Keep the sites as they were, so a site that nginx never accepted never stays on disk.
for name in grove-http.conf grove-https.conf; do
    if [ -f "$sites/$name" ]; then cp -p "$sites/$name" "$saved/$name"; fi
    if [ -L "$enabled/$name" ]; then touch "$saved/$name.enabled"; fi
done

restore_sites() {
    local name
    [ "$restored" = 0 ] || return 0
    restored=1
    for name in grove-http.conf grove-https.conf; do
        rm -f "$sites/$name" "$enabled/$name"
        if [ -f "$saved/$name" ]; then cp -p "$saved/$name" "$sites/$name"; fi
        if [ -f "$saved/$name.enabled" ]; then ln -sfn "$sites/$name" "$enabled/$name"; fi
    done
    # No TLS site may name a certificate that does not exist: nginx would refuse to start.
    if [ ! -f "$certificate" ]; then rm -f "$enabled/grove-https.conf"; fi
}

# Any failure before the last check, in any step, puts the old sites back.
finish() {
    local status=$?
    if [ "$status" != 0 ] && [ "$checked" = 0 ]; then
        restore_sites
        # A reload earlier in this run made the new sites live, so the old ones must go live too.
        if [ "$reloaded" = 1 ] && nginx -t; then systemctl reload-or-restart nginx || true; fi
    fi
    rm -rf "${saved:?}"
    exit "$status"
}
trap finish EXIT

render() {
    sed "s/__GROVE_HOST__/$host/g" "$here/nginx/$1.template" >"$sites/$1.tmp"
    mv "$sites/$1.tmp" "$sites/$1"
    ln -sfn "$sites/$1" "$enabled/$1"
}

checked_reload() {
    if ! nginx -t; then
        die "nginx refused the new sites; the old ones are back"
    fi
    # Also starts nginx if it is not running.
    systemctl reload-or-restart nginx
    reloaded=1
}

mkdir -p "$sites" "$enabled" "$GROVE_ACME_ROOT"
render grove-http.conf

if [ ! -f "$certificate" ]; then
    command -v certbot >/dev/null || die "certbot is not installed"
    rm -f "$enabled/grove-https.conf"
    checked_reload
    log "requesting a certificate for $host"
    if [ -n "${GROVE_ACME_EMAIL:-}" ]; then
        account=(--email "$GROVE_ACME_EMAIL")
    else
        account=(--register-unsafely-without-email)
    fi
    if ! certbot certonly --webroot --webroot-path "$GROVE_ACME_ROOT" --domain "$host" \
        --non-interactive --agree-tos --keep-until-expiring "${account[@]}"; then
        die "certbot got no certificate for $host; no TLS site is enabled"
    fi
fi
render grove-https.conf

install -d "$GROVE_LETSENCRYPT_DIR/renewal-hooks/deploy"
install -m 755 "$here/certbot-reload-nginx.sh" \
    "$GROVE_LETSENCRYPT_DIR/renewal-hooks/deploy/grove-reload-nginx.sh"
install -m 644 "$here/logrotate.conf" "${GROVE_LOGROTATE_DIR:-/etc/logrotate.d}/grove"
for unit in grove-backup.service grove-backup.timer grove-services.service; do
    install -m 644 "$here/systemd/$unit" "$GROVE_SYSTEMD_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now grove-backup.timer
# Enabled for the next boot only: started now, it would wait for the lock this deploy holds.
systemctl enable grove-services.service

checked_reload
checked=1
"""


def test_cutover_first_upgrade_old_parent_fallback_keeps_installed_guard(box):
    installed = box.root / "installed/cutover.py"
    box.env["GROVE_CUTOVER_INSTALLED"] = str(installed)
    result = box.run("install-host.sh")
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    before = {p: p.read_bytes() for p in (box.root / "systemd").glob("*.service.d/90-cutover.conf")}
    assert len(before) == 2
    revisions = box.root / "revisions"
    old = revisions / OTHER_SHA / "deploy"
    new = revisions / SHA / "deploy"
    shutil.copytree(box.src / "deploy", old)
    shutil.copytree(box.src / "deploy", new)
    (old / "deploy.sh").write_text(BASELINE_PARENT)
    (old / "install-host.sh").write_text(BASELINE_INSTALLER)
    # The real old shell retains its inode while fake git replaces the checkout.
    (box.src / "deploy/deploy.sh").write_text(BASELINE_PARENT)
    (box.src / "deploy/install-host.sh").write_text(BASELINE_INSTALLER)
    (new / "apply.sh").write_text(
        '#!/bin/bash\nset -eu\nprintf \'{"stage":"migration_started"}\\n\' > "$GROVE_STATE/cutover.json"\nexit 42\n'
    )
    box.env["FAKE_REVISIONS"] = str(revisions)
    box.deployed(OTHER_SHA)
    (box.root / "fake/HEAD").write_text(OTHER_SHA)
    result = box.run("deploy.sh", SHA)
    assert result.returncode == 42
    assert (box.root / "fake/HEAD").read_text() == OTHER_SHA
    assert (box.src / "deploy/install-host.sh").read_text() == BASELINE_INSTALLER
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest
    assert all(path.read_bytes() == content for path, content in before.items())
    # Both ExecStart and ExecStopPost use the surviving installed guard.
    backup = (box.root / "systemd/grove-backup.service.d/90-cutover.conf").read_text()
    assert "ExecStopPost=" in backup and "recover services-up" in backup
    for action in ("backup", "services-up"):
        prior = box.calls()
        guarded = subprocess.run(
            [sys.executable, str(installed), "recover", action], env=box.env, capture_output=True
        )
        assert guarded.returncode == 1
        assert box.calls() == prior  # No cleanup, Docker or restart attempt.
    assert json.loads((box.state / "cutover.json").read_text())["stage"] == "migration_started"


@pytest.mark.parametrize("script", ["services-up.sh", "backup.sh", "deploy.sh"])
def test_cutover_direct_current_scripts_refuse_before_any_mutation(box, script):
    (box.state / "cutover.json").write_text("{incomplete")
    result = box.run(script, *([SHA] if script == "deploy.sh" else []))
    assert result.returncode != 0
    assert not any(call.startswith(("docker ", "git ")) for call in box.calls())


def test_cutover_installed_guard_remains_when_checkout_is_absent(box):
    installed = box.root / "installed/cutover.py"
    box.env["GROVE_CUTOVER_INSTALLED"] = str(installed)
    assert box.run("install-host.sh").returncode == 0
    (box.state / "cutover.json").write_text("{}")
    shutil.rmtree(box.src)
    result = subprocess.run(
        [sys.executable, str(installed), "recover", "services-up"], env=box.env, capture_output=True
    )
    assert result.returncode == 1
