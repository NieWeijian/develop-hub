#!/bin/sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  echo "python3-venv is required. On Ubuntu: sudo apt install python3.10-venv" >&2
  exit 1
fi

if [ ! -x .venv/bin/pip ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e './server'

cd "$project_root/web"
npm ci
npm run build

cd "$project_root"
PYTHONPATH=server/src .venv/bin/python -m alembic -c server/alembic.ini upgrade testing@head

service_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$service_dir"
# Render the checkout location into user units instead of assuming a home path.
.venv/bin/python - "$project_root" "$service_dir" <<'PY'
import sys
from pathlib import Path
root, target = map(Path, sys.argv[1:])
escaped = str(root).replace('%', '%%').replace(' ', r'\x20')
for name in ('develop-hub-api.service', 'develop-hub-web.service'):
    content = (root / 'deploy/systemd/user' / name).read_text().replace('%h/develop-hub', escaped)
    (target / name).write_text(content)
PY

systemctl --user daemon-reload
systemctl --user enable --now develop-hub-api.service develop-hub-web.service
echo "Develop Hub is running: http://127.0.0.1:3000"
