"""v0.3 upgrade tests — the fixes that close shed's loops and keep it honest.

Covers: hook-feedback noise filter, correction-log redaction, oversized-paste
guard, durable decisions log → real accept-rate, grade_skill baseline,
skill-apply backup, CLAUDE.md as a memory root, installer corrupt-JSON refusal,
clean uninstall, and self-maintenance (groom + report).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

# --- reflect: Stop-hook-feedback noise filter --------------------------------


def test_hook_feedback_is_filtered():
    from shed.reflect import _is_hook_feedback

    assert _is_hook_feedback(
        "Stop hook feedback:\n[/Users/x/.shed/hooks/handoff-writer.sh]: No stderr output"
    )
    assert _is_hook_feedback("[/Users/x/.shed/hooks/inject.sh]: No stderr output")
    assert _is_hook_feedback("PostToolUse hook feedback: something")
    assert not _is_hook_feedback("no, don't use tabs — use spaces instead")
    assert not _is_hook_feedback("")


# --- observe: redaction + oversized-paste guard ------------------------------


def test_corrections_log_is_redacted(shed_home_path):
    from shed.config import state_dir
    from shed.observe import observe_text

    observe_text("don't commit the key sk-livetestKEY1234567890ABCD here")
    log = state_dir() / "corrections.jsonl"
    assert log.exists()
    assert "sk-livetestKEY" not in log.read_text()


def test_observe_skips_oversized_paste(shed_home_path):
    from shed.observe import observe_text

    big = "no, don't do that. " + ("blah " * 2000)  # > 4000 chars, has a cue
    assert observe_text(big) is None


# --- brief + stats: durable decisions → real accept-rate ---------------------


def test_decisions_log_drives_accept_rate(shed_home_path):
    from shed.brief import PendingProposal, _log_decision
    from shed.stats import _proposal_stats

    pp = PendingProposal(
        path=Path("x.md"), title="t", category="workflow",
        body="confidence: 0.8\n", kind="lesson",
    )
    _log_decision(pp, "accept")
    _log_decision(pp, "accept")
    _log_decision(pp, "reject")

    s = _proposal_stats()
    assert s["proposals_accepted"] == 2
    assert s["proposals_rejected"] == 1
    assert s["proposal_accept_rate"] == round(2 / 3, 3)


# --- learn: grade_skill measures savings vs the pre-learning baseline --------


def test_grade_skill_measures_savings_vs_baseline():
    from shed.learn import Iteration, grade_skill

    baseline = Iteration(n=1, turns=20, cost=0.20, output="first", ok=True)
    best = Iteration(n=3, turns=6, cost=0.06, output="converged", ok=True)

    def runner(task, strategy):
        return ("done\n", 6, 0.06)

    md = "<!-- shed:protected -->\nthe path\n<!-- /shed:protected -->\n"
    g = grade_skill("t", "s", best, runner, skill_md=md, baseline_iter=baseline)
    assert g.held_out_ok
    assert g.turns_saved > 0.5
    assert g.cost_saved > 0.5

    # Without a baseline, savings collapse toward zero (old behaviour preserved).
    g0 = grade_skill("t", "s", best, runner, skill_md=md)
    assert g0.turns_saved <= g.turns_saved


# --- brief: skill apply backs up a hand-edited SKILL.md ----------------------


_SKILL_PROPOSAL = """---
kind: skill
name: demo-skill
confidence: 0.85
---
<!-- SKILL-BODY-START -->
---
name: demo-skill
source: shed-learn
---
# demo-skill
body version {V}
<!-- SKILL-BODY-END -->
"""


def test_apply_skill_backs_up_existing(tmp_path, monkeypatch):
    from shed.brief import PendingProposal, _accept
    from shed.config import proposals_dir

    monkeypatch.setenv("SHED_SKILLS_DIR", str(tmp_path / "skills"))
    proposals_dir().mkdir(parents=True, exist_ok=True)

    p1 = proposals_dir() / "skill-1-demo-skill.md"
    p1.write_text(_SKILL_PROPOSAL.replace("{V}", "1"))
    _accept(PendingProposal.from_path(p1), Console(record=True), pin=False)

    p2 = proposals_dir() / "skill-2-demo-skill.md"
    p2.write_text(_SKILL_PROPOSAL.replace("{V}", "2"))
    _accept(PendingProposal.from_path(p2), Console(record=True), pin=False)

    bak = tmp_path / "skills" / "demo-skill" / "SKILL.md.shed-bak"
    assert bak.exists()
    assert "version 1" in bak.read_text()


# --- config: CLAUDE.md is a memory root --------------------------------------


def test_memory_roots_includes_claude_md(tmp_path, monkeypatch):
    monkeypatch.delenv("SHED_MEMORY_ROOTS", raising=False)
    monkeypatch.setenv("SHED_CLAUDE_HOME", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# global rules\nuse uv")

    from shed.config import memory_roots

    assert any(r.name == "CLAUDE.md" for r in memory_roots())


# --- install: corrupt settings.json is refused, not silently wiped -----------


def test_install_refuses_corrupt_settings():
    from shed.config import claude_home
    from shed.hooks.install import install

    settings = claude_home() / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ this is not valid json ")

    with pytest.raises(RuntimeError):
        install()
    # A backup was taken before the refusal — nothing was lost.
    assert settings.with_suffix(".json.shed-bak").exists()


# --- install: clean uninstall removes only shed's hooks ----------------------


def test_uninstall_removes_hooks():
    from shed.config import claude_home, hooks_dir
    from shed.hooks.install import install, uninstall

    install()
    res = uninstall()
    assert res["hooks_removed"] >= 1

    data = json.loads((claude_home() / "settings.json").read_text())
    hd = str(hooks_dir())
    for entries in data.get("hooks", {}).values():
        for e in entries:
            assert hd not in (e.get("command") or "")


# --- maintenance: groom / junk / report --------------------------------------


def test_groom_trims_oversized_log(shed_home_path):
    from shed.config import state_dir
    from shed.maintenance import groom_logs

    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    p = sd / "injections.jsonl"
    p.write_text("\n".join(json.dumps({"i": i}) for i in range(7000)) + "\n")

    freed = groom_logs(cap_lines=5000)
    assert "injections.jsonl" in freed
    assert len(p.read_text().splitlines()) == 5000


def test_find_junk_proposals_flags_oversized(shed_home_path):
    from shed.config import proposals_dir
    from shed.maintenance import find_junk_proposals

    pdir = proposals_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "big.md").write_text("x" * 5000)
    (pdir / "small.md").write_text("a real little lesson")

    names = {j["path"].name for j in find_junk_proposals(max_bytes=4000)}
    assert "big.md" in names
    assert "small.md" not in names


def test_build_report_returns_verdict(shed_home_path):
    from shed.maintenance import build_report

    r = build_report()
    assert isinstance(r.get("verdict"), str) and r["verdict"]
    assert "healthy" in r
    assert "log_bytes_total" in r
