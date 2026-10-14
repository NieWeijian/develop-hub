#!/bin/sh
set -eu
rm -f /var/dev/web.sock
cd /app/web
export NODE_OPTIONS=--require=/isolation/uds.cjs
export DEV_HTTP_PORT=3025
exec npm run start -- --host 127.0.0.1 --port 3025
