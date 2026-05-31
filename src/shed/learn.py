"""Active procedure-learning — the `shed learn` track.

shed's other loops are *passive*: they watch your sessions and graduate
**facts** (corrections -> memory files). ``learn`` is the *active* sibling — it
graduates **procedures**.

You hand it a recurring task ("deploy emergency-ai to Fly", "run the
casterly-ship motion on <repo>", "extract listings from <site>"). It:

1. Reads ``strategy.md`` — a scratchpad, empty on the first iteration.
2. Runs the task end-to-end against reality via a Claude Code subagent.
3. Captures the trace: turn count, cost, and the agent's own notes on what
   worked / broke / can be dropped.
4. Appends those observations to ``strategy.md`` so the next iteration starts
   smarter.
5. Repeats until consecutive iterations stop improving in cost AND turns
   (convergence), or ``max_iters`` is hit.
6. Graduates the converged approach into a ``SKILL.md`` proposal under
   ``~/.shed/proposals/`` (``kind: skill``). You approve it in ``shed brief``;
   accepted skills land live in ``~/.claude/skills/<name>/``.

This is Browserbase's Autobrowse generalized off the browser: the loop doesn't
care whether the "reality" it runs against is a website, a deploy target, a
repo, or a macOS daemon. The artifact — a readable, reusable skill — is the
point.

Cost discipline: the inner agent spends real tokens, so this NEVER fires
unattended. It runs only on explicit ``shed learn`` invocation, and
``LearnConfig.enabled`` gates it off by default.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import os
import shutil

from shed.config import (
    Config,
    archive_dir,
    current_mode,
    is_globally_disabled,
    load_config,
    proposals_dir,
    runs_dir,
    skills_dir,
)
from shed.memory import now_iso, slugify

STRATEGY_FILENAME = "strategy.md"
TRACE_FILENAME = "trace.jsonl"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Iteration:
    n: int
    turns: int
    cost: float
    output: str
    notes: str = ""  # the agent's own "what worked / broke / drop" observations
    ok: bool = True
    error: str | None = None


@dataclass
class LearnResult:
    task: str
    slug: str
    iterations: list[Iteration]
    converged: bool
    reason: str
    strategy: str
    proposal_path: Path | None = None
    run_dir: Path | None = None
    validated: bool | None = None  # SkillOpt gate: None=not run, True/False=verdict
    score: float | None = None    # earned-efficiency score 0..1 (None=not graded)
    grade_detail: str = ""        # human-readable breakdown from Grade.as_line()

    @property
    def best(self) -> Iteration | None:
        ok = [it for it in self.iterations if it.ok]
        if not ok:
            return None
        # Cheapest by cost, tie-break on fewer turns.
        return min(ok, key=lambda it: (it.cost, it.turns))


# A runner takes (task, strategy_text) and returns (output, turns, cost).
# Default runner shells out to `claude -p`; tests inject a deterministic fake.
Runner = "Callable[[str, str], tuple[str, int, float]]"


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def _rel_delta(prev: float, cur: float) -> float:
    """Relative change, robust to zero baselines."""
    denom = max(abs(prev), 1e-9)
    return abs(cur - prev) / denom


def has_converged(
    iters: list[Iteration],
    cost_eps: float,
    turns_eps: float,
    window: int,
) -> bool:
    """Converged when the last ``window`` consecutive iteration-to-iteration
    steps each moved BOTH cost and turns by less than their epsilons.

    Needs ``window + 1`` successful iterations to evaluate ``window`` deltas.
    This is Autobrowse's "short-circuit when consecutive runs stop improving in
    cost and turn count" rule, made explicit.
    """
    ok = [it for it in iters if it.ok]
    if len(ok) < window + 1:
        return False
    recent = ok[-(window + 1):]
    for prev, cur in zip(recent, recent[1:]):
        if _rel_delta(prev.cost, cur.cost) >= cost_eps:
            return False
        if _rel_delta(float(prev.turns), float(cur.turns)) >= turns_eps:
            return False
    return True


# ---------------------------------------------------------------------------
# Default runner: claude -p headless
# ---------------------------------------------------------------------------


def _build_inner_prompt(task: str, strategy: str) -> str:
    strat_block = strategy.strip() or "(none yet — this is the first attempt)"
    return (
        f"You are running a real, recurring task so shed can graduate a reusable "
        f"skill from the winning approach.\n\n"
        f"TASK:\n{task}\n\n"
        f"STRATEGY SO FAR (read this first; it is your accumulated scratchpad of "
        f"what worked, what broke, and what to stop doing):\n{strat_block}\n\n"
        f"Do the task end-to-end against reality. Prefer the cheapest reliable "
        f"path: reach for deterministic helpers (a single fetch, a script) before "
        f"a heavyweight interactive loop. Drop any step that didn't pull weight.\n\n"
        f"When done, end your response with a fenced block exactly like:\n"
        f"```shed-notes\n"
        f"worked: <what reliably worked this run>\n"
        f"broke: <what failed or was flaky>\n"
        f"drop: <steps to remove next time>\n"
        f"```\n"
    )


def _default_runner(cfg: Config) -> Runner:
    def run(task: str, strategy: str) -> tuple[str, int, float]:
        if not cfg.learn.runner_cmd:
            raise RuntimeError(
                "learn.runner_cmd is empty — set it in ~/.shed/config.toml "
                "(e.g. 'claude -p') or pass a runner to learn_task()."
            )
        prompt = _build_inner_prompt(task, strategy)
        cmd = shlex.split(cfg.learn.runner_cmd)
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=cfg.learn.iter_timeout_s,
        )
        out = proc.stdout or ""
        turns, cost = _parse_trace(out)
        return out, turns, cost

    return run


def _parse_trace(output: str) -> tuple[int, float]:
    """Best-effort extraction of turn-count and cost from a claude -p run.

    `claude -p --output-format json` emits a final JSON object with
    ``num_turns`` and ``total_cost_usd``. We tolerate plain text (fall back to
    counting non-empty lines as a rough turn proxy and cost 0.0).
    """
    text = output.strip()
    # Try last JSON object in the stream.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                turns = int(obj.get("num_turns", obj.get("turns", 0)) or 0)
                cost = float(obj.get("total_cost_usd", obj.get("cost", 0.0)) or 0.0)
                if turns or cost:
                    return turns, cost
            except Exception:
                continue
    # Fallback proxy.
    nonempty = [ln for ln in text.splitlines() if ln.strip()]
    return len(nonempty), 0.0


def _extract_notes(output: str) -> str:
    """Pull the agent's ```shed-notes``` block, if present."""
    lines = output.splitlines()
    inside = False
    grabbed: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("```shed-notes"):
            inside = True
            continue
        if inside and s.startswith("```"):
            break
        if inside:
            grabbed.append(ln)
    return "\n".join(grabbed).strip()


# ---------------------------------------------------------------------------
# Strategy scratchpad
# ---------------------------------------------------------------------------


def _append_strategy(run_dir: Path, it: Iteration) -> str:
    path = run_dir / STRATEGY_FILENAME
    existing = path.read_text() if path.exists() else "# strategy\n"
    entry = (
        f"\n## iter {it.n} — {it.turns} turns, ${it.cost:.4f}"
        f"{'' if it.ok else ' (FAILED: ' + (it.error or '?') + ')'}\n"
        f"{it.notes or '(no notes returned)'}\n"
    )
    text = existing + entry
    path.write_text(text)
    return text


def _log_iteration(run_dir: Path, it: Iteration) -> None:
    row = {
        "ts": time.time(),
        "n": it.n,
        "turns": it.turns,
        "cost": it.cost,
        "ok": it.ok,
        "error": it.error,
    }
    with (run_dir / TRACE_FILENAME).open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Skill graduation -> proposal
# ---------------------------------------------------------------------------


PROTECTED_OPEN = "<!-- shed:protected -->"
PROTECTED_CLOSE = "<!-- /shed:protected -->"


def _distill_strategy(strategy: str) -> str:
    """Compress the iteration scratchpad into a few high-signal lines.

    SkillOpt lesson #3: compactness wins (median skill ~920 tokens). We keep the
    agent's worked/broke/drop notes and drop the per-iteration cost chatter.
    """
    keep: list[str] = []
    for line in strategy.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("## iter"):
            continue
        if s.startswith(("worked:", "broke:", "drop:")):
            keep.append(f"- {s}")
    # Dedup while preserving order.
    seen: set[str] = set()
    out = [x for x in keep if not (x in seen or seen.add(x))]
    return "\n".join(out) if out else "- (no distilled notes)"


def _make_description(task: str) -> str:
    """SkillOpt lesson: the router only sees the *description*. Make it a
    trigger-rich 'use this when…' line, not a restatement of the task."""
    t = task.strip().rstrip(".")
    return f"Use when you need to: {t}. Graduated by shed-learn from real runs."


def _active_model_id(cfg: Config | None = None) -> str:
    """Return the model id to stamp on a graduated skill.

    Priority: explicit ``cfg.learn.model_id`` -> ``$SHED_MODEL_ID`` ->
    ``$CLAUDE_MODEL`` -> ``"unknown"``. The retirement gate uses this stamp
    to decide whether a skill graduated under an older model should be
    re-evaluated against the current one.
    """
    if cfg is not None and cfg.learn.model_id:
        return cfg.learn.model_id
    return os.environ.get("SHED_MODEL_ID") or os.environ.get("CLAUDE_MODEL") or "unknown"


def _skill_markdown(task: str, slug: str, result_best: Iteration, strategy: str,
                    max_chars: int = 5200, model_id: str = "unknown") -> str:
    """The graduated SKILL.md body — compact, with a protected core.

    SkillOpt fixes folded in:
    - strong *description* surface (router-facing, separate from the body)
    - distilled (not dumped) converged approach + notes — capped at max_chars
    - a protected-section invariant: the hard-won converged path is fenced so a
      later fast re-optimization pass cannot silently overwrite it.
    """
    approach = result_best.output.strip()
    # Compact the body before fencing: trim the raw transcript to its tail if
    # huge; the converged *path* is what matters, not the full run log.
    budget = max_chars - 900  # leave room for frontmatter + notes
    if len(approach) > budget:
        approach = "…(truncated)\n" + approach[-budget:]

    notes = _distill_strategy(strategy)

    return (
        f"---\n"
        f"name: {slug}\n"
        f"description: >-\n"
        f"  {_make_description(task)}\n"
        f"source: shed-learn\n"
        f"status: autobrowsed\n"
        f"updated: {now_iso()}\n"
        f"converged_turns: {result_best.turns}\n"
        f"converged_cost_usd: {result_best.cost:.4f}\n"
        f"graduated_model: {model_id}\n"
        f"---\n\n"
        f"# {slug}\n\n"
        f"## Task\n\n{task.strip()}\n\n"
        f"## Converged approach\n\n"
        f"{PROTECTED_OPEN}\n"
        f"The shortest reliable path shed found "
        f"({result_best.turns} turns, ${result_best.cost:.4f}). "
        f"Do not rewrite wholesale — bounded edits only.\n\n"
        f"{approach}\n"
        f"{PROTECTED_CLOSE}\n\n"
        f"## Notes (distilled across iterations)\n\n"
        f"{notes}\n"
    )


def _write_skill_proposal(task: str, slug: str, best: Iteration, strategy: str,
                          max_chars: int = 5200, skill_body: str | None = None,
                          score: float | None = None,
                          model_id: str = "unknown") -> Path:
    proposals_dir().mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = proposals_dir() / f"skill-{ts}-{slug}.md"
    body = skill_body if skill_body is not None else _skill_markdown(
        task, slug, best, strategy, max_chars=max_chars, model_id=model_id)
    score_line = f"quality_score: {score:.4f}\n" if score is not None else ""
    # brief.py dispatches on a `kind:` frontmatter key; mark this as a skill.
    proposal = (
        f"---\n"
        f"kind: skill\n"
        f"name: {slug}\n"
        f"confidence: {_confidence_from(best, score):.2f}\n"
        f"graduated_model: {model_id}\n"
        f"{score_line}"
        f"---\n"
        f"<!-- shed skill proposal -->\n\n"
        f"## {slug}\n\n"
        f"Converged in {best.turns} turns / ${best.cost:.4f}. "
        f"Accept to write `SKILL.md` live into your skills dir.\n\n"
        f"<!-- SKILL-BODY-START -->\n"
        f"{body}"
        f"<!-- SKILL-BODY-END -->\n"
    )
    path.write_text(proposal)
    return path


def _confidence_from(best: Iteration, score: float | None = None) -> float:
    """Map earned-efficiency score into the brief's review band [0.5, 0.9].

    If a score is available, we use it directly (clamped). Otherwise fall back
    to a fixed 0.85 so callers that don't pass a score still work.
    Capped at 0.9 so proposals always land in the review band (never auto-applied).
    """
    if score is not None:
        return max(0.5, min(0.9, score))
    return 0.85


@dataclass
class Grade:
    """Earned-efficiency report for a converged skill.

    The react-doctor principle, ported off React: instead of asking the
    unanswerable "is this skill *good*?" (open-ended verifier, SkillOpt lesson
    #6), we score the answerable "did the skill *earn its keep*?" — five
    deterministic checks, no LLM judge, no token cost. ``passes`` is the hard
    gate (held-out run must succeed within the converged envelope); ``score`` is
    the 0..1 scalar that drives "graduate only if better than the prior version"
    and gives the brief a legible number instead of a yes/no.
    """
    passes: bool
    score: float
    turns_saved: float       # 0..1 — fraction of baseline turns shaved
    cost_saved: float        # 0..1 — fraction of baseline cost shaved
    held_out_ok: bool        # the validation run ran clean & in-envelope
    compact: bool            # skill body within max_skill_chars
    protected_intact: bool   # converged path fenced by PROTECTED markers
    detail: str = ""

    def as_line(self) -> str:
        return (
            f"score={self.score:.2f} "
            f"(turns-saved {self.turns_saved:.0%}, cost-saved {self.cost_saved:.0%}, "
            f"held-out {'✓' if self.held_out_ok else '✗'}, "
            f"compact {'✓' if self.compact else '✗'}, "
            f"protected {'✓' if self.protected_intact else '✗'})"
        )


# Rubric weights — efficiency is rewarded, hygiene is rewarded; correctness is a
# hard gate (passes), not a weighted term, so a skill can never "buy back" a
# failed run with compactness points.
_W_TURNS, _W_COST, _W_COMPACT, _W_PROTECTED = 0.40, 0.40, 0.10, 0.10


def grade_skill(
    task: str,
    strategy: str,
    best: Iteration,
    run: Runner,
    skill_md: str = "",
    max_chars: int = 5200,
    baseline_iter: Iteration | None = None,
) -> Grade:
    """Run a held-out validation and score the converged skill 0..1.

    ``best`` is the cheapest successful iteration. Savings are measured against
    ``baseline_iter`` — the naive *first* attempt before any learning — because
    the held-out run uses the converged strategy and therefore ≈ ``best`` (so
    measuring against it would always report ~0% saved). When no baseline is
    supplied we fall back to the held-out run for backward compatibility.
    """
    # Held-out run — same strict envelope as before. This is the correctness gate.
    held_out_ok = True
    try:
        output, turns, cost = run(task, strategy)
    except Exception:
        held_out_ok = False
        output, turns, cost = "", best.turns, best.cost
    if not output.strip():
        held_out_ok = False
    if turns > best.turns * 1.25 + 1:
        held_out_ok = False
    if cost > best.cost * 1.25 + 1e-6:
        held_out_ok = False

    # Efficiency vs. the naive first attempt: how much did learning shave?
    # Prefer the explicit pre-learning baseline; fall back to the held-out run.
    if baseline_iter is not None and baseline_iter.turns > 0:
        base_turns = float(baseline_iter.turns)
        base_cost = baseline_iter.cost
    else:
        base_turns = float(turns) if held_out_ok else float(best.turns)
        base_cost = cost if held_out_ok else best.cost
    turns_saved = _saved_ratio(best.turns, base_turns)
    cost_saved = _saved_ratio(best.cost, base_cost)

    # Hygiene — your own SkillOpt lessons #3 (compact) and #9 (protected section).
    compact = (not skill_md) or len(skill_md) <= max_chars
    protected_intact = (not skill_md) or (
        PROTECTED_OPEN in skill_md and PROTECTED_CLOSE in skill_md
    )

    score = (
        _W_TURNS * turns_saved
        + _W_COST * cost_saved
        + _W_COMPACT * (1.0 if compact else 0.0)
        + _W_PROTECTED * (1.0 if protected_intact else 0.0)
    )
    # Correctness is a hard multiplier, never a weighted term: a failed held-out
    # run scores 0 no matter how compact or efficient it looked.
    if not held_out_ok:
        score = 0.0

    passes = held_out_ok and compact and protected_intact
    g = Grade(
        passes=passes, score=round(score, 4),
        turns_saved=round(turns_saved, 4), cost_saved=round(cost_saved, 4),
        held_out_ok=held_out_ok, compact=compact, protected_intact=protected_intact,
    )
    g.detail = g.as_line()
    return g


def _saved_ratio(achieved: float, baseline: float) -> float:
    """Fraction of ``baseline`` shaved off, clamped to 0..1. A skill that
    matches or beats baseline scores high; one that regresses scores 0."""
    if baseline <= 0:
        return 0.0
    saved = (baseline - achieved) / baseline
    if saved < 0.0:
        return 0.0
    if saved > 1.0:
        return 1.0
    # A skill no worse than baseline still earned its keep (encapsulation value),
    # so floor the reward at a small positive when it merely matches.
    return saved if saved > 0 else 0.0


def _validate(task: str, strategy: str, best: Iteration, run: Runner) -> bool:
    """Binary back-compat wrapper around :func:`grade_skill` — kept so existing
    callers/tests that only need pass/fail are unchanged."""
    return grade_skill(task, strategy, best, run).passes


def _prior_score(slug: str) -> float | None:
    """Return the max quality_score seen in existing proposals/skills for this slug.

    Scans proposals_dir() for files matching the slug and reads their
    ``quality_score:`` frontmatter value. Fail-open: any exception -> None.
    """
    try:
        scores: list[float] = []
        pdir = proposals_dir()
        if pdir.exists():
            for p in pdir.glob(f"skill-*-{slug}.md"):
                try:
                    for line in p.read_text().splitlines():
                        if line.startswith("quality_score:"):
                            val = float(line.split(":", 1)[1].strip())
                            scores.append(val)
                            break
                except Exception:
                    continue
        return max(scores) if scores else None
    except Exception:
        return None


def extract_skill_body(proposal_text: str) -> str:
    """Pull the SKILL.md body out of a kind:skill proposal (used by brief on
    accept)."""
    start = proposal_text.find("<!-- SKILL-BODY-START -->")
    end = proposal_text.find("<!-- SKILL-BODY-END -->")
    if start == -1 or end == -1:
        return proposal_text
    return proposal_text[start + len("<!-- SKILL-BODY-START -->"):end].strip() + "\n"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def learn_task(
    task: str,
    cfg: Config | None = None,
    runner: Runner | None = None,
    cwd: Path | None = None,
    on_iter=None,
) -> LearnResult:
    """Run the convergence loop for ``task`` and graduate a skill proposal.

    ``runner`` overrides the default ``claude -p`` shell runner — tests pass a
    deterministic fake. ``on_iter(Iteration)`` is an optional progress callback.
    """
    cfg = cfg or load_config()
    slug = slugify(task)

    if is_globally_disabled():
        return LearnResult(task, slug, [], False, "shed globally disabled", "")
    if current_mode(cwd) == "private":
        return LearnResult(task, slug, [], False, "private mode", "")
    if not cfg.learn.enabled:
        return LearnResult(task, slug, [], False, "learn.enabled is false", "")

    run = runner or _default_runner(cfg)

    run_dir = runs_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / STRATEGY_FILENAME).write_text(f"# strategy: {task}\n")

    iterations: list[Iteration] = []
    strategy = (run_dir / STRATEGY_FILENAME).read_text()
    converged = False
    reason = f"hit max_iters={cfg.learn.max_iters}"

    for n in range(1, cfg.learn.max_iters + 1):
        try:
            output, turns, cost = run(task, strategy)
            it = Iteration(
                n=n, turns=turns, cost=cost, output=output,
                notes=_extract_notes(output), ok=True,
            )
        except Exception as e:  # a failed run is data, not a crash.
            it = Iteration(n=n, turns=0, cost=0.0, output="", ok=False, error=str(e))

        iterations.append(it)
        _log_iteration(run_dir, it)
        strategy = _append_strategy(run_dir, it)
        if on_iter:
            on_iter(it)

        if has_converged(
            iterations,
            cfg.learn.converge_cost_eps,
            cfg.learn.converge_turns_eps,
            cfg.learn.converge_window,
        ):
            converged = True
            reason = f"converged after {n} iterations"
            break

    result = LearnResult(
        task=task, slug=slug, iterations=iterations,
        converged=converged, reason=reason, strategy=strategy, run_dir=run_dir,
    )

    best = result.best
    if best is None:
        return result  # all runs failed — nothing to graduate

    # Render the skill body once — reused for grading (compact/protected checks)
    # and for writing the proposal (avoids double render).
    model_id = _active_model_id(cfg)
    skill_body = _skill_markdown(task, slug, best, strategy,
                                 max_chars=cfg.learn.max_skill_chars,
                                 model_id=model_id)

    # SkillOpt validation gate: don't graduate slop. A held-out run must pass.
    if cfg.learn.validate_gate:
        first_ok = next((it for it in result.iterations if it.ok), None)
        grade = grade_skill(task, strategy, best, run,
                            skill_md=skill_body, max_chars=cfg.learn.max_skill_chars,
                            baseline_iter=first_ok)
        result.validated = grade.passes
        result.score = grade.score
        result.grade_detail = grade.detail
        if not grade.passes:
            result.reason += " — validation FAILED, not graduated"
            return result

        # Score-beats-prior graduation gate (react-doctor principle):
        # only graduate if the new skill strictly beats its prior version.
        prior = _prior_score(slug)
        if prior is not None and grade.score <= prior:
            result.reason += (
                f" — score {grade.score:.2f} did not beat prior {prior:.2f}, not graduated"
            )
            return result

        result.proposal_path = _write_skill_proposal(
            task, slug, best, strategy,
            max_chars=cfg.learn.max_skill_chars,
            skill_body=skill_body,
            score=grade.score,
            model_id=model_id,
        )
    else:
        result.proposal_path = _write_skill_proposal(
            task, slug, best, strategy,
            max_chars=cfg.learn.max_skill_chars,
            skill_body=skill_body,
            model_id=model_id,
        )
    return result


# ---------------------------------------------------------------------------
# Retirement gate — re-evaluate live skills, retire (don't delete) the dead.
# ---------------------------------------------------------------------------


@dataclass
class RetirementVerdict:
    """Outcome of evaluating one live skill against a fresh baseline.

    A skill *earns its keep* when running the task with the skill loaded
    beats running it without by more than ``retire_margin``. If the
    baseline matches (or beats) the skill-using run, the procedure is no
    longer pulling its weight — retire it to ``~/.shed/archive/skills/``,
    never delete. Retirement is reversible by a human; deletion isn't.
    """
    slug: str
    skill_path: Path
    retired: bool
    reason: str
    graduated_model: str = "unknown"
    current_model: str = "unknown"
    baseline_cost: float = 0.0
    skill_cost: float = 0.0
    archived_to: Path | None = None


def _parse_skill_frontmatter(skill_md: Path) -> dict[str, str]:
    """Best-effort extraction of YAML-ish frontmatter from a SKILL.md."""
    out: dict[str, str] = {}
    try:
        text = skill_md.read_text()
    except Exception:
        return out
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _retire_skill(skill_md: Path) -> Path:
    """Move a SKILL.md (with its containing dir) into the shed archive.

    Preserves the directory structure (skill dirs often hold helpers and
    references). Returns the archive path. Never deletes — retire-not-delete
    is the invariant: a human re-promotes from archive if the gate misfired.
    """
    slug = skill_md.parent.name
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = archive_dir() / "skills" / f"{slug}-{ts}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill_md.parent), str(dest))
    return dest


def evaluate_skill_retirement(
    skill_md: Path,
    task: str,
    skill_runner: Runner,
    baseline_runner: Runner,
    cfg: Config | None = None,
) -> RetirementVerdict:
    """Run the task twice — once with the skill loaded, once without — and
    decide whether the skill still earns its keep.

    Callers supply two runners so the harness stays testable: ``skill_runner``
    runs the task with the skill available (the live agent); ``baseline_runner``
    runs the same task with the skill suppressed. In production both shell out
    to ``claude -p`` with different system-prompt overrides; in tests they're
    deterministic fakes.

    Retirement triggers when the baseline matches or beats the skill-using run
    within ``cfg.learn.retire_margin``. Model-stamp drift also flags the skill
    for re-evaluation: a procedure tuned for an older model may be unnecessary
    overhead on the current one — but model drift alone never retires; only
    a losing A/B does. The model-stamp is a *trigger* for re-evaluation, not
    a verdict.
    """
    cfg = cfg or load_config()
    front = _parse_skill_frontmatter(skill_md)
    slug = front.get("name") or skill_md.parent.name
    graduated_model = front.get("graduated_model", "unknown")
    current_model = _active_model_id(cfg)

    try:
        _, _, skill_cost = skill_runner(task, "")
    except Exception as e:
        return RetirementVerdict(
            slug=slug, skill_path=skill_md, retired=False,
            reason=f"skill run failed ({e}); keeping skill conservatively",
            graduated_model=graduated_model, current_model=current_model,
        )
    try:
        _, _, baseline_cost = baseline_runner(task, "")
    except Exception as e:
        return RetirementVerdict(
            slug=slug, skill_path=skill_md, retired=False,
            reason=f"baseline run failed ({e}); keeping skill",
            graduated_model=graduated_model, current_model=current_model,
            skill_cost=skill_cost,
        )

    margin = cfg.learn.retire_margin
    # The skill earns its keep when it's cheaper than the baseline by more
    # than the margin. Anything else is "no measurable benefit" -> retire.
    skill_wins = skill_cost > 0 and (baseline_cost - skill_cost) / max(baseline_cost, 1e-9) > margin

    verdict = RetirementVerdict(
        slug=slug, skill_path=skill_md, retired=False, reason="",
        graduated_model=graduated_model, current_model=current_model,
        baseline_cost=baseline_cost, skill_cost=skill_cost,
    )
    if skill_wins:
        verdict.reason = (
            f"skill cheaper than baseline by "
            f"{(baseline_cost - skill_cost)/max(baseline_cost,1e-9):.0%} (> {margin:.0%}) — kept"
        )
        return verdict

    verdict.retired = True
    verdict.archived_to = _retire_skill(skill_md)
    verdict.reason = (
        f"baseline ${baseline_cost:.4f} vs skill ${skill_cost:.4f} — "
        f"no benefit beyond margin {margin:.0%}; retired to {verdict.archived_to}"
    )
    return verdict


def retire_stale_skills(
    cfg: Config | None = None,
    task_lookup=None,
    skill_runner: Runner | None = None,
    baseline_runner: Runner | None = None,
) -> list[RetirementVerdict]:
    """Walk live shed-graduated skills and retire any that fail their A/B.

    Only skills with ``source: shed-learn`` in their frontmatter are touched —
    we never retire hand-written skills. ``task_lookup(slug, frontmatter)``
    returns the canonical task string for re-running the A/B; if it returns
    None, the skill is skipped (no canonical task = can't evaluate).
    """
    cfg = cfg or load_config()
    verdicts: list[RetirementVerdict] = []
    root = skills_dir()
    if not root.exists():
        return verdicts
    for skill_md in root.glob("*/SKILL.md"):
        front = _parse_skill_frontmatter(skill_md)
        if front.get("source") != "shed-learn":
            continue
        task = task_lookup(front.get("name") or skill_md.parent.name, front) if task_lookup else None
        if not task:
            continue
        if skill_runner is None or baseline_runner is None:
            # In production the caller wires these to claude -p with/without
            # the skill loaded; absence here is a misuse, not a runtime crash.
            break
        verdicts.append(evaluate_skill_retirement(
            skill_md, task, skill_runner, baseline_runner, cfg=cfg,
        ))
    return verdicts
