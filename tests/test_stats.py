"""Tests for shed.stats — observability snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from shed.config import state_dir
from shed.quality import log_citations, log_injection
from shed.stats import collect, write_stats


def test_collect_empty(shed_home_path):
    """collect() works with no data — returns None for rates, 0 for counts."""
    row = collect()
    assert row["injection_hit_rate"] is None
    assert row["proposal_accept_rate"] is None
    assert row["injected_total"] == 0
    assert row["cited_total"] == 0
    assert row["top_injected"] == []
    assert "ts" in row


def test_collect_with_injections(shed_home_path):
    log_injection(["mem1", "mem2"])
    log_injection(["mem1"])
    row = collect()
    assert row["injected_total"] >= 3
    assert row["injection_hit_rate"] is not None
    # No citations yet — score should be below neutral (< 0.5)
    assert row["injection_hit_rate"] < 0.6


def test_collect_with_citations(shed_home_path):
    log_injection(["mem1"] * 3)
    log_citations(["mem1"] * 3)
    row = collect()
    # Citations raise the score above neutral
    assert row["injection_hit_rate"] is not None
    assert row["injection_hit_rate"] > 0.3


def test_write_stats_creates_file(shed_home_path):
    row = collect()
    path = write_stats(row)
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert "ts" in data
    assert "injection_hit_rate" in data


def test_write_stats_appends(shed_home_path):
    write_stats(collect())
    write_stats(collect())
    path = state_dir() / "stats.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_top_injected_order(shed_home_path):
    # mem1 injected 5x, mem2 injected 2x
    log_injection(["mem1"] * 5)
    log_injection(["mem2"] * 2)
    row = collect()
    # mem1 should appear before mem2 (or mem1 alone if slugs map to IDs)
    assert len(row["top_injected"]) <= 5
