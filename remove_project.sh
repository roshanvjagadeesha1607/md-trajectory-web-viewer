#!/usr/bin/env bash
# Remove a project from the library:  ./remove_project.sh PROJECT_ID
# Only the projects.json entry is deleted -- GRO/XTC files are never touched.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_projects.py remove "$@"
