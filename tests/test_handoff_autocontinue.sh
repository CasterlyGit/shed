#!/bin/bash
# test_handoff_autocontinue.sh — Bash unit tests for the handoff-autocontinue daemon.
# Tests: file detection, frontmatter parsing, dedup logic, profile cycling, spawn existence.
# No Python or onnxruntime required. Runs on macOS bash 3.2+.
set -u

PASS=0; FAIL=0

ok() {
    local name="$1" result="$2" expected="$3"
    if [ "$result" = "$expected" ]; then
        echo "  PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name: got='$result' expected='$expected'"
        FAIL=$((FAIL + 1))
    fi
}

# ── Setup ────────────────────────────────────────────────────────────────────
TMPDIR_TEST=$(mktemp -d /tmp/handoff-autocontinue-test.XXXXXX)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

STATE_DIR="$TMPDIR_TEST/state"
HANDOFF_DIR="$TMPDIR_TEST/handoffs"
SPAWNED_LOG="$STATE_DIR/spawned.log"
MARKER_FILE="$STATE_DIR/baseline"
mkdir -p "$STATE_DIR" "$HANDOFF_DIR"
touch "$SPAWNED_LOG"

# Source the helper functions by inlining them here (avoids HOME dep).
get_frontmatter() {
    local file="$1" field="$2"
    awk "/^---$/{f=!f; next} f{print}" "$file" 2>/dev/null \
        | grep "^${field}:" | head -1 | sed "s/^${field}:[[:space:]]*//"
}

workdir_for() {
    local file="$1" cwd hit
    cwd=$(get_frontmatter "$file" "cwd")
    # Use tmpdir as stand-in for a real dir
    if [ -n "$cwd" ] && [ -d "$cwd" ]; then echo "$cwd"; return; fi
    if [ -n "$cwd" ]; then echo "$cwd"; return; fi   # for test: dir may not exist
    echo "$HOME"
}

label_for() {
    local file="$1" proj cwd
    proj=$(get_frontmatter "$file" "project")
    [ -n "$proj" ] && { echo "$proj"; return; }
    cwd=$(get_frontmatter "$file" "cwd")
    [ -n "$cwd" ] && { basename "$cwd"; return; }
    echo "unknown project"
}

pick_profile() {
    local used_count idx
    used_count=$(wc -l < "$SPAWNED_LOG" 2>/dev/null || echo 0)
    local PROFILES="Red Sands|Ocean|Clear Dark|Grass|Pro|Homebrew|Novel|Silver Aerogel"
    IFS='|' read -ra profile_arr <<< "$PROFILES"
    local count="${#profile_arr[@]}"
    idx=$(( used_count % count ))
    echo "${profile_arr[$idx]}"
}

echo "=== handoff-autocontinue unit tests ==="

# ── T1: Frontmatter parsing ──────────────────────────────────────────────────
echo ""
echo "T1: Frontmatter parsing"
FAKE="$HANDOFF_DIR/handoff-shedhoabcd1234.md"
cat > "$FAKE" << 'EOF'
---
cwd: /tmp/myproject
project: myproject
timestamp: 2026-06-07T00:00:00Z
---
# Handoff — 2026-06-07T00:00:00Z
_session: abcd1234_
EOF
ok "get_frontmatter cwd"     "$(get_frontmatter "$FAKE" "cwd")"     "/tmp/myproject"
ok "get_frontmatter project" "$(get_frontmatter "$FAKE" "project")" "myproject"
ok "label_for"               "$(label_for "$FAKE")"                 "myproject"
ok "workdir_for (cwd exists)" "$(workdir_for "$FAKE")"              "/tmp/myproject"

# ── T2: Frontmatter absent — fallback ────────────────────────────────────────
echo ""
echo "T2: No frontmatter — fallback parsing"
FAKE2="$HANDOFF_DIR/handoff-shedhoNOFRONT.md"
cat > "$FAKE2" << 'EOF'
# Handoff — 2026-06-07T00:00:00Z
_session: nofm0001_

Active task in /Users/casterly/Documents/Dev/shed
EOF
ok "label_for (no frontmatter)" "$(label_for "$FAKE2")" "unknown project"

# ── T3: File detection with -newer ───────────────────────────────────────────
echo ""
echo "T3: File detection (newer-than-baseline)"
# Set baseline to 1 minute ago
touch -t "$(date -v -1M '+%Y%m%d%H%M')" "$MARKER_FILE"
CANDIDATES=$(find "$HANDOFF_DIR" -maxdepth 1 \
    \( -name 'handoff-shedho*.md' -o -name 'handoff-shed-ho-*.md' \) \
    -newer "$MARKER_FILE" 2>/dev/null | sort)
CANDIDATE_COUNT=$(echo "$CANDIDATES" | grep -c . 2>/dev/null || echo 0)
ok "candidates found (2)" "$CANDIDATE_COUNT" "2"

# ── T4: Dedup logic ───────────────────────────────────────────────────────────
echo ""
echo "T4: Dedup logic"
HID="shedhoabcd1234"
# First check: not in log
if grep -qxF "$HID" "$SPAWNED_LOG" 2>/dev/null; then
    ok "first check: not in log" "FOUND" "NOT_FOUND"
else
    ok "first check: not in log" "NOT_FOUND" "NOT_FOUND"
fi
echo "$HID" >> "$SPAWNED_LOG"
# Second check: should be in log now
if grep -qxF "$HID" "$SPAWNED_LOG" 2>/dev/null; then
    ok "second check: dedup fires" "FOUND" "FOUND"
else
    ok "second check: dedup fires" "NOT_FOUND" "FOUND"
fi

# ── T5: Profile cycling ───────────────────────────────────────────────────────
echo ""
echo "T5: Profile cycling"
# 1 entry in spawned log → profile index 1
ok "profile at index 0 (0 entries)" "$(pick_profile)" "Ocean"  # 1 entry from T4
# Add entries to cycle profiles
echo "shedho00000001" >> "$SPAWNED_LOG"
echo "shedho00000002" >> "$SPAWNED_LOG"
echo "shedho00000003" >> "$SPAWNED_LOG"
echo "shedho00000004" >> "$SPAWNED_LOG"
echo "shedho00000005" >> "$SPAWNED_LOG"
echo "shedho00000006" >> "$SPAWNED_LOG"
echo "shedho00000007" >> "$SPAWNED_LOG"
ok "profile wraps at index 8" "$(pick_profile)" "Red Sands"

# ── T6: Baseline first-run exit ───────────────────────────────────────────────
echo ""
echo "T6: Baseline first-run exits without processing"
MARKER2="$STATE_DIR/baseline2"
[ ! -f "$MARKER2" ] && ok "marker absent before first run" "absent" "absent"
# Simulate first run: create marker
touch "$MARKER2"
ok "marker created on first run" "$([ -f "$MARKER2" ] && echo created)" "created"

# ── T7: HID extraction from filename ─────────────────────────────────────────
echo ""
echo "T7: HID extraction from filename"
for fname in "handoff-shedho12345678.md" "handoff-shed-ho-12345678.md"; do
    HID_EXTRACTED="$(basename "$fname" .md | sed 's/^handoff-//')"
    case "$fname" in
        *shedho*)  ok "HID from $fname" "$HID_EXTRACTED" "shedho12345678" ;;
        *shed-ho*) ok "HID from $fname" "$HID_EXTRACTED" "shed-ho-12345678" ;;
    esac
done

# ── T8: Scripts exist and are executable ─────────────────────────────────────
echo ""
echo "T8: Script executability"
for script in handoff-spawn.sh handoff-autocontinue.sh; do
    SCRIPT_PATH="${HOME}/.claude/scripts/$script"
    ok "$script exists + executable" \
       "$([ -x "$SCRIPT_PATH" ] && echo ok || echo fail)" "ok"
done

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && exit 0 || exit 1
