#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_env="$script_dir/../.env"

codex_proxy="${DEVELOP_HUB_CODEX_PROXY:-}"
codex_no_proxy="${DEVELOP_HUB_CODEX_NO_PROXY:-}"
if [ -r "$project_env" ]; then
  if [ -z "$codex_proxy" ]; then
    codex_proxy="$(sed -n 's/^DEVELOP_HUB_CODEX_PROXY=//p' "$project_env" | tail -n 1 | tr -d '\r')"
  fi
  if [ -z "$codex_no_proxy" ]; then
    codex_no_proxy="$(sed -n 's/^DEVELOP_HUB_CODEX_NO_PROXY=//p' "$project_env" | tail -n 1 | tr -d '\r')"
  fi
fi
if [ -n "$codex_proxy" ]; then
  HTTP_PROXY="$codex_proxy"
  HTTPS_PROXY="$codex_proxy"
  http_proxy="$codex_proxy"
  https_proxy="$codex_proxy"
  export HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
fi
if [ -n "$codex_no_proxy" ]; then
  NO_PROXY="$codex_no_proxy"
  no_proxy="$codex_no_proxy"
  export NO_PROXY no_proxy
fi

codex_bin="${DEVELOP_HUB_CODEX_REAL_BIN:-}"
if [ -z "$codex_bin" ]; then
  codex_bin="$(command -v codex || true)"
fi
if [ -z "$codex_bin" ]; then
  echo "codex is not available; set DEVELOP_HUB_CODEX_REAL_BIN in .env" >&2
  exit 1
fi

node_dir="$(dirname -- "$codex_bin")"
PATH="$node_dir:$PATH"
export PATH
exec "$codex_bin" "$@"
