#!/usr/bin/env bash
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
