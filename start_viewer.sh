#!/usr/bin/env bash
# Start the MD Trajectory Web Viewer.
set -u
cd "$(dirname "$0")"
PROJDIR=$(pwd -P)

PORT="${MTV_PORT:-8877}"
LOG="logs/server.log"
PIDFILE="logs/server.pid"
mkdir -p logs

# ALWAYS the project's own venv interpreter — never a system/conda python
# that a sourced activate might leave behind, and never a copied venv from
# another machine (install.sh validates/rebuilds those). If it is missing
# or won't execute, stop here instead of launching the wrong interpreter.
PY="$PROJDIR/venv/bin/python"
if [ ! -x "$PY" ] || ! "$PY" -c "" 2>/dev/null; then
    echo "ERROR: $PY is missing or not runnable on this machine." >&2
    echo "       Run ./install.sh first (it creates a local venv)." >&2
    exit 1
fi
if ! "$PY" -c "import waitress" 2>/dev/null; then
    echo "ERROR: $PY lacks the runtime dependencies (waitress missing)." >&2
    echo "       Run ./install.sh to rebuild the local venv." >&2
    exit 1
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Server already running (PID $(cat "$PIDFILE"))."
else
    MTV_PORT="$PORT" MTV_HOST=0.0.0.0 nohup "$PY" serve.py >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Starting server (PID $!) — first start counts trajectory frames (up to ~2 min)..."
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')

# wait until the server answers (metadata warmup can take a while on first run)
for i in $(seq 1 180); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/api/info"; then
        READY=1; break
    fi
    sleep 1
done

echo ""
echo "=============================================================="
echo "  MD Trajectory Web Viewer running at:"
echo ""
echo "  Local:"
echo "      http://localhost:${PORT}/"
if [ -n "${IP:-}" ]; then
echo ""
echo "  LAN (same institute network):"
echo "      http://${IP}:${PORT}/"
fi
echo ""
if [ "${READY:-0}" = "1" ]; then
    echo "  Server is READY. Open the URL above in Chrome or Firefox."
else
    echo "  WARNING: server not answering yet — check $LOG"
fi
echo "  Stop it with:  ./stop_viewer.sh"
echo "=============================================================="
