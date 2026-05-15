"""L5 — Self-tuning thresholds.

Every accept/reject in ``shed brief`` writes to ``threshold-feedback.jsonl``
with the kind of proposal and the confidence at the time. Periodically
(during evolve, or on-demand via ``shed thresholds tune``), we recompute
per-kind thresholds:

- If the user accepted most of what was proposed at confidence ≥ T, T is fine
  — maybe even drop it slightly (more proposals).
- If the user rejected most of what was proposed at confidence ≥ T, raise T
  (less noise).

The new threshold is the 75th percentile confidence of the last N accepts,
clamped to [min_threshold, max_threshold] (defaults [0.5, 0.95]).

Per-kind tuning lives in ``~/.shed/state/thresholds.json``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from shed.config import state_dir

FEEDBACK_LOG = "threshold-feedback.jsonl"
THRESHOLDS_FILE = "thresholds.json"

MIN_THRESHOLD = 0.5
MAX_THRESHOLD = 0.95
RECENT_N = 20


@dataclass
class ThresholdReport:
    kind: str
    threshold: float
    accepts: int
    rejects: int
    accept_rate: float
    reason: str


def log_feedback(kind: str, confidence: float, decision: str) -> None:
    """Record a single accept/reject decision. ``decision`` ∈ {accept, reject, skip}."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.time(),
            "kind": kind,
            "confidence": confidence,
            "decision": decision,
        }
        with (state_dir() / FEEDBACK_LOG).open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def load_thresholds() -> dict[str, float]:
    """Per-kind tuned thresholds. Empty dict means 'use config defaults'."""
    p = state_dir() / THRESHOLDS_FILE
    if not p.exists():
        return {}
    try:
        return {k: float(v) for k, v in json.loads(p.read_text()).items()}
    except Exception:
        return {}


def save_thresholds(values: dict[str, float]) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / THRESHOLDS_FILE).write_text(json.dumps(values, indent=2))


def get_threshold(kind: str, default: float) -> float:
    return load_thresholds().get(kind, default)


def tune_all(defaults: dict[str, float] | None = None) -> list[ThresholdReport]:
    """Recompute per-kind thresholds from the feedback log. Persists.

    ``defaults`` maps kind → fallback threshold (used when there's not enough
    data to tune). Common defaults: ``{"permit": 3, "lesson": 0.6, "merge": 0.92}``
    — but expressed as the *value used to filter*, not the count.
    """
    feedback_path = state_dir() / FEEDBACK_LOG
    if not feedback_path.exists():
        return []

    by_kind: dict[str, list[dict]] = {}
    for line in feedback_path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        by_kind.setdefault(row.get("kind", "unknown"), []).append(row)

    new_thresholds: dict[str, float] = load_thresholds()
    reports: list[ThresholdReport] = []
    defaults = defaults or {}

    for kind, rows in by_kind.items():
        recent = rows[-RECENT_N:]
        accepts = [r for r in recent if r.get("decision") == "accept"]
        rejects = [r for r in recent if r.get("decision") == "reject"]
        n = len(accepts) + len(rejects)
        if n < 5:
            continue  # not enough data to tune
        accept_rate = len(accepts) / n
        if accepts:
            confs = sorted(float(r.get("confidence", 0.5)) for r in accepts)
            # 75th percentile of accepted confidences.
            idx = int(len(confs) * 0.75)
            new_t = confs[min(idx, len(confs) - 1)]
        else:
            # All rejects → raise threshold above the highest seen reject conf.
            confs = [float(r.get("confidence", 0.5)) for r in rejects]
            new_t = max(confs) + 0.05 if confs else defaults.get(kind, 0.7)

        # Clamp.
        new_t = max(MIN_THRESHOLD, min(MAX_THRESHOLD, new_t))
        prev_t = new_thresholds.get(kind, defaults.get(kind, 0.7))

        # Heuristic: don't move >0.1 in one tuning pass.
        delta = new_t - prev_t
        if abs(delta) > 0.1:
            new_t = prev_t + (0.1 if delta > 0 else -0.1)

        new_thresholds[kind] = round(new_t, 3)
        reason = (
            f"accept_rate={accept_rate:.2f} over {n} decisions, "
            f"target {new_t:.2f} from prev {prev_t:.2f}"
        )
        reports.append(
            ThresholdReport(
                kind=kind,
                threshold=new_t,
                accepts=len(accepts),
                rejects=len(rejects),
                accept_rate=accept_rate,
                reason=reason,
            )
        )

    save_thresholds(new_thresholds)
    return reports
