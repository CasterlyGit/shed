"""Tests for the optional Haiku judge.

We never call the real API here — we test the budget tracking and the
fall-through paths. End-to-end with the API is a manual smoke test.
"""

from __future__ import annotations

from shed.judge import (
    DEFAULT_DAILY_CAP,
    JudgeVerdict,
    _decrement_budget,
    get_remaining_budget,
    judge,
)


def test_no_api_key_returns_non_correction(shed_home_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    v = judge("anything")
    assert isinstance(v, JudgeVerdict)
    assert v.is_correction is False
    assert v.used_budget is False
    assert "no API key" in v.rationale


def test_budget_starts_full(shed_home_path):
    assert get_remaining_budget() == DEFAULT_DAILY_CAP


def test_decrement_reduces_budget(shed_home_path):
    initial = get_remaining_budget()
    _decrement_budget()
    assert get_remaining_budget() == initial - 1
    _decrement_budget()
    _decrement_budget()
    assert get_remaining_budget() == initial - 3


def test_budget_resets_on_new_day(shed_home_path, monkeypatch):
    # Spend some budget today.
    _decrement_budget()
    _decrement_budget()
    initial_after = get_remaining_budget()

    # Mock today() to return a different date.

    import shed.judge as judge_mod

    class _NextDay:
        def isoformat(self):
            return "2099-01-01"

    monkeypatch.setattr(judge_mod, "_today", lambda: "2099-01-01")
    # Budget should have reset.
    assert get_remaining_budget() == DEFAULT_DAILY_CAP
    assert get_remaining_budget() != initial_after


def test_budget_exhausted_short_circuits(shed_home_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Burn the whole budget.
    for _ in range(DEFAULT_DAILY_CAP):
        _decrement_budget()
    assert get_remaining_budget() == 0
    v = judge("durable correction text")
    assert v.is_correction is False
    assert v.used_budget is False
    assert "budget" in v.rationale.lower()
