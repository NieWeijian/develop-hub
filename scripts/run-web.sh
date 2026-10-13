#!/bin/sh
set -eu

if [ -n "${DEVELOP_HUB_NPM_BIN:-}" ]; then
  npm_dir="$(dirname -- "$DEVELOP_HUB_NPM_BIN")"
  PATH="$npm_dir:$PATH"
  export PATH
  exec "$DEVELOP_HUB_NPM_BIN" run start -- --host 0.0.0.0
fi

npm_bin="$(command -v npm || true)"
if [ -z "$npm_bin" ]; then
  echo "npm is not available; set DEVELOP_HUB_NPM_BIN in .env" >&2
  exit 1
fi

exec "$npm_bin" run start -- --host 0.0.0.0
