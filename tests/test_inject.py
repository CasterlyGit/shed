"""Injection tests using the deterministic hash embedder."""

import shutil
from pathlib import Path

from shed.config import Config
from shed.inject import format_block, inject_for_prompt
from shed.memory import discover

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(memdir):
    shutil.copy2(FIXTURES / "coding_prefs.md", memdir / "coding_prefs.md")
    shutil.copy2(FIXTURES / "git_workflow.md", memdir / "git_workflow.md")


def test_inject_returns_block_with_relevant_memory(memdir):
    _seed(memdir)
    res = inject_for_prompt("how should I run the tests for this Python project?")
    assert res.skipped_reason is None
    assert res.candidates  # we got candidates
    # Block contains shed-context wrapper.
    assert "<shed-context>" in res.block
    assert "</shed-context>" in res.block


def test_inject_skips_when_no_memories(memdir):
    res = inject_for_prompt("anything")
    assert res.skipped_reason == "no-memories"
    assert res.block == ""


def test_inject_skips_in_private_mode(memdir, monkeypatch):
    _seed(memdir)
    monkeypatch.setenv("SHED_MODE", "private")
    res = inject_for_prompt("test")
    assert res.skipped_reason and "private" in res.skipped_reason
    assert res.block == ""


def test_format_block_truncates_long_body(memdir):
    _seed(memdir)
    mems = discover()
    block = format_block(mems, max_chars=20)
    assert "..." in block


def test_inject_logs_to_jsonl(memdir, shed_home_path):
    _seed(memdir)
    inject_for_prompt("explain my git workflow")
    log = shed_home_path / "state" / "injections.jsonl"
    assert log.exists() and log.read_text().strip()


def test_min_score_filter_drops_low_relevance(memdir):
    _seed(memdir)
    cfg = Config()
    cfg.inject.min_score = 0.99  # nothing should clear this
    res = inject_for_prompt("totally unrelated banana phone", cfg=cfg)
    assert res.block == ""
    # Candidates are still recorded, just none chosen.
    assert all(not c.chosen for c in res.candidates)
