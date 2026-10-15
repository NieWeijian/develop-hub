#!/bin/sh
set -eu
umask 077
rm -f /var/dev/web.sock
cd /app/web
export NODE_OPTIONS=--require=/app/deploy/isolated-development/uds.cjs
export DEV_HTTP_PORT=3025
exec npm run start -- --host 127.0.0.1 --port 3025
