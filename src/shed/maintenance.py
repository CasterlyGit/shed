"""Self-maintenance — keep shed's own state honest and bounded.

shed is a long-running background system: its append-only state logs
(``injections.jsonl``, ``quality.jsonl``, ``corrections.jsonl``,
``permits-*.jsonl``, ``stats.jsonl``) grow forever, and the proposal queue can
fill with junk. Left alone, a months-old install reads multi-MB files on the
inject hot path. This module is the opposing force:

* ``groom_logs`` — bound each rotating log to its last N lines (newest wins).
  Old events are already decayed to ~0 weight by the quality/stats math, so
  trimming them is loss-free for ranking and a real latency win.
* ``find_junk_proposals`` — flag oversized / stale proposals so the brief
  doesn't drown in noise.
* ``build_report`` — one honest verdict: is shed earning its keep?

Everything is local, deterministic, fail-open, and idempotent.
"""

from __future__ import annotations

import time
from pathlib import Path

from shed.config import proposals_dir, state_dir

# Append-only logs that rotate. Order is stable for reporting.
ROTATING_LOGS: tuple[str, ...] = (
    "injections.jsonl",
    "quality.jsonl",
    "corrections.jsonl",
    "decisions.jsonl",
    "stats.jsonl",
    "permits-pending.jsonl",
    "permits-approved.jsonl",
    "threshold-feedback.jsonl",
)

# Keep this many recent lines per log. quality.jsonl uses a 30-day half-life and
# stats a 7-day window, so far-older lines contribute ~nothing — safe to drop.
DEFAULT_CAP_LINES = 5000
# Only bother rewriting a file once it's meaningfully over the cap.
_GROOM_SLACK = 1000


def log_sizes() -> dict[str, int]:
    """Byte size of every rotating log that exists."""
    sd = state_dir()
    out: dict[str, int] = {}
    for name in ROTATING_LOGS:
        p = sd / name
        if p.exists():
            try:
                out[name] = p.stat().st_size
            except OSError:
                out[name] = 0
    return out


def _trim_file(path: Path, cap_lines: int) -> int:
    """Rewrite ``path`` to its last ``cap_lines`` lines. Returns bytes freed.

    Atomic (write tmp, then replace). Fail-open: returns 0 on any error.
    """
    try:
        before = path.stat().st_size
        lines = path.read_text(errors="ignore").splitlines()
        if len(lines) <= cap_lines + _GROOM_SLACK:
            return 0
        kept = lines[-cap_lines:]
        tmp = path.with_suffix(path.suffix + ".groom-tmp")
        tmp.write_text("\n".join(kept) + "\n")
        tmp.replace(path)
        after = path.stat().st_size
        return max(0, before - after)
    except Exception:
        return 0


def groom_logs(cap_lines: int = DEFAULT_CAP_LINES) -> dict[str, int]:
    """Trim every over-cap rotating log to its last ``cap_lines`` lines.

    Returns ``{name: bytes_freed}`` for logs that were actually trimmed.
    """
    sd = state_dir()
    freed: dict[str, int] = {}
    for name in ROTATING_LOGS:
        p = sd / name
        if not p.exists():
            continue
        n = _trim_file(p, cap_lines)
        if n > 0:
            freed[name] = n
    return freed


def maybe_groom(cap_lines: int = DEFAULT_CAP_LINES, high_water_bytes: int = 2_000_000) -> dict[str, int]:
    """Cheap session-end hook: only walk + trim if some log is genuinely large.

    A single ``stat`` per log gates the (more expensive) read+rewrite, so the
    common case (small logs) costs almost nothing. Returns bytes freed per log.
    """
    sizes = log_sizes()
    if not any(b >= high_water_bytes for b in sizes.values()):
        return {}
    return groom_logs(cap_lines)


def find_junk_proposals(max_bytes: int = 4000, stale_days: float = 30.0) -> list[dict]:
    """Flag proposals that are almost certainly noise.

    A real lesson/permit/skill proposal is small and recent. A multi-KB blob
    (a pasted tweet/log that tripped the correction prefilter) or a month-old
    untouched proposal is junk worth purging. Returns a list of
    ``{path, reason, bytes}`` dicts — never deletes; the caller/brief decides.
    """
    pdir = proposals_dir()
    if not pdir.exists():
        return []
    now = time.time()
    out: list[dict] = []
    for p in sorted(pdir.glob("*.md")):
        try:
            size = p.stat().st_size
            age_days = (now - p.stat().st_mtime) / 86400.0
        except OSError:
            continue
        reasons = []
        if size > max_bytes:
            reasons.append(f"oversized ({size}B > {max_bytes}B)")
        if age_days > stale_days:
            reasons.append(f"stale ({age_days:.0f}d)")
        if reasons:
            out.append({"path": p, "reason": ", ".join(reasons), "bytes": size})
    return out


def build_report() -> dict:
    """Synthesize an honest health snapshot + a plain-English verdict.

    Pulls the existing stats snapshot, log sizes, pending/junk proposal counts,
    and turns them into a single ``verdict`` string so the user gets a yes/no on
    "is shed helping?" without reading five dashboards.
    """
    from shed.stats import collect

    snap = collect()
    sizes = log_sizes()
    total_log_bytes = sum(sizes.values())
    junk = find_junk_proposals()
    pdir = proposals_dir()
    pending = len(list(pdir.glob("*.md"))) if pdir.exists() else 0

    hit = snap.get("injection_hit_rate")
    acc = snap.get("proposal_accept_rate")

    # Verdict: weigh the two real signals shed has about its own value.
    bits: list[str] = []
    if hit is None:
        bits.append("no injection data yet")
    elif hit >= 0.4:
        bits.append(f"memories land ({hit:.0%} hit-rate)")
    elif hit >= 0.2:
        bits.append(f"injection is lukewarm ({hit:.0%}) — consider `shed evolve`")
    else:
        bits.append(f"injection is noisy ({hit:.0%}) — prune memories")

    if acc is not None:
        bits.append(f"you accept {acc:.0%} of proposals")
    if junk:
        bits.append(f"{len(junk)} junk proposal(s) — run `shed groom --proposals`")
    if total_log_bytes > 5_000_000:
        bits.append(f"state logs are {total_log_bytes // 1_000_000}MB — run `shed groom`")

    healthy = (hit is None or hit >= 0.2) and total_log_bytes < 10_000_000
    verdict = ("✓ shed is earning its keep — " if healthy else "⚠ shed needs attention — ") + "; ".join(bits)

    return {
        "verdict": verdict,
        "healthy": healthy,
        "injection_hit_rate": hit,
        "proposal_accept_rate": acc,
        "injected_total": snap.get("injected_total", 0),
        "cited_total": snap.get("cited_total", 0),
        "proposals_pending": pending,
        "proposals_accepted": snap.get("proposals_accepted", 0),
        "proposals_rejected": snap.get("proposals_rejected", 0),
        "junk_proposals": len(junk),
        "top_injected": snap.get("top_injected", []),
        "log_sizes": sizes,
        "log_bytes_total": total_log_bytes,
    }
