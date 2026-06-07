#!/bin/bash
# handoff-spawn.sh — open a fresh Terminal.app window and inject a handoff ID.
# Generalized from conductor-handoff-spawn.sh.
# Usage: handoff-spawn.sh <handoff-id> [profile] [workdir]
set -euo pipefail

HID="${1:?usage: handoff-spawn.sh <handoff-id> [profile] [workdir]}"
PROFILE="${2:-Homebrew}"
WORKDIR="${3:-$HOME}"

# 1) New window with profile, cd + launch claude.
osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    set newWin to do script "cd " & quoted form of "$WORKDIR" & " && claude"
    set current settings of newWin to settings set "$PROFILE"
end tell
APPLESCRIPT

# 2) Give claude time to boot its TUI.
sleep 6

# 3) Inject via clipboard paste, then Return.
OLD_CLIP="$(pbpaste 2>/dev/null || true)"
printf '%s' "$HID" | pbcopy
osascript <<'APPLESCRIPT'
tell application "Terminal" to activate
delay 0.4
tell application "System Events"
    keystroke "v" using command down
    delay 0.3
    key code 36
end tell
APPLESCRIPT
sleep 1
printf '%s' "$OLD_CLIP" | pbcopy
echo "spawned + injected $HID into Terminal ($PROFILE) at $WORKDIR"
