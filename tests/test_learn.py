"""Tests for the `shed learn` active procedure-learning track.

Everything runs against a deterministic fake runner — no real agent, no tokens,
no network. The fake returns a scripted sequence of (output, turns, cost) so we
can assert convergence behaviour exactly.
"""

from __future__ import annotations

from pathlib import Path

from shed.config import load_config
from shed.learn import (
    PROTECTED_CLOSE,
    PROTECTED_OPEN,
    Iteration,
    _distill_strategy,
    _make_description,
    _prior_score,
    _saved_ratio,
    _skill_markdown,
    extract_skill_body,
    grade_skill,
    has_converged,
    learn_task,
)


def _it(n: int, turns: int, cost: float, ok: bool = True) -> Iteration:
    return Iteration(n=n, turns=turns, cost=cost, output="", ok=ok)


# ---------------------------------------------------------------------------
# convergence rule
# ---------------------------------------------------------------------------


def test_not_converged_without_enough_iterations():
    iters = [_it(1, 10, 1.0), _it(2, 8, 0.8)]
    assert not has_converged(iters, 0.05, 0.05, window=2)


def test_converges_when_cost_and_turns_flatten():
    # iters 2,3,4 are flat within 5% on both axes.
    iters = [
        _it(1, 20, 2.00),
        _it(2, 10, 1.00),
        _it(3, 10, 1.00),
        _it(4, 10, 1.00),
    ]
    assert has_converged(iters, 0.05, 0.05, window=2)


def test_no_convergence_if_cost_still_dropping():
    iters = [
        _it(1, 10, 2.00),
        _it(2, 10, 1.00),  # 50% cost drop
        _it(3, 10, 0.50),  # 50% cost drop
    ]
    assert not has_converged(iters, 0.05, 0.05, window=2)


def test_no_convergence_if_turns_still_dropping():
    iters = [
        _it(1, 20, 1.00),
        _it(2, 12, 1.00),
        _it(3, 8, 1.00),  # turns still moving >5%
    ]
    assert not has_converged(iters, 0.05, 0.05, window=2)


def test_failed_iterations_are_ignored_for_convergence():
    iters = [
        _it(1, 10, 1.0),
        _it(2, 0, 0.0, ok=False),  # failure shouldn't count as a flat step
        _it(3, 10, 1.0),
    ]
    # Only two *ok* iters -> can't satisfy window=2 (needs 3 ok).
    assert not has_converged(iters, 0.05, 0.05, window=2)


# ---------------------------------------------------------------------------
# orchestrator with a fake runner
# ---------------------------------------------------------------------------


def _scripted_runner(seq):
    """Return a runner that yields (output, turns, cost) from ``seq`` in order,
    repeating the last entry once exhausted."""
    calls = {"i": 0}

    def run(task: str, strategy: str):
        i = min(calls["i"], len(seq) - 1)
        calls["i"] += 1
        out, turns, cost = seq[i]
        return out, turns, cost

    return run


def test_learn_disabled_by_default_does_nothing():
    cfg = load_config()
    assert cfg.learn.enabled is False
    res = learn_task("deploy the app", cfg=cfg, runner=_scripted_runner([("x", 1, 0.1)]))
    assert res.iterations == []
    assert "enabled" in res.reason


def test_learn_runs_to_convergence_and_graduates(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 5
    # First run expensive, then flat -> should converge at iter 3.
    seq = [
        ("attempt 1\n```shed-notes\nworked: found api\n```", 20, 2.00),
        ("attempt 2\n```shed-notes\ndrop: screenshots\n```", 10, 1.00),
        ("attempt 3", 10, 1.00),
        ("attempt 4", 10, 1.00),
    ]
    res = learn_task("extract listings from example.com", cfg=cfg, runner=_scripted_runner(seq))

    assert res.converged is True
    # iters 2,3,4 are the flat trio (iter1->2 still drops), so it converges at
    # iter 4 and short-circuits before burning iter 5.
    assert len(res.iterations) == 4
    assert res.best.turns == 10
    # graduated a skill proposal
    assert res.proposal_path is not None
    assert res.proposal_path.exists()
    assert res.proposal_path.name.startswith("skill-")
    text = res.proposal_path.read_text()
    assert "kind: skill" in text
    assert "<!-- SKILL-BODY-START -->" in text


def test_learn_stops_at_max_iters_when_never_converging(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 3
    # Always improving -> never converges -> hits the cap.
    seq = [
        ("a", 30, 3.0),
        ("b", 20, 2.0),
        ("c", 10, 1.0),
    ]
    res = learn_task("never converges", cfg=cfg, runner=_scripted_runner(seq))
    assert res.converged is False
    assert len(res.iterations) == 3
    assert "max_iters" in res.reason
    assert res.best.turns == 10  # cheapest is still graduated


def test_strategy_scratchpad_accumulates(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 2
    seq = [
        ("r1\n```shed-notes\nworked: alpha\n```", 10, 1.0),
        ("r2\n```shed-notes\ndrop: beta\n```", 10, 1.0),
    ]
    res = learn_task("task", cfg=cfg, runner=_scripted_runner(seq))
    # strategy.md should hold notes from both iterations
    assert "alpha" in res.strategy
    assert "beta" in res.strategy
    strat_file = res.run_dir / "strategy.md"
    assert strat_file.exists()
    assert "iter 1" in strat_file.read_text()


def test_failed_run_is_data_not_crash(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 2

    def boom(task, strategy):
        raise RuntimeError("agent exploded")

    res = learn_task("task", cfg=cfg, runner=boom)
    assert len(res.iterations) == 2
    assert all(not it.ok for it in res.iterations)
    assert res.best is None
    assert res.proposal_path is None  # nothing to graduate


# ---------------------------------------------------------------------------
# skill body extraction (used by brief on accept)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SkillOpt hardening: validation gate, compactness, protected section, desc
# ---------------------------------------------------------------------------


def test_validation_failure_blocks_graduation(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 4
    cfg.learn.validate_gate = True
    # Converge cheap (10 turns), then the validation run blows up to 99 turns
    # -> strict gate rejects, no skill graduated.
    seq = [
        ("a", 10, 1.0),
        ("b", 10, 1.0),
        ("c", 10, 1.0),       # converged here
        ("BAD VALIDATION", 99, 9.0),  # the held-out run
    ]
    res = learn_task("flaky task", cfg=cfg, runner=_scripted_runner(seq))
    assert res.converged is True
    assert res.validated is False
    assert res.proposal_path is None
    assert "validation FAILED" in res.reason


def test_validation_pass_allows_graduation(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 4
    cfg.learn.validate_gate = True
    seq = [("a", 10, 1.0), ("b", 10, 1.0), ("c", 10, 1.0), ("good", 10, 1.0)]
    res = learn_task("reliable task", cfg=cfg, runner=_scripted_runner(seq))
    assert res.validated is True
    assert res.proposal_path is not None


def test_validation_can_be_disabled(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.validate_gate = False
    cfg.learn.max_iters = 3
    seq = [("a", 10, 1.0), ("b", 10, 1.0), ("c", 10, 1.0)]
    res = learn_task("task", cfg=cfg, runner=_scripted_runner(seq))
    assert res.validated is None
    assert res.proposal_path is not None


def test_distill_strategy_keeps_signal_drops_chatter():
    strat = (
        "# strategy: x\n"
        "## iter 1 — 20 turns, $2.0000\n"
        "worked: found the json api\n"
        "## iter 2 — 10 turns, $1.0000\n"
        "drop: screenshots\n"
        "worked: found the json api\n"  # dup
    )
    out = _distill_strategy(strat)
    assert "worked: found the json api" in out
    assert "drop: screenshots" in out
    assert "iter 1" not in out  # cost chatter dropped
    assert out.count("found the json api") == 1  # deduped


def test_skill_markdown_is_compact_and_protected():
    big = Iteration(n=1, turns=5, cost=0.2, output="X" * 50_000, ok=True)
    md = _skill_markdown("do the thing", "do-the-thing", big, "worked: api\n", max_chars=5200)
    assert len(md) <= 5200 + 1200  # capped near budget, not 50k
    assert PROTECTED_OPEN in md and PROTECTED_CLOSE in md
    # description is router-facing 'use when', not a bare task restatement
    assert "Use when you need to" in md


def test_make_description_is_trigger_rich():
    d = _make_description("deploy emergency-ai to Fly.")
    assert d.startswith("Use when you need to:")
    assert "deploy emergency-ai to Fly" in d


def test_extract_skill_body_pulls_between_markers():
    text = (
        "---\nkind: skill\n---\n"
        "<!-- SKILL-BODY-START -->\n"
        "---\nname: foo\n---\n# foo\nbody here\n"
        "<!-- SKILL-BODY-END -->\n"
    )
    body = extract_skill_body(text)
    assert body.startswith("---")
    assert "name: foo" in body
    assert "SKILL-BODY" not in body


# ---------------------------------------------------------------------------
# Earned-efficiency grader: grade_skill / _saved_ratio / _prior_score
# ---------------------------------------------------------------------------


def _best_iter(turns: int = 10, cost: float = 1.0) -> Iteration:
    """A typical converged best iteration."""
    return Iteration(n=3, turns=turns, cost=cost, output="done", ok=True)


def _make_compact_protected_skill_md() -> str:
    """A skill body that is compact and has PROTECTED markers."""
    return (
        f"{PROTECTED_OPEN}\n"
        "converged path here\n"
        f"{PROTECTED_CLOSE}\n"
    )


def test_grade_skill_passes_clean_run():
    best = _best_iter(turns=10, cost=1.0)
    skill_md = _make_compact_protected_skill_md()
    # Clean held-out run within envelope.
    runner = _scripted_runner([("clean output", 10, 1.0)])
    g = grade_skill("my task", "strategy", best, runner, skill_md=skill_md)
    assert g.passes is True
    assert g.score > 0
    assert g.held_out_ok is True
    assert g.compact is True
    assert g.protected_intact is True


def test_grade_skill_fails_on_blown_envelope():
    best = _best_iter(turns=10, cost=1.0)
    skill_md = _make_compact_protected_skill_md()
    # Held-out run blows turns past best * 1.25 + 1 = 13.5 -> 99 is over.
    runner = _scripted_runner([("bad", 99, 9.0)])
    g = grade_skill("my task", "strategy", best, runner, skill_md=skill_md)
    assert g.held_out_ok is False
    assert g.score == 0.0
    assert g.passes is False


def test_grade_skill_fails_when_not_compact():
    best = _best_iter()
    # skill_md longer than max_chars=10 (tiny budget for test).
    big_md = _make_compact_protected_skill_md() + "x" * 20
    runner = _scripted_runner([("output", 10, 1.0)])
    g = grade_skill("my task", "strategy", best, runner, skill_md=big_md, max_chars=10)
    assert g.compact is False
    assert g.passes is False


def test_grade_skill_fails_without_protected_markers():
    best = _best_iter()
    # skill_md present but no PROTECTED markers.
    skill_md = "some skill content without markers"
    runner = _scripted_runner([("output", 10, 1.0)])
    g = grade_skill("my task", "strategy", best, runner, skill_md=skill_md)
    assert g.protected_intact is False
    assert g.passes is False


def test_saved_ratio_math():
    # Full regression (achieved > baseline) -> 0.
    assert _saved_ratio(20.0, 10.0) == 0.0
    # Half saved.
    ratio = _saved_ratio(5.0, 10.0)
    assert abs(ratio - 0.5) < 1e-9
    # Zero baseline -> 0.
    assert _saved_ratio(5.0, 0.0) == 0.0
    # Perfect save -> 1.0 (clamped).
    assert _saved_ratio(0.0, 10.0) == 1.0


def test_graduation_blocked_when_score_not_beating_prior(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 4
    cfg.learn.validate_gate = True

    # Write a prior proposal with a high quality_score for this slug.
    from shed.config import proposals_dir
    proposals_dir().mkdir(parents=True, exist_ok=True)
    slug = "my-task"
    prior_proposal = proposals_dir() / f"skill-20260101-000000-{slug}.md"
    prior_proposal.write_text(
        "---\nkind: skill\nname: my-task\nquality_score: 0.9999\n---\nbody\n"
    )

    # Converge cleanly — but validation score will be low (matching baseline -> 0 saved).
    seq = [
        ("a", 10, 1.0),
        ("b", 10, 1.0),
        ("c", 10, 1.0),
        ("good output", 10, 1.0),  # held-out run: matches best exactly -> low score
    ]
    res = learn_task("my task", cfg=cfg, runner=_scripted_runner(seq))
    assert res.validated is True  # gate passed
    assert res.proposal_path is None  # but score didn't beat prior
    assert "did not beat" in res.reason


def test_graduation_allowed_when_score_beats_prior(shed_home_path):
    cfg = load_config()
    cfg.learn.enabled = True
    cfg.learn.max_iters = 4
    cfg.learn.validate_gate = True

    from shed.config import proposals_dir
    proposals_dir().mkdir(parents=True, exist_ok=True)
    slug = "my-task"
    # Prior with very low score -> new score should beat it easily.
    prior_proposal = proposals_dir() / f"skill-20260101-000000-{slug}.md"
    prior_proposal.write_text(
        "---\nkind: skill\nname: my-task\nquality_score: 0.0001\n---\nbody\n"
    )

    # Converge cheaply, held-out run also cheap -> compact/protected bonuses push score up.
    seq = [
        ("a", 20, 2.0),
        ("b", 10, 1.0),
        ("c", 10, 1.0),
        ("good output", 10, 1.0),  # held-out
    ]
    res = learn_task("my task", cfg=cfg, runner=_scripted_runner(seq))
    assert res.validated is True
    assert res.proposal_path is not None
    assert res.proposal_path.exists()
    text = res.proposal_path.read_text()
    assert "quality_score:" in text
