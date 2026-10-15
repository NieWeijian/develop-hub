#!/bin/sh
set -eu
umask 077
rm -f /var/dev/api.sock
python -m alembic -c /app/server/alembic.ini upgrade testing@head
exec python -m uvicorn develop_hub.main:app --uds /var/dev/api.sock
