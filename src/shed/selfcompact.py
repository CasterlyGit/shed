"""In-session self-compaction.

Claude Code auto-compacts the context on its own when it nears the window
limit — the user does not (and often *cannot*) type ``/compact`` mid-work.
The gap is that auto-compaction has no idea *what to keep*, so it summarises
blindly: it can drop the active task and re-bloat the next turns by dragging
the handoff scaffolding back in.

Two hard facts from the Claude Code hook contract shape this module:

1. A ``PreCompact`` hook **cannot** feed focus text to the summariser. Its
   only power is block/allow (exit 2) — stdout does not reach the summary.
2. The ONE documented lever for "what to preserve" is a **Compact
   Instructions** section in ``CLAUDE.md``, which the summariser reads.

So self-compaction = keep a fresh, lean *Compact Instructions* block in the
global ``CLAUDE.md``, refreshed from the live transcript right before any
compaction fires. The block tells the auto-summariser to keep the active
task + last unprocessed user message and to DROP tool-output noise and the
cross-session handoff scaffolding (which belongs in handoff docs, not in
every in-session summary). Fail-open everywhere — a compaction must never
be broken by shed.

Entry points:
- ``on_precompact(payload)`` — PreCompact hook handler.
- ``refresh_compact_instructions(transcript_path)`` — rebuild the block.
- ``install_to_claude_settings`` / ``uninstall_from_claude_settings``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from shed.config import claude_home, state_dir

# Marker-delimited managed block. We only ever touch text between these two
# lines, so a user editing the rest of CLAUDE.md is never clobbered.
BEGIN = "<!-- BEGIN shed:compact-instructions (managed — do not edit by hand) -->"
END = "<!-- END shed:compact-instructions -->"

# The standing guidance. The transcript-derived "active task" line is appended
# beneath it when we can extract one.
_HEADER = "## Compact Instructions"
_GUIDANCE = """\
When compacting this conversation, KEEP:
- The active task and its current state (what is being built/debugged right now).
- The most recent user message and any not-yet-finished instruction.
- Key file paths, function names, and decisions made this session.

DROP:
- Verbatim tool outputs, file dumps, and command logs (keep only conclusions).
- Cross-session handoff scaffolding (handoff IDs, "paste into new session"
  text, shed-context blocks) — that lives in handoff docs, not in this
  session's summary; re-reading it every turn is pure waste.
- Resolved sub-steps once their outcome is captured in one line."""


def _now_compact_log_row(trigger: str, wrote_block: bool, active_task: str) -> dict:
    return {
        "ts": time.time(),
        "event": "compact",
        "trigger": trigger,
        "wrote_block": wrote_block,
        "active_task": active_task[:200],
    }


def on_precompact(payload: dict) -> bool:
    """PreCompact hook handler. Refresh the Compact Instructions block from the
    live transcript, then log the compaction event. Returns True if the block
    was written/updated. Never raises — compaction must always proceed.
    """
    try:
        transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
        trigger = (payload.get("trigger") if isinstance(payload, dict) else "") or "unknown"
        active_task, wrote = refresh_compact_instructions(
            Path(transcript_path) if transcript_path else None
        )
        _log_compact(trigger, wrote, active_task)
        return wrote
    except Exception:
        return False


def refresh_compact_instructions(transcript_path: Path | None) -> tuple[str, bool]:
    """Rebuild the managed Compact Instructions block in global CLAUDE.md.

    Returns ``(active_task, wrote)``. ``wrote`` is False if the block was
    already identical (idempotent — no needless churn / git noise).
    """
    active_task = ""
    if transcript_path is not None:
        active_task = _active_task_from_transcript(transcript_path)

    block = _render_block(active_task)
    md_path = claude_home() / "CLAUDE.md"
    wrote = _upsert_block(md_path, block)
    return active_task, wrote


# ---------------------------------------------------------------------------


def _render_block(active_task: str) -> str:
    body = [BEGIN, _HEADER, "", _GUIDANCE]
    if active_task:
        body += ["", f"**Active task this session:** {active_task}"]
    body += [END]
    return "\n".join(body)


def _upsert_block(md_path: Path, block: str) -> bool:
    """Insert or replace the managed block in ``md_path``. Idempotent."""
    try:
        text = md_path.read_text() if md_path.exists() else ""
    except Exception:
        return False

    if BEGIN in text and END in text:
        pre, rest = text.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        new_text = pre + block + post
    else:
        # Append at end, separated by a blank line.
        sep = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
        new_text = text + sep + block + "\n"

    if new_text == text:
        return False
    try:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(new_text)
        return True
    except Exception:
        return False


def _active_task_from_transcript(transcript_path: Path) -> str:
    """One-line summary of what the session is doing: the most recent natural
    -language user message (skipping hook-feedback / shed-context noise),
    trimmed. Best-effort; returns "" if nothing usable.
    """
    if not transcript_path or not transcript_path.exists():
        return ""
    try:
        lines = transcript_path.read_text(errors="ignore").splitlines()
    except Exception:
        return ""

    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        msg = row.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _plain_text(msg.get("content"))
        if not text:
            continue
        if _is_noise(text):
            continue
        # First non-empty line, collapsed.
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return " ".join(first.split())[:200]
    return ""


def _plain_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts).strip()


def _is_noise(text: str) -> bool:
    """Skip hook-feedback, shed-context injections, and compact-guard blocks —
    none of those describe the user's actual task."""
    s = text.lstrip().lower()
    return (
        s.startswith("<shed-context")
        or "hook feedback" in s[:60]
        or s.startswith("[compact-guard")
        or s.startswith("caveat:")
    )


def _log_compact(trigger: str, wrote: bool, active_task: str) -> None:
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / "compactions.jsonl").open("a") as f:
            f.write(json.dumps(_now_compact_log_row(trigger, wrote, active_task)) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# settings.json wiring — mirrors statusline.install_to_claude_settings idiom.


def _hook_command() -> str:
    return f"{Path.home()}/.shed/hooks/precompact.sh"


def install_to_claude_settings(claude_settings_path: Path) -> bool:
    """Register the shed PreCompact hook in ~/.claude/settings.json.

    Returns True if changed, False if already wired. Fail-closed on parse
    error (don't trash a settings file we can't read)."""
    cmd = _hook_command()
    if not claude_settings_path.exists():
        data: dict = {}
    else:
        try:
            data = json.loads(claude_settings_path.read_text())
        except Exception:
            return False

    hooks = data.setdefault("hooks", {})
    pre = hooks.setdefault("PreCompact", [])
    for group in pre:
        for h in group.get("hooks", []) if isinstance(group, dict) else []:
            if isinstance(h, dict) and h.get("command") == cmd:
                return False  # already wired
    pre.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
    claude_settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def uninstall_from_claude_settings(claude_settings_path: Path) -> bool:
    """Remove the shed PreCompact hook. Returns True if removed."""
    cmd = _hook_command()
    if not claude_settings_path.exists():
        return False
    try:
        data = json.loads(claude_settings_path.read_text())
    except Exception:
        return False
    pre = data.get("hooks", {}).get("PreCompact")
    if not isinstance(pre, list):
        return False
    changed = False
    new_pre = []
    for group in pre:
        if isinstance(group, dict):
            kept = [h for h in group.get("hooks", []) if not (isinstance(h, dict) and h.get("command") == cmd)]
            if len(kept) != len(group.get("hooks", [])):
                changed = True
            if kept:
                new_pre.append({**group, "hooks": kept})
        else:
            new_pre.append(group)
    if not changed:
        return False
    if new_pre:
        data["hooks"]["PreCompact"] = new_pre
    else:
        data["hooks"].pop("PreCompact", None)
    claude_settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return True
