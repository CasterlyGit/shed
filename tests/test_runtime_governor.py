"""S1-E2: governor — both clocks, pacing, dial, stale degradation."""

from __future__ import annotations

import json
import time

from shed.runtime import governor


def _write_rl(path, now, fh_used=10.0, fh_reset_in=3600, sd_used=5.0,
              sd_reset_in=3 * 86400, age=0):
    captured = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "captured_at": captured,
        "session_id": "t",
        "rate_limits": {
            "five_hour": {"used_percentage": fh_used, "resets_at": int(now + fh_reset_in)},
            "seven_day": {"used_percentage": sd_used, "resets_at": int(now + sd_reset_in)},
        },
    }))


def test_no_data_neutral_posture():
    r = governor.compute_report(now=1000.0)
    assert r["fresh"] is False
    assert r["wall_imminent"] is False
    assert r["fanout_scale"] == 1.0
    assert r["five_hour"] is None


def test_fresh_data_basic_fields():
    now = time.time()
    _write_rl(governor.rate_limits_path(), now, fh_used=42.0, sd_used=30.0)
    r = governor.compute_report(now=now)
    assert r["fresh"] is True
    assert r["five_hour"]["used_pct"] == 42.0
    assert r["five_hour"]["minutes_to_reset"] == 59  # 3600s, int floor
    assert r["seven_day"]["used_pct"] == 30.0


def test_wall_imminent_only_when_fresh():
    now = time.time()
    _write_rl(governor.rate_limits_path(), now, fh_used=97.0)
    assert governor.compute_report(now=now)["wall_imminent"] is True
    # Same numbers but stale → never a wall (reactive path owns the blind stretch).
    _write_rl(governor.rate_limits_path(), now, fh_used=97.0, age=900)
    r = governor.compute_report(now=now)
    assert r["fresh"] is False
    assert r["wall_imminent"] is False


def test_wall_zeroes_fanout():
    now = time.time()
    _write_rl(governor.rate_limits_path(), now, fh_used=96.0)
    assert governor.compute_report(now=now)["fanout_scale"] == 0.0


def test_near_wall_floors_fanout():
    now = time.time()
    _write_rl(governor.rate_limits_path(), now, fh_used=88.0)
    assert governor.compute_report(now=now)["fanout_scale"] <= 0.25


def test_behind_pace_scales_up():
    now = time.time()
    # Week 50% elapsed (reset 3.5d away), only 10% used → far behind → scale > 1.
    _write_rl(governor.rate_limits_path(), now, fh_used=10.0,
              sd_used=10.0, sd_reset_in=int(3.5 * 86400))
    r = governor.compute_report(now=now)
    assert r["pace_delta"] < -30
    assert r["fanout_scale"] > 1.0


def test_ahead_of_pace_scales_down():
    now = time.time()
    # Week 10% elapsed (reset 6.3d away), 60% already used → ahead → scale < 1.
    _write_rl(governor.rate_limits_path(), now, fh_used=10.0,
              sd_used=60.0, sd_reset_in=int(6.3 * 86400))
    r = governor.compute_report(now=now)
    assert r["pace_delta"] > 30
    assert r["fanout_scale"] < 1.0


def test_wall_pct_env_override(monkeypatch):
    now = time.time()
    monkeypatch.setenv("SHED_WALL_PCT", "80")
    _write_rl(governor.rate_limits_path(), now, fh_used=85.0)
    assert governor.compute_report(now=now)["wall_imminent"] is True


def test_tick_writes_report():
    now = time.time()
    _write_rl(governor.rate_limits_path(), now)
    governor.tick(now)
    saved = json.loads(governor.report_path().read_text())
    assert saved["computed_at"] == int(now)
