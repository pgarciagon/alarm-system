#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_simulation.sh — Thin wrapper around sim/launch.py
#
# Usage:
#   ./sim/run_simulation.sh [NUM_ROOMS]
#
# On macOS each component opens in its own Terminal.app window via
# `open -a Terminal`, which gives each process proper AppKit/NSRunLoop
# context (required for tkinter).
# Trigger an alarm: in any client window type  a  + Enter.
# ---------------------------------------------------------------------------

set -euo pipefail

NUM_ROOMS="${1:-3}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Find a Python with tkinter
_find_python() {
    if [[ -n "${PYTHON:-}" ]]; then echo "$PYTHON"; return; fi
    # Prefer 3.12+ — ships with Tcl/Tk 9 which works on macOS 26 (Tahoe).
    # Python 3.9 has Tcl/Tk 8.6 which crashes on macOS 26.
    for c in \
        /opt/homebrew/opt/python@3.13/bin/python3.13 \
        /opt/homebrew/opt/python@3.12/bin/python3.12 \
        /opt/homebrew/opt/python@3.11/bin/python3.11 \
        /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.11 \
        /usr/local/opt/python@3.13/bin/python3.13 \
        /usr/local/opt/python@3.12/bin/python3.12 \
        /usr/local/opt/python@3.11/bin/python3.11 \
        /usr/local/bin/python3.13 \
        /usr/local/bin/python3.12 \
        /usr/local/bin/python3.11 \
        /opt/homebrew/bin/python3.9 \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        python3
    do
        if command -v "$c" &>/dev/null && "$c" -c "import tkinter" 2>/dev/null; then
            echo "$c"; return
        fi
    done
    echo "python3"
}

PYTHON="$(_find_python)"
echo "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

exec "$PYTHON" "$REPO_ROOT/sim/launch.py" --rooms "$NUM_ROOMS" --python "$PYTHON"
