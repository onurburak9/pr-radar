#!/bin/bash
# Install PR radar as a login item (launchd) and as a SwiftBar menu-bar plugin.
# Re-runnable: re-running picks up config/port changes and restarts the server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.pr-radar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/pr-radar.log"
LAUNCHD_LOG="$HOME/Library/Logs/pr-radar.launchd.log"
PLUGIN_DIR="$ROOT/swiftbar"
UV="$(command -v uv || echo /opt/homebrew/bin/uv)"

if [ ! -f "$ROOT/config.toml" ]; then
  cp "$ROOT/config.example.toml" "$ROOT/config.toml"
  echo "Created config.toml from config.example.toml. Set github_login and repos, then re-run make install."
  exit 1
fi

PORT="$("$UV" run --project "$ROOT" python -c 'from pr_radar.config import load_config; print(load_config().port)')"

echo "==> uv sync"
"$UV" sync --project "$ROOT" --quiet

echo "==> launchd agent $PLIST (port $PORT)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>-m</string><string>pr_radar</string>
    <string>--log-file</string><string>$LOG</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$LAUNCHD_LOG</string>
  <key>StandardErrorPath</key><string>$LAUNCHD_LOG</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart "gui/$(id -u)/$LABEL"  # bootstrap alone can leave it pended

echo "==> SwiftBar plugin in $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cat > "$PLUGIN_DIR/pr-radar.1m.sh" <<EOF
#!/bin/bash
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
if out=\$(/usr/bin/curl -sf --max-time 4 http://localhost:$PORT/swiftbar); then
  printf '%s\n' "\$out"
else
  echo "PR ⚠ | color=#d92d20"
  echo "---"
  echo "PR radar server is not running | color=#d92d20"
  echo "Restart server | bash=/bin/launchctl param1=kickstart param2=-k param3=gui/\$(id -u)/$LABEL terminal=false refresh=true"
  echo "Open log | bash=/usr/bin/open param1=$LOG terminal=false"
fi
EOF
chmod +x "$PLUGIN_DIR/pr-radar.1m.sh"

if [ -d /Applications/SwiftBar.app ]; then
  current="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
  if [ -z "$current" ]; then
    defaults write com.ameba.SwiftBar PluginDirectory "$PLUGIN_DIR"
    echo "    SwiftBar plugin folder set to $PLUGIN_DIR"
  elif [ "$current" != "$PLUGIN_DIR" ]; then
    ln -sf "$PLUGIN_DIR/pr-radar.1m.sh" "$current/pr-radar.1m.sh"
    echo "    linked into existing SwiftBar plugin folder $current"
  fi
  # SwiftBar's own "Launch at Login" is off by default; without this the menu bar is empty after a reboot.
  if ! osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null | grep -q SwiftBar; then
    osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:false}' >/dev/null \
      && echo "    added SwiftBar to Login Items" \
      || echo "    could not add SwiftBar to Login Items — enable 'Launch at Login' in SwiftBar's preferences"
  fi
  osascript -e 'quit app "SwiftBar"' 2>/dev/null || true
  sleep 1
  open -a SwiftBar
else
  echo "    SwiftBar not installed: brew install --cask swiftbar, then re-run this script"
fi

echo "==> done. Page: http://localhost:$PORT/   Log: $LOG"
