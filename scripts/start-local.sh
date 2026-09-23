#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.local"
API_PID="$RUN_DIR/api.pid"
WEB_PID="$RUN_DIR/web.pid"
API_LOG="$RUN_DIR/api.log"
WEB_LOG="$RUN_DIR/web.log"

mkdir -p "$RUN_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

API_PORT="${RIPPERR_API_PORT:-8876}"
WEB_PORT="${RIPPERR_WEB_PORT:-5174}"

for command in "$ROOT_DIR/.venv/bin/python" npm lsof; do
  if [[ "$command" == */* ]]; then
    if [[ ! -x "$command" ]]; then
      echo "missing required command: $command" >&2
      exit 1
    fi
  elif ! command -v "$command" >/dev/null 2>&1; then
    echo "missing required command: $command" >&2
    exit 1
  fi
done

for port in "$API_PORT" "$WEB_PORT"; do
  owner="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN | head -n 1 || true)"
  if [[ -n "$owner" ]]; then
    echo "port $port is already in use by pid $owner; stop it before starting Ripperr" >&2
    exit 1
  fi
done

api_args=("$ROOT_DIR/.venv/bin/python" -m ripperr.cli serve --host 127.0.0.1 --port "$API_PORT")
if [[ -n "${RIPPERR_API_TOKEN:-}" ]]; then
  api_args+=(--token "$RIPPERR_API_TOKEN")
fi

(
  cd "$ROOT_DIR"
  nohup "${api_args[@]}" >"$API_LOG" 2>&1 </dev/null &
  echo $! >"$API_PID"
)

(
  cd "$ROOT_DIR/frontend"
  nohup env RIPPERR_API_SERVER="http://127.0.0.1:$API_PORT" \
    npm run dev -- --host 127.0.0.1 --port "$WEB_PORT" \
    >"$WEB_LOG" 2>&1 </dev/null &
  echo $! >"$WEB_PID"
)

sleep 0.5
for item in "API:$API_PID:$API_LOG" "dashboard:$WEB_PID:$WEB_LOG"; do
  IFS=: read -r label pid_file log_file <<<"$item"
  pid="$(tr -d '[:space:]' <"$pid_file")"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$label failed to start; see $log_file" >&2
    sed -n '1,80p' "$log_file" >&2 || true
    exit 1
  fi
done

echo "Ripperr API: http://127.0.0.1:$API_PORT"
echo "Ripperr dashboard: http://127.0.0.1:$WEB_PORT"
echo "logs: $API_LOG and $WEB_LOG"
