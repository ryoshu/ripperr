#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.local"

stop_pid_file() {
  local label="$1"
  local pid_file="$2"
  local pattern="$3"

  if [[ ! -f "$pid_file" ]]; then
    return
  fi
  local pid
  pid="$(tr -d '[:space:]' <"$pid_file")"
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    return
  fi

  local command_line
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$command_line" != *"$pattern"* ]]; then
    echo "not stopping pid $pid: it is not the expected $label process" >&2
    return 1
  fi

  kill "$pid"
  for _ in {1..25}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "stopped $label (pid $pid)"
      return
    fi
    sleep 0.2
  done
  echo "$label (pid $pid) did not stop after 5 seconds" >&2
  return 1
}

for label in com.ryoshu.ripperr.api com.ryoshu.ripperr.web; do
  target="gui/$(id -u)/$label"
  if launchctl print "$target" >/dev/null 2>&1; then
    launchctl bootout "$target"
    echo "stopped $label"
  fi
done

stop_pid_file "dashboard" "$RUN_DIR/web.pid" "npm run dev"
stop_pid_file "API" "$RUN_DIR/api.pid" "ripperr.cli serve"
