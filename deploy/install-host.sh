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

render() {
    sed "s/__GROVE_HOST__/$host/g" "$here/nginx/$1.template" >"$sites/$1.tmp"
    mv "$sites/$1.tmp" "$sites/$1"
    ln -sfn "$sites/$1" "$enabled/$1"
}

mkdir -p "$sites" "$enabled" "$GROVE_ACME_ROOT"
render grove-http.conf

if [ ! -f "$GROVE_LETSENCRYPT_DIR/live/$host/fullchain.pem" ]; then
    rm -f "$enabled/grove-https.conf"
    nginx -t
    systemctl reload nginx
    log "requesting a certificate for $host"
    certbot certonly --webroot --webroot-path "$GROVE_ACME_ROOT" --domain "$host" \
        --non-interactive --agree-tos --keep-until-expiring
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

nginx -t
systemctl reload nginx
