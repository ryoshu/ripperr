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

args=(.venv/bin/python -m ripperr.cli serve --host 127.0.0.1 --port "${RIPPERR_API_PORT:-8876}")
if [[ -n "${RIPPERR_API_TOKEN:-}" ]]; then
  args+=(--token "$RIPPERR_API_TOKEN")
fi
exec "${args[@]}"
