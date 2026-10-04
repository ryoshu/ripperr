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

for command in "$ROOT_DIR/.venv/bin/python" npm lsof curl; do
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

if [[ ! -x "$ROOT_DIR/frontend/node_modules/.bin/vite" ]]; then
  echo "frontend dependencies are missing; run npm ci --prefix frontend" >&2
  exit 1
fi

for port in "$API_PORT" "$WEB_PORT"; do
  owner="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN | head -n 1 || true)"
  if [[ -n "$owner" ]]; then
    echo "port $port is already in use by pid $owner; stop it before starting Ripperr" >&2
    exit 1
  fi
done

api_args=("$ROOT_DIR/.venv/bin/python" -m ripperr.cli serve --host 127.0.0.1 --port "$API_PORT")

(
  cd "$ROOT_DIR"
  nohup "${api_args[@]}" >"$API_LOG" 2>&1 </dev/null &
  echo $! >"$API_PID"
)

cleanup_on_error() {
  local status=$?
  if (( status != 0 )); then
    bash "$ROOT_DIR/scripts/stop-local.sh" || true
  fi
}
trap cleanup_on_error EXIT

(
  cd "$ROOT_DIR/frontend"
  nohup env RIPPERR_API_SERVER="http://127.0.0.1:$API_PORT" \
    VITE_RIPPERR_TOKEN="${RIPPERR_API_TOKEN:-}" \
    npm run dev -- --host 127.0.0.1 --port "$WEB_PORT" \
    >"$WEB_LOG" 2>&1 </dev/null &
  echo $! >"$WEB_PID"
)

wait_for_ready() {
  local label="$1" url="$2" pid_file="$3" log_file="$4"
  local pid
  pid="$(tr -d '[:space:]' <"$pid_file")"
  for _ in {1..100}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    local -a curl_args=(-fsS --max-time 1)
    if [[ "$label" == "API" && -n "${RIPPERR_API_TOKEN:-}" ]]; then
      curl_args+=(-H "Authorization: Bearer $RIPPERR_API_TOKEN")
    fi
    if curl "${curl_args[@]}" "$url" >/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  echo "$label failed to become ready; see $log_file" >&2
  sed -n '1,80p' "$log_file" >&2 || true
  return 1
}

wait_for_ready "API" "http://127.0.0.1:$API_PORT/healthz" "$API_PID" "$API_LOG"
wait_for_ready "dashboard" "http://127.0.0.1:$WEB_PORT/" "$WEB_PID" "$WEB_LOG"

echo "Ripperr API: http://127.0.0.1:$API_PORT"
echo "Ripperr dashboard: http://127.0.0.1:$WEB_PORT"
echo "logs: $API_LOG and $WEB_LOG"
trap - EXIT
