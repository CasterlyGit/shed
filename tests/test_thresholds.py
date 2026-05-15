"""Tests for L5 self-tuning thresholds."""

from __future__ import annotations

from shed.thresholds import (
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    get_threshold,
    load_thresholds,
    log_feedback,
    save_thresholds,
    tune_all,
)


def test_no_feedback_returns_empty(shed_home_path):
    assert tune_all() == []
    assert load_thresholds() == {}


def test_get_threshold_falls_back_to_default(shed_home_path):
    assert get_threshold("nonexistent", 0.6) == 0.6


def test_save_load_roundtrip(shed_home_path):
    save_thresholds({"permit": 0.85, "lesson": 0.6})
    loaded = load_thresholds()
    assert loaded["permit"] == 0.85
    assert loaded["lesson"] == 0.6


def test_few_decisions_skipped(shed_home_path):
    for _ in range(3):
        log_feedback("permit", 0.7, "accept")
    reports = tune_all()
    assert reports == [], "should not tune with <5 decisions"


def test_high_accept_rate_lowers_threshold(shed_home_path):
    # Save initial high threshold.
    save_thresholds({"permit": 0.9})
    # 10 accepts at low confidence — user happily accepting things below 0.9.
    for _ in range(10):
        log_feedback("permit", 0.55, "accept")
    reports = tune_all()
    assert reports
    # Threshold should have dropped (clamped to -0.1 max move per cycle).
    new_t = load_thresholds()["permit"]
    assert new_t < 0.9
    assert new_t >= MIN_THRESHOLD


def test_all_rejects_raises_threshold(shed_home_path):
    save_thresholds({"permit": 0.6})
    for _ in range(8):
        log_feedback("permit", 0.7, "reject")
    tune_all()
    new_t = load_thresholds()["permit"]
    assert new_t > 0.6
    assert new_t <= MAX_THRESHOLD


def test_clamps_to_bounds(shed_home_path):
    save_thresholds({"permit": 0.95})
    for _ in range(10):
        log_feedback("permit", 0.99, "reject")
    tune_all()
    assert load_thresholds()["permit"] <= MAX_THRESHOLD


def test_per_kind_independent(shed_home_path):
    for _ in range(10):
        log_feedback("permit", 0.6, "accept")
        log_feedback("lesson", 0.85, "reject")
    save_thresholds({"permit": 0.7, "lesson": 0.7})
    tune_all()
    th = load_thresholds()
    # permit accepted lots → should not have risen
    assert th["permit"] <= 0.7
    # lesson rejected → should have risen (or held at upper bound)
    assert th["lesson"] >= 0.7
