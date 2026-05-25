"""shed stats — session-level observability.

Collects three signals and writes one line to ~/.shed/stats.jsonl on demand
(called at session end by the Stop hook, and directly via ``shed stats``):

  injection_hit_rate  — fraction of injected memories that were cited back
                        (uses quality.jsonl, last 7 days)
  proposal_accept_rate — fraction of proposals user accepted vs rejected
                         (scans ~/.shed/proposals/*.md for [x]/[ ])
  top_injected         — top-5 memory slugs by injection count, last 7 days

Schema (one JSON object per line):
    {"ts": 1748000000, "injection_hit_rate": 0.42,
     "proposal_accept_rate": 0.80, "top_injected": [...],
     "injected_total": 12, "cited_total": 5,
     "proposals_accepted": 4, "proposals_rejected": 1}
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from shed.config import shed_home, state_dir

STATS_LOG = "stats.jsonl"
SEVEN_DAYS = 7 * 86400.0
DECAY_DAYS = 7.0


# ---------------------------------------------------------------------------
# collection helpers
# ---------------------------------------------------------------------------


def _injection_stats(window: float = SEVEN_DAYS) -> dict:
    """Read quality.jsonl; return hit-rate and top-5 slugs for the window."""
    path = state_dir() / "quality.jsonl"
    if not path.exists():
        return {"injection_hit_rate": None, "injected_total": 0, "cited_total": 0, "top_injected": []}

    now = time.time()
    cutoff = now - window
    decay_lambda = math.log(2) / (DECAY_DAYS * 86400)

    inj: dict[str, float] = {}
    cit: dict[str, float] = {}
    inj_count: dict[str, int] = {}

    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        ts = row.get("ts", 0.0)
        if ts < cutoff:
            continue
        mid = row.get("memory_id")
        ev = row.get("event")
        if not mid or not ev:
            continue
        age = max(0.0, now - ts)
        w = math.exp(-decay_lambda * age)
        if ev == "injected":
            inj[mid] = inj.get(mid, 0.0) + w
            inj_count[mid] = inj_count.get(mid, 0) + 1
        elif ev == "cited":
            cit[mid] = cit.get(mid, 0.0) + w

    injected_total = sum(inj_count.values())
    cited_total = len([m for m in cit if cit[m] > 0.1])

    hit_rate: float | None = None
    if inj:
        scores = {m: (cit.get(m, 0.0) + 0.5) / (inj[m] + 1.0) for m in inj}
        hit_rate = round(sum(scores.values()) / len(scores), 3)

    # top-5 by raw injection count
    top5 = sorted(inj_count.items(), key=lambda kv: kv[1], reverse=True)[:5]

    # resolve memory IDs → slugs
    from shed.memory import discover
    id_to_slug: dict[str, str] = {}
    try:
        for m in discover():
            id_to_slug[m.id] = m.slug
    except Exception:
        pass
    top_slugs = [id_to_slug.get(mid, mid[:10]) for mid, _ in top5]

    return {
        "injection_hit_rate": hit_rate,
        "injected_total": injected_total,
        "cited_total": cited_total,
        "top_injected": top_slugs,
    }


def _proposal_stats() -> dict:
    """Scan proposals dir; return accept/reject counts and rate."""
    proposals_dir = shed_home() / "proposals"
    if not proposals_dir.exists():
        return {"proposal_accept_rate": None, "proposals_accepted": 0, "proposals_rejected": 0}

    accepted = rejected = 0
    for p in proposals_dir.glob("*.md"):
        text = p.read_text()
        # Convention: proposal files are prepended with "accepted: true/false"
        # after user action, or we infer from the brief walk marking.
        if "accepted: true" in text or "status: accepted" in text:
            accepted += 1
        elif "accepted: false" in text or "status: rejected" in text:
            rejected += 1

    total = accepted + rejected
    rate: float | None = round(accepted / total, 3) if total > 0 else None

    return {
        "proposal_accept_rate": rate,
        "proposals_accepted": accepted,
        "proposals_rejected": rejected,
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def collect() -> dict:
    """Collect stats snapshot. Returns a dict ready for display and logging."""
    row: dict = {"ts": time.time()}
    row.update(_injection_stats())
    row.update(_proposal_stats())
    return row


def write_stats(row: dict | None = None) -> Path:
    """Append one stats row to stats.jsonl. Returns the path."""
    if row is None:
        row = collect()
    out = state_dir() / STATS_LOG
    state_dir().mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return out
