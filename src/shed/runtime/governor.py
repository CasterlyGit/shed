"""S1-E2 · Budget governor.

Knows BOTH clocks — the 5-hour rolling window and the weekly reset (~Sunday
10:00) — and turns them into one report consumers act on:

- ``wall_imminent``  → resumed pauses headless runs cleanly (proactive path)
- ``fanout_scale``   → S2 scales agent fan-out ("scale to the budget the
                       governor reports")
- ``pace_delta``     → are we under- or over-spending the week?

Locked-in model (Tarun, 2026-05-31): per-session token burn is the GATE
(compact-guard owns that); the quota here is only a DIAL — it scales ambition
up/down but never gates a session by itself. ``wall_imminent`` is not a gate
on *work*, it is the checkpoint signal so the wall never lands mid-task.

Data plane: ``~/.claude/state/rate-limits.json`` (statusline capture hook +
mitm sniffer both write it). Trusted only while fresh; when stale we degrade
to neutral — and we NEVER declare a wall imminent on stale data. The blind
stretch (headless runs don't tick the statusline) is covered by the reactive
path in ``resumed``: a run that dies on the wall is parsed and re-queued.
"""

from __future__ import annotations

import calendar
import datetime
import os
from pathlib import Path

from shed.config import claude_home, state_dir

from . import atomic_write_json, now_epoch, read_json

FRESH_S = 300            # trust rate-limits.json only this long (matches hooks)
WALL_PCT = 95.0          # 5h used% at which resumed checkpoints active runs
NEAR_WALL_PCT = 85.0     # start shrinking fan-out here
WEEK_S = 7 * 86400
FIVE_H_S = 5 * 3600


def rate_limits_path() -> Path:
    return claude_home() / "state" / "rate-limits.json"


def report_path() -> Path:
    return state_dir() / "governor.json"


def _parse_captured_at(s: str) -> float:
    return calendar.timegm(
        datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").timetuple()
    )


def read_rate_limits(now: float | None = None) -> tuple[dict | None, float]:
    """Return (payload, age_seconds). payload=None if missing/unparseable."""
    now = now or now_epoch()
    d = read_json(rate_limits_path())
    if not d or "rate_limits" not in d:
        return None, float("inf")
    try:
        age = now - _parse_captured_at(d.get("captured_at", ""))
    except Exception:
        return None, float("inf")
    return d, age


def _wall_pct() -> float:
    try:
        return float(os.environ.get("SHED_WALL_PCT", WALL_PCT))
    except ValueError:
        return WALL_PCT


def compute_report(now: float | None = None) -> dict:
    """Build the governor report. Pure function of rate-limits.json + clock."""
    now = now or now_epoch()
    data, age = read_rate_limits(now)
    fresh = data is not None and age <= FRESH_S

    report: dict = {
        "computed_at": int(now),
        "fresh": fresh,
        "data_age_s": None if age == float("inf") else int(age),
        "wall_pct": _wall_pct(),
    }

    if data is None:
        # No data at all: neutral posture, never wall, mid fan-out.
        report.update(
            five_hour=None, seven_day=None,
            wall_imminent=False, fanout_scale=1.0, pace_delta=None,
        )
        return report

    rl = data.get("rate_limits", {})
    fh = rl.get("five_hour", {}) or {}
    sd = rl.get("seven_day", {}) or {}

    fh_used = float(fh.get("used_percentage") or 0.0)
    fh_reset = int(fh.get("resets_at") or 0)
    sd_used = float(sd.get("used_percentage") or 0.0)
    sd_reset = int(sd.get("resets_at") or 0)

    report["five_hour"] = {
        "used_pct": round(fh_used, 1),
        "resets_at": fh_reset,
        "minutes_to_reset": max(0, int((fh_reset - now) / 60)) if fh_reset else None,
    }

    # Weekly pacing: how far through the week are we vs how much is spent?
    # pace_delta < 0 → behind (under-spending) → scale ambition UP.
    pace_delta = None
    elapsed_frac = None
    if sd_reset:
        elapsed_frac = min(1.0, max(0.0, (now - (sd_reset - WEEK_S)) / WEEK_S))
        pace_delta = round(sd_used - elapsed_frac * 100.0, 1)
    report["seven_day"] = {
        "used_pct": round(sd_used, 1),
        "resets_at": sd_reset,
        "minutes_to_reset": max(0, int((sd_reset - now) / 60)) if sd_reset else None,
        "elapsed_fraction": round(elapsed_frac, 3) if elapsed_frac is not None else None,
    }
    report["pace_delta"] = pace_delta

    # Wall detection: ONLY on fresh data (stale → reactive path owns it).
    wall = bool(fresh and fh_used >= report["wall_pct"])
    report["wall_imminent"] = wall

    # Fan-out dial. Base 1.0; push up when the week is under-spent, pull down
    # as the 5h window fills; floor at 0.25 near the wall, 0 at the wall.
    scale = 1.0
    if pace_delta is not None:
        scale += max(-0.5, min(1.0, -pace_delta / 50.0))
    if fh_used >= report["wall_pct"]:
        scale = 0.0
    elif fh_used >= NEAR_WALL_PCT:
        scale = min(scale, 0.25)
    elif fh_used >= 70.0:
        scale = min(scale, 0.6)
    if not fresh:
        scale = min(scale, 1.0)  # stale data never justifies extra ambition
    report["fanout_scale"] = round(max(0.0, min(2.0, scale)), 2)

    return report


def write_report(report: dict) -> Path:
    p = report_path()
    atomic_write_json(p, report)
    return p


def tick(now: float | None = None) -> dict:
    report = compute_report(now)
    write_report(report)
    return report
