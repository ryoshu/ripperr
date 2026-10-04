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

# Control plane only: models run in ripperr-worker, which claims what this prepares.
.venv/bin/python -m ripperr.cli sync
exec .venv/bin/python -m ripperr.cli prepare
