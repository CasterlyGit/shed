#!/bin/bash
# handoff-autocontinue.sh — launchd WatchPaths daemon body.
# Fired whenever ~/.claude/state/ changes. Scans for newly written handoff docs,
# deduplicates by ID (flock-serialized), prompts user Yes/No, and on Yes spawns
# a new Terminal window with the handoff ID injected.
#
# Compatible with macOS system bash (3.2).
# Deps: flock (via util-linux brew), osascript, pbcopy, pbpaste
# State: ~/.shed/state/handoff-spawned.log  (one ID per line)
# Lock:  ~/.shed/state/handoff-autocontinue.lock

set -u
LOG_FILE="${HOME}/.shed/log/handoff-autocontinue.log"
mkdir -p "$(dirname "$LOG_FILE")" "${HOME}/.shed/state"
exec >>"$LOG_FILE" 2>&1

HANDOFF_DIR="${HOME}/.claude/state"
SPAWNED_LOG="${HOME}/.shed/state/handoff-spawned.log"
LOCK_FILE="${HOME}/.shed/state/handoff-autocontinue.lock"
SPAWN_SCRIPT="${HOME}/.claude/scripts/handoff-spawn.sh"
MARKER_FILE="${HOME}/.shed/state/handoff-autocontinue-baseline"

touch "$SPAWNED_LOG"

# Terminal profiles to cycle through so concurrent resumed windows are distinct.
PROFILES="Red Sands|Ocean|Clear Dark|Grass|Pro|Homebrew|Novel|Silver Aerogel"

pick_profile() {
    local used_count idx profile_arr
    used_count=$(wc -l < "$SPAWNED_LOG" 2>/dev/null || echo 0)
    # Split pipe-delimited list into positional params
    IFS='|' read -ra profile_arr <<< "$PROFILES"
    local count="${#profile_arr[@]}"
    idx=$(( used_count % count ))
    echo "${profile_arr[$idx]}"
}

# Extract a YAML frontmatter field from a handoff doc.
get_frontmatter() {
    local file="$1" field="$2"
    awk '/^---$/{f=!f; next} f{print}' "$file" 2>/dev/null \
        | grep "^${field}:" | head -1 | sed "s/^${field}:[[:space:]]*//"
}

workdir_for() {
    local file="$1" cwd hit
    cwd=$(get_frontmatter "$file" "cwd")
    if [ -n "$cwd" ] && [ -d "$cwd" ]; then echo "$cwd"; return; fi
    hit=$(grep -oE "($HOME|~)/Documents/Dev/[A-Za-z0-9_-]+" "$file" 2>/dev/null \
          | head -1 | sed "s|~|$HOME|")
    if [ -n "$hit" ] && [ -d "$hit" ]; then echo "$hit"; return; fi
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

# ── First-run baseline: on very first invocation, just set the marker and exit.
# This prevents prompting for all historical handoffs when first installed.
if [ ! -f "$MARKER_FILE" ]; then
    touch "$MARKER_FILE"
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] baseline set — ignoring pre-existing handoffs"
    exit 0
fi

# ── Find candidate handoff files newer than baseline ────────────────────────
# Using find with -newer; results into a newline-delimited string.
CANDIDATES=$(find "$HANDOFF_DIR" -maxdepth 1 \
    \( -name 'handoff-shedho*.md' -o -name 'handoff-shed-ho-*.md' \) \
    -newer "$MARKER_FILE" 2>/dev/null \
    | sort)

if [ -z "$CANDIDATES" ]; then
    exit 0
fi

# Advance baseline so the NEXT run only sees truly new files.
touch "$MARKER_FILE"

# ── Serialize all processing behind a global lock ───────────────────────────
# flock: requires util-linux (brew install util-linux).
# Fallback: if flock is not available, use a simple lock file with PID check.
FLOCK_BIN=$(command -v flock 2>/dev/null || true)

run_locked() {
    if [ -n "$FLOCK_BIN" ]; then
        (
            "$FLOCK_BIN" -x 200
            process_candidates
        ) 200>"$LOCK_FILE"
    else
        # Lightweight fallback: sleep-and-retry spin (≤3 tries, 1s apart)
        local attempts=0
        while [ $attempts -lt 3 ]; do
            if ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2>/dev/null; then
                trap 'rm -f "$LOCK_FILE"' EXIT INT TERM
                process_candidates
                rm -f "$LOCK_FILE"
                break
            fi
            attempts=$((attempts + 1))
            sleep 1
        done
    fi
}

process_candidates() {
    # Iterate over newline-separated candidate paths.
    while IFS= read -r HANDOFF_FILE; do
        [ -z "$HANDOFF_FILE" ] && continue
        [ -f "$HANDOFF_FILE" ] || continue

        # Extract bare ID: handoff-shed-ho-XXXXXXXX.md → shed-ho-XXXXXXXX
        # or handoff-shedhoXXXXXXXX.md → shedhoXXXXXXXX
        HID="$(basename "$HANDOFF_FILE" .md | sed 's/^handoff-//')"
        [ -n "$HID" ] || continue

        # Dedup check
        if grep -qxF "$HID" "$SPAWNED_LOG" 2>/dev/null; then
            echo "[$(date '+%Y-%m-%dT%H:%M:%S')] already processed $HID — skip"
            continue
        fi

        PROJECT=$(label_for "$HANDOFF_FILE")
        WORKDIR=$(workdir_for "$HANDOFF_FILE")
        PROFILE=$(pick_profile)

        echo "[$(date '+%Y-%m-%dT%H:%M:%S')] new handoff: $HID  project=$PROJECT  workdir=$WORKDIR"

        # Yes/No prompt — native macOS dialog, 600s timeout defaults to No.
        ANSWER=$(osascript -e "
set dlg to display dialog \"Session ended in \\\"${PROJECT}\\\".

Handoff ID: ${HID}

Open a new terminal and auto-resume?\" ¬
    buttons {\"No\", \"Yes\"} ¬
    default button \"No\" ¬
    with title \"Handoff Ready — ${PROJECT}\" ¬
    giving up after 600
if gave up of dlg then return \"No\"
return button returned of dlg
" 2>/dev/null || echo "No")

        # Record as processed regardless of answer (no double-prompt ever).
        echo "$HID" >> "$SPAWNED_LOG"

        if [ "$ANSWER" = "Yes" ]; then
            echo "[$(date '+%Y-%m-%dT%H:%M:%S')] user Yes — spawning ($PROFILE) at $WORKDIR"
            bash "$SPAWN_SCRIPT" "$HID" "$PROFILE" "$WORKDIR" \
                >> "$LOG_FILE" 2>&1 &
            # Brief delay so window focus settles before next prompt.
            sleep 2
        else
            echo "[$(date '+%Y-%m-%dT%H:%M:%S')] No / timeout for $HID"
        fi

    done <<< "$CANDIDATES"
}

run_locked
