#!/bin/sh
set -eu

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

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

cd "$project_root/server"
PYTHONPATH=src ../.venv/bin/python -m alembic -c alembic.ini upgrade head

service_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$service_dir"
cp "$project_root/deploy/systemd/user/develop-hub-api.service" "$service_dir/"
cp "$project_root/deploy/systemd/user/develop-hub-web.service" "$service_dir/"

systemctl --user daemon-reload
systemctl --user enable --now develop-hub-api.service develop-hub-web.service
echo "Develop Hub is running: http://127.0.0.1:3000"
