#!/usr/bin/env bash
# Stop the MD Trajectory Web Viewer.
cd "$(dirname "$0")"
PIDFILE="logs/server.pid"

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" && echo "Stopped viewer server (PID $PID)."
    else
        echo "PID $PID not running; cleaning stale pidfile."
    fi
    rm -f "$PIDFILE"
else
    echo "No pidfile found; looking for a python serve.py process..."
    # Match only the interpreter running our serve.py, never this script.
    for pid in $(pgrep -f "python .*serve\.py" 2>/dev/null); do
        kill "$pid" && echo "Stopped viewer server (PID $pid)."
    done
fi
