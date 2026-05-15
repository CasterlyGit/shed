"""shed statusline — small glyph in Claude Code's statusline.

Format: ``[shed: ●●●○ 3↓ ✓2]``
- ``●●●○`` — memory health quartile (full=hot+well-organized, empty=stale)
- ``3↓`` — 3 proposals pending in morning brief
- ``✓2`` — 2 actions auto-applied today

Cached in ``~/.shed/state/statusline.txt``. The Claude Code statusline
configuration just runs ``cat`` on this file (zero latency for the agent).
A daily Stop hook refreshes it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from shed.config import changelog_path, proposals_dir, state_dir
from shed.memory import discover

STATUSLINE_FILE = "statusline.txt"


def memory_health_quartile() -> int:
    """Return 0-4 based on memory liveness.

    0 = no memories at all, 4 = lots of recently-cited hot memories.
    """
    mems = discover()
    if not mems:
        return 0
    cutoff = (datetime.now().astimezone() - timedelta(days=30))
    cited_recent = 0
    for m in mems:
        if not m.last_cited_at:
            continue
        try:
            ts = datetime.fromisoformat(m.last_cited_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=cutoff.tzinfo)
            if ts >= cutoff:
                cited_recent += 1
        except Exception:
            continue
    ratio = cited_recent / len(mems)
    if ratio >= 0.5:
        return 4
    if ratio >= 0.3:
        return 3
    if ratio >= 0.15:
        return 2
    if ratio > 0:
        return 1
    return 0


def pending_count() -> int:
    if not proposals_dir().exists():
        return 0
    return sum(1 for _ in proposals_dir().glob("*.md"))


def applied_today() -> int:
    """Count changelog entries from today (rough — string match on date prefix)."""
    p = changelog_path()
    if not p.exists():
        return 0
    today = date.today().isoformat()
    count = 0
    for line in p.read_text().splitlines():
        if today in line and line.startswith("-"):
            count += 1
    return count


def render() -> str:
    """The string the statusline displays."""
    q = memory_health_quartile()
    pending = pending_count()
    applied = applied_today()
    bar = "●" * q + "○" * (4 - q)
    parts = [f"shed:{bar}"]
    if pending:
        parts.append(f"{pending}↓")
    if applied:
        parts.append(f"✓{applied}")
    return "[" + " ".join(parts) + "]"


def write_cache() -> Path:
    """Render and write to the cache file."""
    state_dir().mkdir(parents=True, exist_ok=True)
    p = state_dir() / STATUSLINE_FILE
    p.write_text(render())
    return p


def read_cache() -> str:
    """Read cached text. Empty string if cache missing — Claude Code falls
    through to its default statusline."""
    p = state_dir() / STATUSLINE_FILE
    if not p.exists():
        return ""
    try:
        return p.read_text().strip()
    except Exception:
        return ""


def install_to_claude_settings(claude_settings_path: Path) -> bool:
    """Wire up the statusline in ~/.claude/settings.json.

    Backs up the current statusLine field if present. Sets to a script that
    cats the cache file. Returns True if changed, False if already wired.
    """
    import json

    cache_path = state_dir() / STATUSLINE_FILE
    cmd = f"cat {cache_path} 2>/dev/null"

    if not claude_settings_path.exists():
        data: dict = {}
    else:
        try:
            data = json.loads(claude_settings_path.read_text())
        except Exception:
            return False

    existing = data.get("statusLine")
    if isinstance(existing, dict) and existing.get("command") == cmd:
        return False  # already wired
    if isinstance(existing, str) and existing == cmd:
        return False

    # Backup any existing statusline.
    if existing:
        data["statusLine_pre_shed_backup"] = existing
    data["statusLine"] = {"type": "command", "command": cmd}
    claude_settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def uninstall_from_claude_settings(claude_settings_path: Path) -> bool:
    """Remove shed statusline; restore backup if present."""
    import json

    if not claude_settings_path.exists():
        return False
    try:
        data = json.loads(claude_settings_path.read_text())
    except Exception:
        return False
    cache_path = state_dir() / STATUSLINE_FILE
    cmd = f"cat {cache_path} 2>/dev/null"
    sl = data.get("statusLine")
    is_ours = (
        (isinstance(sl, dict) and sl.get("command") == cmd)
        or (isinstance(sl, str) and sl == cmd)
    )
    if not is_ours:
        return False
    backup = data.pop("statusLine_pre_shed_backup", None)
    if backup:
        data["statusLine"] = backup
    else:
        data.pop("statusLine", None)
    claude_settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return True
