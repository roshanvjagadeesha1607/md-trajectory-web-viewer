#!/usr/bin/env bash
# Register an allowed-root folder:  ./add_folder.sh "Name" /abs/path
# Run with no arguments for an interactive prompt.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_folders.py add "$@"
