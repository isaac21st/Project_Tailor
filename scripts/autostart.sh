#!/usr/bin/env bash
# Keep the Tailor server (and Ollama, the local model) running: started at login, restarted if they stop.
#   bash scripts/autostart.sh install     # start now and at every login
#   bash scripts/autostart.sh status
#   bash scripts/autostart.sh update      # after changing the code: refresh the copy it runs from, restart
#   bash scripts/autostart.sh uninstall   # back to starting it by hand
# The server listens on 127.0.0.1 only. Its log: ~/project_tailor_data/logs/server.log
# It runs from a local copy of the code (~/project_tailor_app): macOS does not let a login item read files in OneDrive
# (or iCloud) without asking, and a question nobody sees would leave the server stuck at startup.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${TAILOR_DATA:-$HOME/project_tailor_data}"
MODEL="${TAILOR_MODEL:-qwen3:8b}"
LABEL="com.projecttailor.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$DATA/venv/bin/python"
APP="${TAILOR_APP:-$HOME/project_tailor_app}"

copy_code() {
  mkdir -p "$APP"
  rsync -a --delete --exclude "qwen3-8b - Copy" --exclude "__pycache__" --exclude "project_tailor_transfer" \
        --exclude "*.pdf" --exclude ".git" --exclude "My_Application_Info.json" "$PROJECT/" "$APP/"
}

case "${1:-status}" in
  update)
    # a new extension version each update: the panel shows it, so an open tab running an older copy is easy to spot
    "$PY" - "$PROJECT/extension/manifest.json" <<'PYEOF' || true
import json, sys, time
p = sys.argv[1]; m = json.load(open(p, encoding="utf-8"))
t = time.localtime(); m["version"] = f"0.{t.tm_mon}.{t.tm_mday * 1000 + t.tm_hour * 40 + t.tm_min // 2}"
open(p, "w", encoding="utf-8").write(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
PYEOF
    "$PY" "$PROJECT/scripts/save_my_info.py" || echo "Could not refresh My_Application_Info.json (the server update goes on)."
    # Chrome loads the extension from this OneDrive folder and cannot fetch files OneDrive has offloaded to the cloud
    # (2026-10-07: popup, icons and panel.css went "dataless" on a nearly full disk and Chrome switched the extension
    # off). Reading every file brings it back onto the Mac.
    find "$PROJECT/extension" -type f -exec cat {} + > /dev/null 2>&1 || true
    copy_code
    launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "Code copied to $APP; server restarted."
    ;;
  install)
    [ -x "$PY" ] || { echo "No Python environment at $PY: run scripts/setup_mac.sh first."; exit 1; }
    copy_code
    echo "Code copied to $APP (the login item runs from here)."
    mkdir -p "$DATA/logs" "$HOME/Library/LaunchAgents"
    if command -v brew >/dev/null && brew list ollama >/dev/null 2>&1; then
      brew services start ollama >/dev/null && echo "Ollama: starts at login (brew services)."
    else
      echo "Ollama is not installed with Homebrew: the server will start without drafting help unless Ollama runs."
    fi
    # stop a server started by hand, so the login item can take port 8765
    for pid in $(lsof -tiTCP:8765 -sTCP:LISTEN); do kill "$pid" 2>/dev/null || true; done
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP/scripts/run_server.py</string>
    <string>--model</string><string>ollama</string>
    <string>--ollama-model</string><string>$MODEL</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>20</integer>
  <key>StandardOutPath</key><string>$DATA/logs/server.log</string>
  <key>StandardErrorPath</key><string>$DATA/logs/server.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Tailor server: starts at login, restarts if it stops. Log: $DATA/logs/server.log"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    command -v brew >/dev/null && brew services stop ollama >/dev/null 2>&1 || true
    echo "Removed. Start the server by hand again: $PY $PROJECT/scripts/run_server.py --model ollama --ollama-model $MODEL"
    ;;
  status)
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then echo "Tailor server: login item installed"; else echo "Tailor server: not a login item"; fi
    curl -s --max-time 3 http://127.0.0.1:8765/api/health && echo || echo "Tailor server: not answering on 127.0.0.1:8765"
    command -v brew >/dev/null && brew services list 2>/dev/null | grep -i ollama || true
    ;;
  *) echo "usage: bash scripts/autostart.sh install|status|update|uninstall"; exit 2 ;;
esac
