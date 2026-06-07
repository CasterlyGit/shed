"""Tests for shed.observe_procedure — passive procedure capture.

All tests are deterministic, hermetic, and zero-cost (no LLM calls, no network).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from shed.observe_procedure import (
    PROTECTED_CLOSE,
    PROTECTED_OPEN,
    ProcedureSignal,
    _distill_tool_sequence,
    _make_description,
    _seen_count,
    _skill_markdown_from_signal,
    _write_procedure_proposal,
    detect_procedure,
    observe_procedure,
)


# ---------------------------------------------------------------------------
# Transcript fixture builder
# ---------------------------------------------------------------------------


def _write_transcript(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def _tool_use_row(tool_name: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": tool_name, "id": "x", "input": {}}],
        },
    }


def _user_row(text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_row(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


# ---------------------------------------------------------------------------
# detect_procedure: basic detection
# ---------------------------------------------------------------------------


def test_detects_procedure_with_enough_tools(tmp_path):
    rows = [
        _user_row("Deploy the app to Fly.io"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Done. The app is deployed successfully."),
    ]
    t = _write_transcript(tmp_path, rows)
    sig = detect_procedure(t)
    assert sig is not None
    assert sig.success is True
    assert sig.confidence > 0.5
    assert "Deploy" in sig.task


def test_returns_none_below_burst_threshold(tmp_path):
    rows = [
        _user_row("Deploy the app"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Done."),
    ]
    t = _write_transcript(tmp_path, rows)
    # Default MIN_TOOL_BURST=3; only 2 here.
    sig = detect_procedure(t, min_tool_burst=3)
    assert sig is None


def test_passes_with_min_burst_override(tmp_path):
    rows = [
        _user_row("Run the tests please"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("All tests passed."),
    ]
    t = _write_transcript(tmp_path, rows)
    sig = detect_procedure(t, min_tool_burst=2)
    assert sig is not None


def test_no_goal_statement_returns_none(tmp_path):
    rows = [
        _user_row("ok"),  # no imperative language
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Done."),
    ]
    t = _write_transcript(tmp_path, rows)
    sig = detect_procedure(t)
    assert sig is None


def test_edit_tools_count_as_work(tmp_path):
    rows = [
        _user_row("Refactor the config module"),
        _tool_use_row("Edit"),
        _tool_use_row("Edit"),
        _tool_use_row("Write"),
        _assistant_row("Refactoring complete."),
    ]
    t = _write_transcript(tmp_path, rows)
    sig = detect_procedure(t)
    assert sig is not None
    assert "Edit" in sig.tool_calls


def test_non_work_tools_not_counted(tmp_path):
    rows = [
        _user_row("Build the module please"),
        _tool_use_row("Read"),    # read-only, not a work tool
        _tool_use_row("Read"),
        _tool_use_row("Read"),
        _assistant_row("Here is the info."),
    ]
    t = _write_transcript(tmp_path, rows)
    sig = detect_procedure(t)
    assert sig is None  # Read is not in _WORK_TOOLS


def test_missing_transcript_returns_none(tmp_path):
    sig = detect_procedure(tmp_path / "nonexistent.jsonl")
    assert sig is None


def test_failure_language_lowers_success_flag(tmp_path):
    rows = [
        _user_row("Deploy to Fly.io"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Error: permission denied. Could not deploy."),
    ]
    t = _write_transcript(tmp_path, rows)
    sig = detect_procedure(t)
    # Signal exists (enough tool calls + goal) but success should be False
    assert sig is not None
    assert sig.success is False


# ---------------------------------------------------------------------------
# _distill_tool_sequence
# ---------------------------------------------------------------------------


def test_distill_collapses_consecutive_duplicates():
    calls = ["Bash", "Bash", "Bash", "Edit", "Bash"]
    result = _distill_tool_sequence(calls)
    assert "Bash ×3" in result
    assert "Edit" in result
    assert "Bash" in result  # last lone Bash


def test_distill_single_tool():
    result = _distill_tool_sequence(["Bash"])
    assert "Bash" in result
    assert "×" not in result


def test_distill_empty():
    result = _distill_tool_sequence([])
    assert "no tool calls" in result


# ---------------------------------------------------------------------------
# _skill_markdown_from_signal
# ---------------------------------------------------------------------------


def test_skill_markdown_has_required_sections():
    sig = ProcedureSignal(
        task="Deploy app to Fly.io",
        tool_calls=["Bash", "Bash", "Bash"],
        success=True,
        confidence=0.75,
    )
    md = _skill_markdown_from_signal(sig, slug="deploy-app-to-fly-io")
    assert "name: deploy-app-to-fly-io" in md
    assert "source: shed-passive" in md
    assert PROTECTED_OPEN in md
    assert PROTECTED_CLOSE in md
    assert "Deploy app to Fly.io" in md
    assert "confidence: 0.75" in md


def test_skill_markdown_description_is_trigger_rich():
    desc = _make_description("run the full test suite")
    assert "Use when you need to" in desc
    assert "run the full test suite" in desc


def test_skill_markdown_protected_section_present():
    sig = ProcedureSignal(
        task="Set up CI pipeline",
        tool_calls=["Bash", "Edit", "Write"],
        success=True,
        confidence=0.8,
    )
    md = _skill_markdown_from_signal(sig, slug="set-up-ci-pipeline")
    # The protected block must wrap the approach section
    protected_start = md.index(PROTECTED_OPEN)
    protected_end = md.index(PROTECTED_CLOSE)
    assert protected_start < protected_end


# ---------------------------------------------------------------------------
# _write_procedure_proposal
# ---------------------------------------------------------------------------


def test_writes_kind_skill_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))

    sig = ProcedureSignal(
        task="Build and push Docker image",
        tool_calls=["Bash", "Bash", "Bash"],
        success=True,
        confidence=0.80,
    )
    slug = "build-and-push-docker-image"
    path = _write_procedure_proposal(sig, slug)
    assert path.exists()
    body = path.read_text()
    assert "kind: skill" in body
    assert "name: build-and-push-docker-image" in body
    assert "source: shed-passive" in body
    assert "SKILL-BODY-START" in body
    assert "SKILL-BODY-END" in body


def test_dedup_does_not_write_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))

    sig = ProcedureSignal(
        task="Build and push Docker image",
        tool_calls=["Bash", "Bash", "Bash"],
        success=True,
        confidence=0.80,
    )
    slug = "build-and-push-docker-image"
    p1 = _write_procedure_proposal(sig, slug)
    p2 = _write_procedure_proposal(sig, slug)
    # Second call returns the same file, not a new one
    assert p1 == p2
    from shed.config import proposals_dir
    all_files = list(proposals_dir().glob(f"skill-*-{slug}.md"))
    assert len(all_files) == 1


# ---------------------------------------------------------------------------
# observe_procedure (full public entrypoint)
# ---------------------------------------------------------------------------


def test_observe_procedure_returns_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))

    rows = [
        _user_row("Deploy the service to production"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Deployment complete. Service is live."),
    ]
    t = _write_transcript(tmp_path, rows)

    prop = observe_procedure(t, session_id="test-session-123")
    assert prop is not None
    assert prop.path.exists()
    assert prop.confidence > 0.5


def test_observe_procedure_private_mode_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))
    monkeypatch.setenv("SHED_MODE", "private")

    rows = [
        _user_row("Build and deploy the app"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Done successfully."),
    ]
    t = _write_transcript(tmp_path, rows)
    prop = observe_procedure(t)
    assert prop is None


def test_observe_procedure_repeat_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))

    rows = [
        _user_row("Run the full test suite"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("All tests passed."),
    ]
    t = _write_transcript(tmp_path, rows)

    # With repeat_gate=2, first call should return None (seen count=1 < 2)
    prop1 = observe_procedure(t, repeat_gate=2)
    assert prop1 is None

    # Second call: seen count=2 >= 2, should emit a proposal
    prop2 = observe_procedure(t, repeat_gate=2)
    assert prop2 is not None


def test_observe_procedure_fail_open_on_bad_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))
    # Non-existent path: should return None without exception
    prop = observe_procedure(tmp_path / "does-not-exist.jsonl")
    assert prop is None


def test_observe_procedure_fail_open_on_empty_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))
    t = tmp_path / "empty.jsonl"
    t.write_text("")
    prop = observe_procedure(t)
    assert prop is None


# ---------------------------------------------------------------------------
# reflect integration: observe_procedure called from Stop hook
# ---------------------------------------------------------------------------


def test_reflect_calls_observe_procedure(tmp_path, monkeypatch):
    """reflect_from_stop_payload must call observe_procedure without error."""
    monkeypatch.setenv("SHED_HOME", str(tmp_path / "shed"))

    rows = [
        _user_row("Deploy the service"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _tool_use_row("Bash"),
        _assistant_row("Done. Service deployed."),
        # A final user row (last user text for correction detect)
        _user_row("looks good"),
    ]
    t = _write_transcript(tmp_path, rows)

    from shed.reflect import reflect_from_stop_payload

    payload = {"transcript_path": str(t), "session_id": "test-123"}
    # Must not raise; also returns (n_cited, correction_proposal)
    n_cited, correction = reflect_from_stop_payload(payload)
    assert isinstance(n_cited, int)
    # Proposal may be None (no correction signal in "looks good") — that's fine.

    # The procedure proposal should have been written to disk
    from shed.config import proposals_dir
    proposals = list(proposals_dir().glob("skill-*.md"))
    assert len(proposals) >= 1
