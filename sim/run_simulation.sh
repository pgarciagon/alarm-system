#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_simulation.sh — Launch server + N clients on localhost for local testing
#
# Usage:
#   ./sim/run_simulation.sh [NUM_CLIENTS]
#
#   NUM_CLIENTS  Number of simulated rooms (default: 3)
#
# Requirements:
#   pip install websockets keyboard pygame
#
# Each client is started with --fallback-hotkey so you can type 'a'+Enter
# in its terminal window to trigger an alarm without needing Accessibility
# permission on macOS.
#
# The server and each client get their own config written to /tmp/alarm-sim/.
# ---------------------------------------------------------------------------

set -euo pipefail

NUM_CLIENTS="${1:-3}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIM_DIR="/tmp/alarm-sim"

# ---------------------------------------------------------------------------
# Resolve a Python interpreter that has tkinter.
# Prefer an explicit $PYTHON env var, then search common locations.
# ---------------------------------------------------------------------------
_find_python() {
    # Explicit override always wins
    if [[ -n "${PYTHON:-}" ]]; then
        echo "$PYTHON"; return
    fi
    # Homebrew arm64 / Apple-silicon path (python-tk installs here)
    for candidate in \
        /opt/homebrew/bin/python3.9 \
        /opt/homebrew/bin/python3.11 \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3.11 \
        /usr/local/bin/python3.12 \
        /usr/local/bin/python3 \
        python3
    do
        if command -v "$candidate" &>/dev/null; then
            if "$candidate" -c "import tkinter" 2>/dev/null; then
                echo "$candidate"; return
            fi
        fi
    done
    echo "python3"   # last resort — will fail with a clear error at runtime
}
PYTHON="$(_find_python)"
echo "Using Python: $PYTHON ($($PYTHON --version 2>&1))"

mkdir -p "$SIM_DIR"

# ---------------------------------------------------------------------------
# Write server config
# ---------------------------------------------------------------------------
cat > "$SIM_DIR/server_config.toml" <<'TOML'
[server]
host                  = "127.0.0.1"
port                  = 9999
heartbeat_timeout_sec = 15
log_file              = ""
TOML

# ---------------------------------------------------------------------------
# Write one client config per room
# ---------------------------------------------------------------------------
for i in $(seq 1 "$NUM_CLIENTS"); do
    cat > "$SIM_DIR/client_config_room${i}.toml" <<TOML
[client]
room_name   = "Room ${i}"
server_ip   = "127.0.0.1"
server_port = 9999
hotkey      = "alt+n"
alarm_sound = ""
log_file    = ""
TOML
done

# ---------------------------------------------------------------------------
# Detect terminal emulator (macOS: Terminal.app / iTerm2 via osascript)
# ---------------------------------------------------------------------------
open_terminal() {
    local title="$1"
    local cmd="$2"

    if [[ "$OSTYPE" == "darwin"* ]]; then
        osascript - "$title" "$cmd" <<'APPLESCRIPT'
on run argv
    set winTitle to item 1 of argv
    set shellCmd to item 2 of argv
    tell application "Terminal"
        do script "echo '=== " & winTitle & " ===' && " & shellCmd
        activate
    end tell
end run
APPLESCRIPT
    else
        # Linux fallback: try x-terminal-emulator / gnome-terminal / xterm
        if command -v gnome-terminal &>/dev/null; then
            gnome-terminal --title="$title" -- bash -c "$cmd; exec bash"
        elif command -v xterm &>/dev/null; then
            xterm -title "$title" -e bash -c "$cmd; exec bash" &
        else
            echo "Cannot open terminal for $title. Run manually: $cmd"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Start server in a new terminal window
# ---------------------------------------------------------------------------
SERVER_CMD="cd '$REPO_ROOT' && ALARM_CONFIG_DIR='$SIM_DIR' $PYTHON -m server.server --config '$SIM_DIR/server_config.toml'"
echo "Starting server…"
open_terminal "Alarm Server" "$SERVER_CMD"
sleep 1   # give the server a moment to bind

# ---------------------------------------------------------------------------
# Start each client in its own terminal window
# ---------------------------------------------------------------------------
for i in $(seq 1 "$NUM_CLIENTS"); do
    CLIENT_CMD="cd '$REPO_ROOT' && $PYTHON -m client.client --config '$SIM_DIR/client_config_room${i}.toml' --fallback-hotkey"
    echo "Starting client Room ${i}…"
    open_terminal "Alarm Client — Room ${i}" "$CLIENT_CMD"
    sleep 0.3
done

echo ""
echo "Simulation running with $NUM_CLIENTS rooms."
echo "In any client terminal, type  a  + Enter  to trigger an alarm."
echo "Press Ctrl+C in each window to stop."
