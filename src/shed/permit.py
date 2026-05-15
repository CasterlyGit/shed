"""shed permit — learn permission-prompt patterns.

The flow:

1. **Observe.** ``PreToolUse`` hook fires before every tool call. The hook
   script reads JSON from stdin (the tool name + args), canonicalizes the
   call into a pattern (e.g., ``Bash(shed *)``), and appends to
   ``~/.shed/state/permits-pending.jsonl``.

2. **Infer approval.** We can't directly observe "user clicked allow."
   Instead: if a ``PostToolUse`` fires within ~30s for the same tool
   invocation, we infer the call was approved. ``observe.py`` cross-
   references pending and writes to ``permits-approved.jsonl``.

3. **Propose.** After N approvals of the same canonical pattern (default
   3), a proposal markdown is written to ``~/.shed/proposals/permit-*.md``
   and surfaces in the next morning brief.

4. **Apply.** On user approval in ``shed brief``, the pattern is added to
   ``~/.claude/settings.json`` under ``permissions.allow``, with an atomic
   write + backup + JSON validation.

All operations are guarded by:
- A hard blocklist of destructive patterns (``Bash(rm *)``, ``Bash(sudo *)``,
  ``Bash(* --force *)``, plus the trivially-broad ``Bash(*)``, ``Read(*)``)
- An allowlist of directories under which Read/Edit/Write patterns are
  scoped (default: ``~/Documents/Dev/``, ``~/.claude/``, ``~/.shed/``).

Privacy: all logs stay local. Patterns are tool-call shapes, never content.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shed.config import (
    Config,
    claude_home,
    current_mode,
    is_globally_disabled,
    load_config,
    proposals_dir,
    state_dir,
)
from shed.memory import slugify

PENDING_LOG = "permits-pending.jsonl"
APPROVED_LOG = "permits-approved.jsonl"

# Hard blocklist — never propose these patterns, no matter how often approved.
BLOCKLIST_PATTERNS: list[str] = [
    "Bash(*)",
    "Bash(* *)",
    "Read(*)",
    "Edit(*)",
    "Write(*)",
    "Bash(rm *)",
    "Bash(rm -rf *)",
    "Bash(sudo *)",
    "Bash(* --force*)",
    "Bash(* -f *)",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(chmod *)",
    "Bash(chown *)",
    "Bash(dd *)",
    "Bash(mkfs*)",
]

# Default directories under which Read/Edit/Write patterns are valid candidates.
# Anything outside these prefixes won't be proposed as a directory-scoped permit.
DEFAULT_ALLOWLIST_DIRS: list[str] = [
    "~/Documents/Dev/",
    "~/.claude/",
    "~/.shed/",
    "/tmp/",
]


@dataclass
class PendingEvent:
    """A single PreToolUse observation."""

    ts: float
    tool_name: str
    canonical: str
    raw_args_summary: str  # short preview, not full content
    session_id: str | None = None


@dataclass
class PermitProposal:
    pattern: str
    count: int
    first_seen: str
    last_seen: str
    confidence: float
    path: Path


# ---------------------------------------------------------------------------
# Pattern canonicalization
# ---------------------------------------------------------------------------


_BASH_LEADING_WORD = re.compile(r"^\s*([A-Za-z0-9_./-]+)")


def canonicalize(tool_name: str, args: dict[str, Any]) -> str:
    """Turn a raw tool call into a canonical pattern string.

    Examples:
      Bash(command="shed why foo") → "Bash(shed why *)"
      Bash(command="git status")   → "Bash(git status)"
      Read(file_path="/Users/x/Documents/Dev/shed/README.md")
        → "Read(~/Documents/Dev/shed/**)"
    """
    tool = tool_name.strip()
    if tool == "Bash":
        cmd = (args.get("command") or "").strip()
        return f"Bash({_canonicalize_bash(cmd)})"
    if tool in ("Read", "Edit", "Write", "NotebookEdit", "NotebookRead"):
        path = (args.get("file_path") or args.get("notebook_path") or "").strip()
        return f"{tool}({_canonicalize_path(path)})"
    if tool == "Glob":
        # Glob has its own pattern — pass through.
        return f"Glob({(args.get('pattern') or '').strip()})"
    if tool == "Grep":
        # Grep's "pattern" is content; use path scope.
        path = (args.get("path") or "").strip()
        return f"Grep({_canonicalize_path(path)})" if path else "Grep()"
    # Default: just the tool name with a short args summary keys.
    keys = ",".join(sorted(args.keys())[:3])
    return f"{tool}({keys})"


def _canonicalize_bash(cmd: str) -> str:
    """Reduce a bash command line to its leading subcommand pattern."""
    if not cmd:
        return ""
    # Strip leading env prefix like "FOO=bar git status".
    parts = cmd.split()
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        parts = parts[1:]
    if not parts:
        return ""

    leading = parts[0]
    # Pipe, &&, || — only canonicalize the first command.
    if leading in ("(", "{"):
        if len(parts) > 1:
            leading = parts[1]
    if not _BASH_LEADING_WORD.match(leading):
        return ""

    # Common verb patterns: keep verb + subverb, wildcard the rest.
    if leading in ("git", "uv", "shed", "gh", "npm", "pnpm", "yarn", "pip", "brew", "cargo", "go"):
        if len(parts) >= 2 and not parts[1].startswith("-"):
            sub = parts[1]
            return f"{leading} {sub} *" if len(parts) > 2 else f"{leading} {sub}"
        return f"{leading} *"

    # Standalone unix utility — keep just the verb.
    return f"{leading} *" if len(parts) > 1 else leading


def _canonicalize_path(path: str) -> str:
    """Scope a file path to a directory wildcard rooted at the user's home."""
    if not path:
        return ""
    home = str(Path.home())
    p = str(Path(path).expanduser())
    if p.startswith(home + "/"):
        p = "~/" + p[len(home) + 1 :]

    # If the path looks like it's directly under a known dev-ish dir,
    # scope to that dir.
    for prefix in DEFAULT_ALLOWLIST_DIRS:
        pfx = prefix.rstrip("/")
        if p.startswith(pfx + "/"):
            # Find the next directory component below the allowlist dir.
            tail = p[len(pfx) + 1 :]
            next_dir = tail.split("/", 1)[0] if "/" in tail else tail
            return f"{pfx}/{next_dir}/**"
    # Otherwise return the parent dir + ** glob.
    parent = str(Path(p).parent)
    return f"{parent}/**"


def is_blocked(pattern: str) -> bool:
    """Return True if the pattern matches the hard blocklist."""
    if pattern in BLOCKLIST_PATTERNS:
        return True
    # Substring checks for the broad destructive verbs.
    for bad in ("rm -rf", "sudo ", "--force", "-rf "):
        if bad in pattern:
            return True
    # Bash with bare wildcard
    if pattern in ("Bash()", "Read()", "Write()", "Edit()"):
        return True
    return False


# ---------------------------------------------------------------------------
# Logging — called by PreToolUse hook
# ---------------------------------------------------------------------------


def log_pending(tool_name: str, args: dict[str, Any], session_id: str | None = None) -> None:
    """Append a pending PreToolUse event. Called from the hook script.

    Best-effort and bounded: drops silently on any error so the hook stays
    fast and never blocks tool execution.
    """
    try:
        if is_globally_disabled():
            return
        if current_mode() == "private":
            return
        cfg = load_config()
        if not cfg.permits.enabled:
            return

        canonical = canonicalize(tool_name, args)
        # Args summary: just keys + short snippet, never full values.
        keys_summary = ",".join(sorted(args.keys())[:5])
        ev = PendingEvent(
            ts=time.time(),
            tool_name=tool_name,
            canonical=canonical,
            raw_args_summary=keys_summary[:80],
            session_id=session_id,
        )
        state_dir().mkdir(parents=True, exist_ok=True)
        with (state_dir() / PENDING_LOG).open("a") as f:
            f.write(json.dumps(ev.__dict__) + "\n")
    except Exception:
        # Never let a permit-logging error block a tool call.
        return


def record_approval(
    tool_name: str,
    args: dict[str, Any],
    session_id: str | None = None,
    window_seconds: float = 30.0,
) -> bool:
    """If a recent PendingEvent matches this call, write to approved log.

    Called from observe.py (which already runs on PostToolUse). Returns True
    if a match was found and an approval was recorded.
    """
    try:
        if is_globally_disabled():
            return False
        canonical = canonicalize(tool_name, args)
        pending_path = state_dir() / PENDING_LOG
        if not pending_path.exists():
            return False

        now = time.time()
        threshold = now - window_seconds
        matched = False

        # Stream the pending log; find the most recent matching pending event.
        # Keep file intact — we don't need to truncate.
        with pending_path.open() as f:
            lines = f.readlines()

        for line in reversed(lines):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("ts", 0) < threshold:
                continue
            if row.get("canonical") == canonical and row.get("tool_name") == tool_name:
                matched = True
                _append_approved(canonical=canonical, tool_name=tool_name, when=now, session_id=session_id)
                break
        return matched
    except Exception:
        return False


def _append_approved(canonical: str, tool_name: str, when: float, session_id: str | None) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    row = {
        "ts": when,
        "tool_name": tool_name,
        "canonical": canonical,
        "session_id": session_id,
    }
    with (state_dir() / APPROVED_LOG).open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------


def load_approval_counts() -> dict[str, dict[str, Any]]:
    """Return ``{pattern: {count, first, last, recent_session_ids}}``."""
    path = state_dir() / APPROVED_LOG
    if not path.exists():
        return {}
    counts: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        pat = row.get("canonical")
        if not pat:
            continue
        entry = counts.setdefault(pat, {"count": 0, "first": row["ts"], "last": row["ts"], "sessions": set()})
        entry["count"] += 1
        entry["first"] = min(entry["first"], row["ts"])
        entry["last"] = max(entry["last"], row["ts"])
        if row.get("session_id"):
            entry["sessions"].add(row["session_id"])
    return counts


def already_proposed_patterns() -> set[str]:
    """Patterns we've already filed proposals for — don't duplicate."""
    out: set[str] = set()
    if not proposals_dir().exists():
        return out
    for p in proposals_dir().glob("permit-*.md"):
        try:
            text = p.read_text()
            m = re.search(r"^pattern:\s*(.+)$", text, re.MULTILINE)
            if m:
                out.add(m.group(1).strip())
        except Exception:
            continue
    return out


def existing_permissions() -> list[str]:
    """Read the live ``permissions.allow`` from settings.json. Empty on error."""
    s = claude_home() / "settings.json"
    if not s.exists():
        return []
    try:
        data = json.loads(s.read_text())
        perms = data.get("permissions", {})
        return list(perms.get("allow", []))
    except Exception:
        return []


def suggest(
    threshold: int | None = None,
    cfg: Config | None = None,
) -> list[dict[str, Any]]:
    """Return patterns that would be proposed at the current threshold.

    Each entry: ``{pattern, count, first, last, confidence}``.
    """
    cfg = cfg or load_config()
    t = threshold if threshold is not None else cfg.permits.threshold
    counts = load_approval_counts()
    already = already_proposed_patterns() | set(existing_permissions())

    out: list[dict[str, Any]] = []
    for pattern, entry in counts.items():
        if entry["count"] < t:
            continue
        if is_blocked(pattern):
            continue
        if pattern in already:
            continue
        # Confidence: 0.5 baseline, +0.1 per multiplied threshold (capped 0.95).
        ratio = entry["count"] / max(t, 1)
        confidence = min(0.5 + 0.1 * ratio, 0.95)
        out.append(
            {
                "pattern": pattern,
                "count": entry["count"],
                "first": entry["first"],
                "last": entry["last"],
                "confidence": confidence,
                "n_sessions": len(entry.get("sessions", [])),
            }
        )
    out.sort(key=lambda r: r["count"], reverse=True)
    return out


def generate_proposals(cfg: Config | None = None) -> list[PermitProposal]:
    """Write proposal markdown files for any patterns meeting the threshold.

    Returns the proposals that were newly written.
    """
    cfg = cfg or load_config()
    written: list[PermitProposal] = []
    for entry in suggest(cfg=cfg):
        pat = entry["pattern"]
        slug = slugify(pat)[:40] or "permit"
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = proposals_dir() / f"permit-{ts}-{slug}.md"
        if path.exists():
            continue
        first_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(entry["first"]))
        last_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(entry["last"]))
        body = _format_proposal_body(
            pattern=pat,
            count=entry["count"],
            first_iso=first_iso,
            last_iso=last_iso,
            confidence=entry["confidence"],
            n_sessions=entry["n_sessions"],
        )
        proposals_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        written.append(
            PermitProposal(
                pattern=pat,
                count=entry["count"],
                first_seen=first_iso,
                last_seen=last_iso,
                confidence=entry["confidence"],
                path=path,
            )
        )
    return written


def _format_proposal_body(
    pattern: str,
    count: int,
    first_iso: str,
    last_iso: str,
    confidence: float,
    n_sessions: int,
) -> str:
    return f"""---
kind: permit
pattern: {pattern}
count: {count}
first_seen: {first_iso}
last_seen: {last_iso}
confidence: {confidence:.2f}
n_sessions: {n_sessions}
---

Tarun approved `{pattern}` {count} times across {n_sessions} session(s) since {first_iso}.

Proposed addition to `~/.claude/settings.json`:

```diff
  "permissions": {{
    "allow": [
      "...",
+     "{pattern}"
    ]
  }}
```

Approving this means future calls matching `{pattern}` will run without asking.

Reject with `[n]` if this would be too broad; defer with `[s]` to re-evaluate later.
"""


# ---------------------------------------------------------------------------
# Application — write to ~/.claude/settings.json
# ---------------------------------------------------------------------------


def apply_proposal(proposal_path: Path) -> tuple[bool, str]:
    """Add the proposal's pattern to ``~/.claude/settings.json``.

    Returns ``(success, message)``. On any failure the original settings file
    is left untouched.
    """
    if not proposal_path.exists():
        return False, "proposal file missing"

    text = proposal_path.read_text()
    m = re.search(r"^pattern:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return False, "no pattern field in proposal"
    pattern = m.group(1).strip()

    if is_blocked(pattern):
        return False, f"refused — blocklisted pattern: {pattern}"

    settings_path = claude_home() / "settings.json"
    if not settings_path.exists():
        return False, "settings.json missing"

    # Backup first.
    bak = settings_path.with_suffix(
        f".json.shed-permit-bak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(settings_path, bak)

    try:
        data = json.loads(settings_path.read_text())
    except Exception as e:
        return False, f"settings.json unparseable: {e}"

    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if pattern in allow:
        return True, f"already in allow: {pattern}"
    allow.append(pattern)
    perms["allow"] = allow

    new_text = json.dumps(data, indent=2) + "\n"
    # Validate by re-parsing before writing.
    try:
        json.loads(new_text)
    except Exception as e:
        return False, f"refused — generated JSON invalid: {e}"

    # Atomic write: tmp + rename.
    tmp = settings_path.with_suffix(".json.shed-tmp")
    tmp.write_text(new_text)
    tmp.replace(settings_path)

    return True, f"added {pattern} (backup at {bak.name})"
