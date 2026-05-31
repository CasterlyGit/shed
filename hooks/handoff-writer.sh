#!/usr/bin/env bash
# handoff-writer.sh -- Stop hook
# Generates session handoff doc keyed by session ID, copies the ID to clipboard.
# User pastes the ID (hf-XXXXXXXX) into any new session → inject.sh restores context.
set -u
exec 2>>"/Users/casterly/.shed/log/shed.log"

PAYLOAD=$(cat)
[ -n "$PAYLOAD" ] || exit 0
TRANSCRIPT=$(printf '%s' "$PAYLOAD" | jq -r '.transcript_path // empty')
SID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty')
[ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] && [ -n "$SID" ] || exit 0

# Increment turn counter
TURN_FILE="/tmp/shed-turns-$SID"
TURNS=1
[ -f "$TURN_FILE" ] && TURNS=$(( $(cat "$TURN_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$TURNS" > "$TURN_FILE"

# Handoff ID = "shed-ho-" + first 8 chars of session UUID (stable, unique per session)
HF_ID="shed-ho-${SID:0:8}"

python3 /Users/casterly/.claude/scripts/generate-handoff.py "$TRANSCRIPT" "$SID" "$HF_ID" || true

HANDOFF="${HOME}/.claude/state/handoff-${HF_ID}.md"

if [ -f "$HANDOFF" ]; then
  echo "[handoff] $HF_ID ready — paste into new session to restore context" >&2
fi

# Token state. Proxy (filesize/4) is the fallback; prefer the REAL per-session
# context tokens from rate-limits.json when fresh, so the LEARNER tunes on
# ground truth — not just the trigger. (GAP B fix, 2026-05-31.)
TOKEN_FILE="/tmp/shed-tokens-$SID"
SESSION_TOKENS=0
[ -f "$TOKEN_FILE" ] && SESSION_TOKENS="$(cat "$TOKEN_FILE" 2>/dev/null || echo 0)"
REAL_END_TOKENS="$(python3 - <<'PY' 2>/dev/null || true
import json, time, calendar, datetime
from pathlib import Path
try:
    d = json.loads((Path.home() / ".claude/state/rate-limits.json").read_text())
    ts = calendar.timegm(datetime.datetime.strptime(d["captured_at"], "%Y-%m-%dT%H:%M:%SZ").timetuple())
    if time.time() - ts <= 300:
        t = d.get("context_window", {}).get("total_input_tokens", 0)
        if t > 0:
            print(t)
except Exception:
    pass
PY
)"
if [ -n "$REAL_END_TOKENS" ] && [ "$REAL_END_TOKENS" -gt 0 ] 2>/dev/null; then
  SESSION_TOKENS="$REAL_END_TOKENS"
fi

# If session is heavy (above learned pending_inject_tokens threshold), write
# pending-inject.md so /clear + next prompt auto-injects context.
PENDING_THRESHOLD=$(python3 -c "
import json; from pathlib import Path
f = Path.home() / '.shed/state/guard-thresholds.json'
try: print(json.loads(f.read_text()).get('pending_inject_tokens', 80000))
except: print(80000)
" 2>/dev/null || echo 80000)
if [ "$SESSION_TOKENS" -gt "$PENDING_THRESHOLD" ] && [ -f "$HANDOFF" ]; then
  cp "$HANDOFF" "${HOME}/.shed/state/pending-inject.md"
fi

# Learn from this session — nudge thresholds based on outcome
FINAL_TURNS=0
[ -f "$TURN_FILE" ] && FINAL_TURNS="$(cat "$TURN_FILE" 2>/dev/null || echo 0)"
python3 "${HOME}/.shed/scripts/update-guard-thresholds.py" "$SESSION_TOKENS" "$FINAL_TURNS" 2>/dev/null || true

# Auto-cleanup: delete unconsumed handoffs older than 3 days, all archives
find "${HOME}/.claude/state" -maxdepth 1 \( -name "handoff-hf_*.md" -o -name "handoff-shed-ho-*.md" \) -mtime +3 -delete 2>/dev/null || true
find "${HOME}/.claude/state" -maxdepth 1 -name "handoff-archive-*.md" -delete 2>/dev/null || true

exit 0
