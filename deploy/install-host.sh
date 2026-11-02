#!/usr/bin/env bash
# Install the host files of this checkout: the nginx sites, the certificate on first use, the
# backup units, the certbot hook and log rotation. Safe to run again; apply.sh runs it each deploy.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$here/lib.sh"

host=$(public_host)
sites="$GROVE_NGINX_DIR/sites-available"
enabled="$GROVE_NGINX_DIR/sites-enabled"
saved=$(mktemp -d)
trap 'rm -rf "${saved:?}"' EXIT

# Keep the sites as they were, so a site that nginx refuses never stays live.
for name in grove-http.conf grove-https.conf; do
    if [ -f "$sites/$name" ]; then cp -p "$sites/$name" "$saved/$name"; fi
    if [ -L "$enabled/$name" ]; then touch "$saved/$name.enabled"; fi
done

restore_sites() {
    local name
    for name in grove-http.conf grove-https.conf; do
        rm -f "$sites/$name" "$enabled/$name"
        if [ -f "$saved/$name" ]; then cp -p "$saved/$name" "$sites/$name"; fi
        if [ -f "$saved/$name.enabled" ]; then ln -sfn "$sites/$name" "$enabled/$name"; fi
    done
    return 0
}

render() {
    sed "s/__GROVE_HOST__/$host/g" "$here/nginx/$1.template" >"$sites/$1.tmp"
    mv "$sites/$1.tmp" "$sites/$1"
    ln -sfn "$sites/$1" "$enabled/$1"
}

checked_reload() {
    if ! nginx -t; then
        restore_sites
        die "nginx refused the new sites; the old ones are back"
    fi
    systemctl reload nginx
}

mkdir -p "$sites" "$enabled" "$GROVE_ACME_ROOT"
render grove-http.conf

if [ ! -f "$GROVE_LETSENCRYPT_DIR/live/$host/fullchain.pem" ]; then
    rm -f "$enabled/grove-https.conf"
    checked_reload
    log "requesting a certificate for $host"
    if [ -n "${GROVE_ACME_EMAIL:-}" ]; then
        account=(--email "$GROVE_ACME_EMAIL")
    else
        account=(--register-unsafely-without-email)
    fi
    certbot certonly --webroot --webroot-path "$GROVE_ACME_ROOT" --domain "$host" \
        --non-interactive --agree-tos --keep-until-expiring "${account[@]}"
fi
render grove-https.conf

install -d "$GROVE_LETSENCRYPT_DIR/renewal-hooks/deploy"
install -m 755 "$here/certbot-reload-nginx.sh" \
    "$GROVE_LETSENCRYPT_DIR/renewal-hooks/deploy/grove-reload-nginx.sh"
install -m 644 "$here/logrotate.conf" "${GROVE_LOGROTATE_DIR:-/etc/logrotate.d}/grove"
for unit in grove-backup.service grove-backup.timer; do
    install -m 644 "$here/systemd/$unit" "$GROVE_SYSTEMD_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now grove-backup.timer

checked_reload
