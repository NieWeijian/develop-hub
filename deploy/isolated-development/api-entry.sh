#!/bin/sh
set -eu
rm -f /var/dev/api.sock
/app/.venv/bin/python -m alembic -c /app/server/alembic.ini upgrade testing@head
exec /app/.venv/bin/python -m uvicorn develop_hub.main:app --uds /var/dev/api.sock
