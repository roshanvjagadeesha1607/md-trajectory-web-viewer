#!/usr/bin/env bash
# Edit a registered project (registry fields only):
#   ./edit_project.sh PROJECT_ID --name "New name" --description "..."
#   ./edit_project.sh PROJECT_ID --gro /abs/new.gro --xtc /abs/new.xtc
#   ./edit_project.sh PROJECT_ID          # interactive
# Simulation files are never modified.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_projects.py edit "$@"
