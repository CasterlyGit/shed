#!/usr/bin/env bash
# install-runtime.sh — wire the S1 autonomous runtime into launchd.
#
# Idempotent. Installs:
#   1. ~/Library/LaunchAgents/com.casterly.shed-runtime.plist
#      → `shed runtime-tick` every 60s (governor → host keepawake → pause/resume)
#   2. (prints the one root step) /etc/sudoers.d/shed-pmset
#      → lets the tick toggle `pmset -a disablesleep` passwordless, which is
#        the ONLY thing that survives a closed lid. Without it the runtime
#        degrades to caffeinate-only (lid must stay open).
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.casterly.shed-runtime.plist"
LABEL="com.casterly.shed-runtime"
SHED_PY="$HOME/.local/share/uv/tools/shed/bin/python"
LOG="$HOME/Library/Logs/shed-runtime.log"

if [ ! -x "$SHED_PY" ]; then
  echo "ERROR: $SHED_PY not found — install shed first: uv tool install --from $HOME/Documents/Dev/shed shed" >&2
  exit 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SHED_PY}</string>
        <string>-m</string>
        <string>shed</string>
        <string>runtime-tick</string>
    </array>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>5</integer>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✓ ${LABEL} loaded (tick every 60s → ${LOG})"

# ── the one root step: passwordless pmset toggle ──────────────────────────
if sudo -n /usr/bin/pmset -g >/dev/null 2>&1 && \
   sudo -n /usr/bin/pmset -a disablesleep 0 >/dev/null 2>&1; then
  echo "✓ sudoers pmset rule already active (lid-close survival armed)"
else
  cat <<'SUDOERS'

⚠ One root step remains (lid-close survival). Run:

  echo "casterly ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, /usr/bin/pmset -a disablesleep 0" | sudo tee /etc/sudoers.d/shed-pmset && sudo chmod 440 /etc/sudoers.d/shed-pmset

Scoped to exactly those two commands — no wildcard, no shell, lowest blast
radius. Until then the runtime runs caffeinate-only (lid must stay open).
SUDOERS
fi
