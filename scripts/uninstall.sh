#!/bin/bash
# Stop and remove the launchd agent and the SwiftBar plugin. Leaves the repo and the SQLite file.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.pr-radar"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "stopped $LABEL"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
current="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
[ -n "$current" ] && [ -L "$current/pr-radar.1m.sh" ] && rm -f "$current/pr-radar.1m.sh"
rm -f "$ROOT/swiftbar/pr-radar.1m.sh"
echo "removed. (SwiftBar itself is left installed.)"
