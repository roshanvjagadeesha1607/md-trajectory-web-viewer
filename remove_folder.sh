#!/usr/bin/env bash
# Remove a folder REGISTRATION ONLY (never the directory or its files):
#   ./remove_folder.sh FOLDER_ID
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_folders.py remove "$@"
