#!/usr/bin/env bash
# List registered folders:  ./list_folders.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_folders.py list "$@"
