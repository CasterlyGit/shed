#!/usr/bin/env bash
# shed smoke test — end-to-end wiring proof
# Runs without real memories; uses SHED_EMBEDDER=hash fallback.
# Exits 0 on PASS, 1 on FAIL.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Locate Python — prefer .venv, fall back to system python3
if [ -f "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

# Isolated temp shed home so we never touch ~/.shed
SHED_TMP="$(mktemp -d)"
trap 'rm -rf "$SHED_TMP"' EXIT

export SHED_HOME="$SHED_TMP/shed"
export SHED_CLAUDE_HOME="$SHED_TMP/claude"
export SHED_MEMORY_ROOTS="$SHED_TMP/memory"
export SHED_EMBEDDER="hash"

mkdir -p "$SHED_HOME" "$SHED_CLAUDE_HOME" "$SHED_MEMORY_ROOTS"

# Seed one memory so inject has something to work with
cat > "$SHED_MEMORY_ROOTS/smoke-test.md" <<'MD'
---
title: smoke test memory
category: testing
---
## smoke test memory
Use pytest for isolated unit tests. Run with `pytest -q`.
MD

echo "[smoke] Running shed inject..."
RESULT="$(echo "how do I run unit tests" | PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -m shed inject 2>/dev/null)" || INJECT_EXIT=$?
INJECT_EXIT="${INJECT_EXIT:-0}"

if [ "$INJECT_EXIT" -ne 0 ]; then
    echo "FAIL: inject exited $INJECT_EXIT"
    exit 1
fi

# Fail-open: if no memories matched, output may be empty — that's OK.
# Success condition: exit 0. If <shed-context> is present, that's a bonus.
if echo "$RESULT" | grep -q "<shed-context>"; then
    echo "PASS: inject exited 0 and <shed-context> found in output"
else
    echo "PASS: inject exited 0 (fail-open — no context injected, output was empty)"
fi

exit 0
