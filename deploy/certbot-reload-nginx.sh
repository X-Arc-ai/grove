#!/bin/sh
# certbot runs this after it renews a certificate, so nginx serves the new one.
nginx -t && systemctl reload nginx
