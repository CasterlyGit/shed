"""Tests for the L1 closed-loop injection quality module."""

from __future__ import annotations

import time

from shed.quality import (
    compute_scores,
    detect_citations_in_response,
    log_citations,
    log_event,
    log_injection,
)


def test_no_events_returns_empty(shed_home_path):
    assert compute_scores() == {}


def test_injection_alone_drives_score_below_neutral(shed_home_path):
    log_injection(["mem1"] * 5)
    scores = compute_scores()
    assert "mem1" in scores
    # 0 cited / 5 injected → smoothed 0.5/6 ≈ 0.08
    assert scores["mem1"].injection_score < 0.2


def test_citation_pulls_score_up(shed_home_path):
    log_injection(["mem1"] * 3)
    log_citations(["mem1"] * 3)
    scores = compute_scores()
    # 3 cited / 3 injected → smoothed 3.5/4 ≈ 0.875
    assert scores["mem1"].injection_score > 0.7


def test_unrelated_memories_isolated(shed_home_path):
    log_injection(["mem1"] * 5)
    log_injection(["mem2"] * 1)
    log_citations(["mem2"] * 1)
    scores = compute_scores()
    assert scores["mem1"].injection_score < scores["mem2"].injection_score


def test_decay_old_events_have_less_weight(shed_home_path):
    # Event from 60 days ago.
    old = time.time() - 60 * 86400
    log_event("mem_old", "injected", when=old)
    log_event("mem_old", "injected", when=old)
    # Recent event for comparison.
    log_event("mem_new", "injected")
    scores = compute_scores(decay_days=30.0)
    # Old memory's injected_recent should be heavily decayed (~0.5 total weight).
    assert scores["mem_old"].injected_recent < scores["mem_new"].injected_recent * 1.5


def test_detect_citations_finds_title_match():
    candidates = [("a", "long body content here", "DSA Tutoring Style")]
    cited = detect_citations_in_response("Per your DSA tutoring style preference …", candidates)
    assert "a" in cited


def test_detect_citations_finds_substring_match():
    body = "always use uv pip install for python projects"
    candidates = [("a", body, "x")]
    cited = detect_citations_in_response("I should always use uv pip install here", candidates)
    assert "a" in cited


def test_detect_citations_no_false_positive_on_short_overlap():
    # Just word "python" — too short, should not count.
    candidates = [("a", "python", "Python notes")]
    cited = detect_citations_in_response("I love python", candidates)
    # Title match is case-insensitive but >=6 chars; "Python notes" doesn't appear
    # and body "python" is too short for substring match.
    assert "a" not in cited


def test_detect_citations_handles_empty_response():
    candidates = [("a", "anything", "Any")]
    assert detect_citations_in_response("", candidates) == []
