#!/bin/bash
# Manage the lc7001-hue background service on macOS (launchd LaunchAgent).
#
#   ./service.sh test        one-shot connectivity check
#   ./service.sh run         run in the foreground with debug logging
#   ./service.sh install     install + start the LaunchAgent (runs at login)
#   ./service.sh uninstall   stop + remove the LaunchAgent
#   ./service.sh restart     restart it
#   ./service.sh status      is it running?
#   ./service.sh logs        tail the log
#   ./service.sh ui          print the web UI address
#   ./service.sh update      git pull, refresh dependencies, restart
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="net.bakernt.lc7001-hue"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/lc7001-hue"
PYBIN="$HERE/venv/bin/python"

if [ ! -x "$PYBIN" ]; then
  echo "No virtualenv yet - run ./install.sh first."
  exit 1
fi

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYBIN</string>
        <string>$HERE/lc7001_hue.py</string>
        <string>--config</string>
        <string>$HERE/config.json</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HERE</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$LOGDIR/lc7001-hue.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGDIR/lc7001-hue.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLISTEOF
}

uid="$(id -u)"

case "${1:-}" in
  test)
    "$PYBIN" "$HERE/lc7001_hue.py" --config "$HERE/config.json" --check
    ;;
  run)
    "$PYBIN" "$HERE/lc7001_hue.py" --config "$HERE/config.json" --verbose
    ;;
  install)
    write_plist
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$PLIST"
    launchctl enable "gui/$uid/$LABEL"
    echo "Installed and started."
    echo "Web UI:  http://$(ipconfig getifaddr en0 2>/dev/null || hostname):8582"
    echo "Logs:    $LOGDIR/lc7001-hue.log"
    ;;
  uninstall)
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed."
    ;;
  restart)
    launchctl kickstart -k "gui/$uid/$LABEL"
    echo "Restarted."
    ;;
  status)
    if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
      launchctl print "gui/$uid/$LABEL" | grep -E "state|pid|last exit" || true
    else
      echo "Not installed."
    fi
    ;;
  update)
    if [ ! -d "$HERE/.git" ]; then
      echo "Not a git checkout - nothing to pull."
      exit 1
    fi
    git -C "$HERE" pull --ff-only
    "$HERE/venv/bin/pip" install -q -r "$HERE/requirements.txt"
    launchctl kickstart -k "gui/$uid/$LABEL" 2>/dev/null || true
    echo "Updated and restarted."
    ;;
  ui)
    echo "http://$(ipconfig getifaddr en0 2>/dev/null || hostname):8582"
    ;;
  logs)
    mkdir -p "$LOGDIR"
    touch "$LOGDIR/lc7001-hue.log"
    tail -f "$LOGDIR/lc7001-hue.log"
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
