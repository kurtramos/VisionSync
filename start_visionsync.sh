#!/usr/bin/env bash
# VisionSync — Linux/macOS launcher (equivalent of the Windows .bat scripts).
#
# Default: runs camera_service.py in the background (like
# Start_VisionSync_System.bat's silent pythonw) and opens the UI in the
# default browser.
#
# Debug mode: --debug (or -d) keeps the service in the foreground with its
# console output visible (like Start_Debug_VisionSync_System.bat) instead of
# backgrounding it.
set -euo pipefail

# Always run from the directory this script lives in, regardless of cwd.
cd "$(dirname "${BASH_SOURCE[0]}")"

DEBUG=0
for arg in "$@"; do
  case "$arg" in
    --debug|-d) DEBUG=1 ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

if [ ! -f .env ]; then
  echo "[!] .env not found — copy .env.example to .env and fill in your Nx credentials first." >&2
  exit 1
fi

PORT="${PORT:-5010}"
URL="http://127.0.0.1:${PORT}/"

open_browser() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 &
  else
    echo "[i] Open $URL in a browser — no xdg-open/open found to do it automatically."
  fi
}

if [ "$DEBUG" -eq 1 ]; then
  echo "Starting Camera Service (camera_service.py) in DEBUG mode..."
  ( sleep 2; open_browser ) &
  exec "$PYTHON_BIN" camera_service.py
else
  echo "Launching VisionSync Camera Configuration Service..."
  nohup "$PYTHON_BIN" camera_service.py > visionsync.log 2>&1 &
  disown
  echo "VisionSync started in the background (PID $!, logs in visionsync.log)."
  sleep 2
  open_browser
fi
