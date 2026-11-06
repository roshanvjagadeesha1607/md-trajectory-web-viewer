#!/usr/bin/env bash
# Edit a folder registration:
#   ./edit_folder.sh FOLDER_ID [--name "New name"] [--root /abs/path]
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_folders.py edit "$@"
