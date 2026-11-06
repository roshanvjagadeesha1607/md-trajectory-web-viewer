#!/usr/bin/env bash
# Re-discover components from each project's GRO structure file:
#   ./rescan_projects.sh                 # every registered project
#   ./rescan_projects.sh PROJECT_ID      # one project
#
# Use after upgrading the component classifier, or when a project was
# registered before auto-discovery existed. GRO/XTC files are only READ;
# visibility choices from earlier are preserved. The server picks the new
# metadata up automatically (projects.json is hot-reloaded).
set -euo pipefail
cd "$(dirname "$0")"
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" tools/manage_projects.py rescan "$@"
