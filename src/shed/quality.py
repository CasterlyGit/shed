"""L1 — Closed-loop injection quality.

Tracks per-memory metrics so future ranking can downweight memories that get
injected often but never actually cited in the response.

Schema (``~/.shed/state/quality.jsonl``):

    {"ts": 173..., "memory_id": "abc", "event": "injected"}
    {"ts": 173..., "memory_id": "abc", "event": "cited"}
    {"ts": 173..., "memory_id": "abc", "event": "followed_by_correction"}

Per-memory derived score:
    injected_recent = sum(injected events in last 30d, exp-decay)
    cited_recent    = sum(cited events in last 30d, exp-decay)
    injection_score = (cited_recent + 0.5) / (injected_recent + 1)   # smoothed
    # Bayesian-ish: a memory with no data starts at 0.5 (neutral),
    # earns its way up or down as evidence accumulates.

The injection ranker combines this with cosine similarity:
    final = cosine * (1 - quality_weight) + injection_score * quality_weight

``quality_weight`` defaults to 0.3 (cosine still dominates) but is configurable
and self-tunes based on accept/reject feedback (see L5 in shed.thresholds).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

from shed.config import state_dir

QUALITY_LOG = "quality.jsonl"
DEFAULT_DECAY_DAYS = 30.0


@dataclass
class MemoryQuality:
    memory_id: str
    injected_recent: float
    cited_recent: float
    injection_score: float  # 0..1


def log_event(memory_id: str, event: str, when: float | None = None) -> None:
    """Append a quality event. Errors are swallowed (best-effort)."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        row = {
            "ts": when if when is not None else time.time(),
            "memory_id": memory_id,
            "event": event,
        }
        with (state_dir() / QUALITY_LOG).open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def log_injection(memory_ids: list[str]) -> None:
    """One log call per injection turn. Cheap (one file open per turn)."""
    if not memory_ids:
        return
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        now = time.time()
        with (state_dir() / QUALITY_LOG).open("a") as f:
            for mid in memory_ids:
                f.write(json.dumps({"ts": now, "memory_id": mid, "event": "injected"}) + "\n")
    except Exception:
        pass


def log_citations(memory_ids: list[str]) -> None:
    if not memory_ids:
        return
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        now = time.time()
        with (state_dir() / QUALITY_LOG).open("a") as f:
            for mid in memory_ids:
                f.write(json.dumps({"ts": now, "memory_id": mid, "event": "cited"}) + "\n")
    except Exception:
        pass


def compute_scores(decay_days: float = DEFAULT_DECAY_DAYS) -> dict[str, MemoryQuality]:
    """Read the full quality log and compute per-memory scores.

    Exponential decay: a 30-day-old event has weight ~0.5; a 60-day-old
    event has weight ~0.25.
    """
    path = state_dir() / QUALITY_LOG
    if not path.exists():
        return {}

    now = time.time()
    decay_lambda = math.log(2) / (decay_days * 86400.0)  # half-life = decay_days

    inj: dict[str, float] = {}
    cit: dict[str, float] = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        mid = row.get("memory_id")
        ev = row.get("event")
        ts = row.get("ts", now)
        if not mid or not ev:
            continue
        age = max(0.0, now - ts)
        weight = math.exp(-decay_lambda * age)
        if ev == "injected":
            inj[mid] = inj.get(mid, 0.0) + weight
        elif ev == "cited":
            cit[mid] = cit.get(mid, 0.0) + weight

    out: dict[str, MemoryQuality] = {}
    for mid in set(inj) | set(cit):
        i_recent = inj.get(mid, 0.0)
        c_recent = cit.get(mid, 0.0)
        # Smoothed ratio so 0/0 → 0.5.
        score = (c_recent + 0.5) / (i_recent + 1.0)
        out[mid] = MemoryQuality(
            memory_id=mid,
            injected_recent=i_recent,
            cited_recent=c_recent,
            injection_score=min(max(score, 0.0), 1.0),
        )
    return out


def detect_citations_in_response(response_text: str, candidate_memories: list) -> list[str]:
    """Find which candidate memories were 'cited' in a response.

    Heuristic: a memory is cited if the response contains a substring of at
    least 12 contiguous chars from the memory body, OR the memory's title is
    mentioned. Intentionally permissive in the false-positive direction;
    the L1 loop is tolerant of noise.

    candidate_memories is a list of (id, body, title) tuples or Memory-like.
    """
    if not response_text:
        return []
    cited: list[str] = []
    for m in candidate_memories:
        # Duck-type for both Memory and tuple.
        if hasattr(m, "id"):
            mid = m.id
            body = m.body or ""
            title = m.title or ""
        else:
            mid, body, title = m
        if title and len(title) >= 6 and title.lower() in response_text.lower():
            cited.append(mid)
            continue
        # Sliding window over body for any 12+ char shared substring.
        body_lower = body.lower()
        resp_lower = response_text.lower()
        # Cheap: split body into 12-char chunks every 6 chars and check.
        found = False
        for i in range(0, max(0, len(body_lower) - 11), 6):
            chunk = body_lower[i : i + 12].strip()
            if len(chunk) >= 12 and chunk in resp_lower:
                found = True
                break
        if found:
            cited.append(mid)
    return cited
