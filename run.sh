#!/usr/bin/env bash
# Start the VelocityRL items API + background watcher
set -euo pipefail

cd "$(dirname "$0")"

# Generate items.json if it doesn't exist yet
if [ ! -f items.json ]; then
    echo "[run] Generating items.json..."
    python3 extract_items.py
fi

# Start the watcher in the background
python3 watcher.py &
WATCHER_PID=$!
echo "[run] Watcher started (PID $WATCHER_PID)"

# Trap to kill watcher on exit
cleanup() {
    echo "[run] Stopping watcher..."
    kill "$WATCHER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Start the API server
echo "[run] Starting API on http://0.0.0.0:8000"
python3 -m uvicorn api:app --host 0.0.0.0 --port 8000
