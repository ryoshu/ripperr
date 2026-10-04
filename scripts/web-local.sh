#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

export RIPPERR_API_SERVER="${RIPPERR_API_SERVER:-http://127.0.0.1:${RIPPERR_API_PORT:-8876}}"
export VITE_RIPPERR_TOKEN="${RIPPERR_API_TOKEN:-}"
cd frontend
exec node node_modules/vite/bin/vite.js \
  --host=127.0.0.1 --port="${RIPPERR_WEB_PORT:-5174}"
