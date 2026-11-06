#!/usr/bin/env bash
# List registered simulations:  ./list_projects.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_projects.py list "$@"
