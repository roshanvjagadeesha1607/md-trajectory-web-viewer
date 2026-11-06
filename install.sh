#!/usr/bin/env bash
# Portable installer for the MD trajectory web viewer.
#
#   ./install.sh
#
# Creates a LOCAL virtual environment (./venv) and installs the runtime
# Python dependencies into it. Does NOT touch system Python, does NOT use
# sudo, does NOT change firewall or network settings, and installs nothing
# beyond requirements.txt. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

MIN_PY=12  # requires Python 3.12+

pick_python() {
    # Prefer an explicitly requested interpreter, then python3, then a
    # versioned python3.x from newest to oldest.
    local cands=()
    [ -n "${MTV_PYTHON:-}" ] && cands+=("$MTV_PYTHON")
    cands+=(python3)
    local v
    for v in 3.13 3.12 3.11 3.10 3.9; do cands+=("python$v"); done
    local c
    for c in "${cands[@]}"; do
        if command -v "$c" >/dev/null 2>&1 && \
           "$c" -c "import sys; exit(0 if sys.version_info >= (3,$MIN_PY) else 1)" 2>/dev/null; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

PY=$(pick_python) || {
    echo "ERROR: no suitable Python found." >&2
    echo "       This tool needs Python 3.$MIN_PY or newer (3.12 recommended)." >&2
    echo "       Install python3 (e.g. 'sudo apt install python3 python3-venv')" >&2
    echo "       or point MTV_PYTHON=/path/to/python3 at one and re-run." >&2
    exit 1
}

PYVER=$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
echo "Using Python $PYVER ($PY)"

# pip inside an existing system Python may refuse (PEP 668); a fresh venv
# is exempt, but ensure the venv module exists.
if ! "$PY" -c "import venv" >/dev/null 2>&1; then
    echo "ERROR: this Python has no 'venv' module." >&2
    echo "       On Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
fi

# --- is an existing ./venv actually USABLE on this machine? ---------------
# A venv copied along with the project folder is NOT portable: its
# interpreter symlinks, its base interpreter (pyvenv.cfg home) and its
# package tree (lib/python3.X/site-packages) all belong to the machine it
# was built on. A 3.12 package tree is invisible to a 3.13 interpreter,
# which is exactly how "verification passed but waitress is missing at
# start" happens. So validate every property start_viewer.sh will depend
# on, using THAT interpreter itself — and rebuild from this machine's
# Python if any check fails.
PROJDIR=$(pwd -P)

venv_is_valid() {
    [ -x venv/bin/python ] || return 1
    # 1. it actually executes (a copied bin/python whose symlink target is
    #    absent on this machine dies right here)
    venv/bin/python -c "" >/dev/null 2>&1 || return 1
    # 2. its Python minor version matches the installer Python's (packages
    #    are per-minor-version; a mismatched tree is silently unusable)
    local want got
    want=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    got=$(venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || return 1
    [ "$want" = "$got" ] || return 1
    # 3. the base interpreter it is anchored to exists on THIS machine
    venv/bin/python -c 'import os, sys; raise SystemExit(
        0 if os.path.isfile(getattr(sys, "_base_executable", "") or "") else 1)' \
        >/dev/null 2>&1 || return 1
    # 4. the exact runtime imports work from that exact interpreter
    venv/bin/python -c "import flask, waitress, numpy, scipy, mdtraj" \
        >/dev/null 2>&1 || return 1
    return 0
}

if [ -e venv ]; then
    if venv_is_valid; then
        echo "Reusing existing ./venv (validated on this machine)."
    else
        echo "Existing virtual environment is not portable/compatible with this machine."
        echo "Recreating ./venv..."
        # Remove ONLY this project's ./venv — and only when it really looks
        # like a virtualenv. Projects, folders registry, config, logs, meta
        # and simulation files are outside venv/ and never touched.
        if [ "$PROJDIR/venv" = "$(pwd -P)/venv" ] && \
           { [ -f venv/pyvenv.cfg ] || [ -L venv/bin/python ] || [ -x venv/bin/python ]; }; then
            rm -rf -- "$PROJDIR/venv"
        else
            echo "ERROR: ./venv exists but is not a virtual environment;" >&2
            echo "       move it aside and re-run ./install.sh." >&2
            exit 1
        fi
    fi
fi

if [ ! -d venv ]; then
    echo "Creating virtual environment in ./venv ..."
    "$PY" -m venv venv
fi

echo "Installing runtime dependencies (requirements.txt) ..."
venv/bin/python -m pip install --upgrade pip >/dev/null
venv/bin/python -m pip install -r requirements.txt

echo "Verifying imports ..."
venv/bin/python - <<'EOF'
import flask, waitress, numpy, scipy, mdtraj  # noqa: F401
from importlib.metadata import version as V
import registry  # noqa: F401  (our own modules import cleanly with these)
print(f"  flask {V('flask')}, numpy {V('numpy')}, mdtraj {V('mdtraj')}, "
      f"scipy {V('scipy')}, waitress {V('waitress')}")
EOF

# First-run housekeeping: empty-but-valid registry and runtime dirs, so the
# server starts clean on a machine with no projects yet. Never overwrite.
[ -f projects.json ] || printf '{\n  "version": 1,\n  "base_url": null,\n  "port": 8877,\n  "projects": [],\n  "folders": []\n}\n' > projects.json
mkdir -p logs meta

echo
echo "Installation complete."
echo
echo "Start viewer with:"
echo
echo "    ./start_viewer.sh"
echo
echo "Then open  http://localhost:8877/  in your browser."
echo "Register simulations with ./add_project.sh or ./add_folder.sh."
