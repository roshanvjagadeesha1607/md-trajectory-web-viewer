#!/usr/bin/env bash
# Register a simulation:  ./add_project.sh "Name" /abs/path.gro /abs/path.xtc
# Run with no arguments for an interactive prompt.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/add_project.py "$@"
