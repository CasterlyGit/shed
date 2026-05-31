"""Configuration loading and path discovery.

Everything is overridable with ``SHED_*`` environment variables so tests can
point the whole world at a tmp dir without monkey-patching.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

DEFAULT_CATEGORIES: list[str] = [
    "coding-preferences",
    "tool-choices",
    "workflow",
    "project-facts",
]


class InjectConfig(BaseModel):
    enabled: bool = True
    top_k: int = 3
    min_score: float = 0.25
    timeout_ms: int = 2000
    max_chars_per_memory: int = 1200
    # L1 closed-loop quality ranking: weight on injection_score vs cosine.
    # 0.0 = pure semantic, 1.0 = pure track-record. 0.3 default.
    quality_weight: float = 0.3
    # Days before quality events decay to half their weight.
    quality_decay_days: float = 30.0


class ObserveConfig(BaseModel):
    enabled: bool = True
    use_haiku_judge: bool = False  # v0.2 — keep cost zero in v0.1
    haiku_model: str = "claude-haiku-4-5"
    # L5 self-tuning gate: drop correction signals below this confidence.
    # 0.0 keeps every signal (v0.2 behaviour); thresholds.tune_all() raises it
    # from accept/reject feedback so user rejections actually reduce noise.
    min_confidence: float = 0.0
    # A correction is short and leads with the pushback. Pasted blobs (tweets,
    # logs, transcripts) are never corrections — skip anything longer than this.
    max_correction_chars: int = 4000


class EvolveConfig(BaseModel):
    enabled: bool = True
    cold_days: int = 90
    duplicate_threshold: float = 0.92


class PrivacyConfig(BaseModel):
    redact: bool = True
    email_whitelist_domains: list[str] = Field(
        default_factory=lambda: ["anthropic.com", "gmail.com"]
    )


class SyncConfig(BaseModel):
    enabled: bool = False
    remote: str = "git@github.com:CasterlyGit/shed-state-private.git"


class StatuslineConfig(BaseModel):
    enabled: bool = False  # opt-in: writing to ~/.claude/settings.json is intrusive
    refresh_on_stop: bool = True


class LearnConfig(BaseModel):
    """Active procedure-learning (the `shed learn` track).

    shed's other loops are *passive* — they watch sessions and graduate facts.
    ``learn`` is *active*: you hand it a recurring task, it runs the task for
    real against a Claude Code subagent N times, prunes its own approach via a
    ``strategy.md`` scratchpad, and converges on the cheapest reliable path —
    then graduates a ``SKILL.md`` into ``~/.claude/skills/``.

    Off by default — it spends real tokens on the inner agent, so it only runs
    when explicitly invoked (`shed learn "<task>"`). This mirrors the
    Haiku-judge discipline: nothing that costs money fires unattended.
    """

    enabled: bool = False
    max_iters: int = 4  # Autobrowse caps low (3–5) and short-circuits hard.
    # Converge when BOTH cost and turn-count change less than these fractions
    # for `converge_window` consecutive iterations.
    converge_cost_eps: float = 0.05
    converge_turns_eps: float = 0.05
    converge_window: int = 2
    # The subagent runner: a shell command template. `{task}` and `{strategy}`
    # are substituted. Default targets `claude -p` headless. Empty = require the
    # caller to pass a runner (tests inject a fake).
    runner_cmd: str = ""
    # Where accepted skills land. Empty -> ~/.claude/skills/ (live).
    skills_dir: str = ""
    # Per-iteration wall-clock cap (seconds) before the runner is killed.
    iter_timeout_s: int = 600
    # --- SkillOpt-derived hardening ---
    # Validation gate: after convergence, run the task ONCE more on a held-out
    # input. Only graduate if it strictly passes. "Converged" != "correct".
    validate_gate: bool = True
    # Compactness: cap the graduated SKILL.md body. SkillOpt's median final
    # skill is ~920 tokens; bloat is slop. ~4 chars/token -> ~5200 chars.
    max_skill_chars: int = 5200
    # Model stamp: which Claude model graduated the skill. When the active model
    # changes, the retirement gate re-evaluates skills graduated on older
    # models, since a stronger base may have made the procedure obsolete.
    # Empty -> read from $SHED_MODEL_ID or "unknown".
    model_id: str = ""
    # Retirement gate: when re-evaluating an existing live skill, if a
    # baseline (without-skill) run scores within this fraction of the
    # skill-using run, retire the skill — it stopped earning its keep.
    # 0.05 = within 5% means "no measurable benefit".
    retire_margin: float = 0.05


class PermitsConfig(BaseModel):
    """Permission-pattern learning (the L3 loop). See ``shed.permit``."""

    enabled: bool = True
    threshold: int = 3
    # Hard-blocked patterns: never proposed even if approved many times.
    # The hardcoded list lives in shed.permit.BLOCKLIST_PATTERNS;
    # this list is for user additions.
    extra_blocklist: list[str] = Field(default_factory=list)
    # Directories under which Read/Edit/Write patterns are valid candidates.
    # Empty list means "use the defaults baked into shed.permit".
    allowlist_dirs: list[str] = Field(default_factory=list)
    # Per-pattern auto-apply: patterns matching these prefixes auto-apply
    # once threshold is reached. Default empty — manual approve in v0.2.
    auto_apply_prefixes: list[str] = Field(default_factory=list)


class Config(BaseModel):
    auto_apply: bool = False
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    inject: InjectConfig = Field(default_factory=InjectConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)
    evolve: EvolveConfig = Field(default_factory=EvolveConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    permits: PermitsConfig = Field(default_factory=PermitsConfig)
    learn: LearnConfig = Field(default_factory=LearnConfig)
    statusline: StatuslineConfig = Field(default_factory=StatuslineConfig)
    embedding_model: str = "BAAI/bge-small-en-v1.5"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _env_path(var: str, default: Path) -> Path:
    val = os.environ.get(var)
    return Path(val).expanduser() if val else default


def shed_home() -> Path:
    return _env_path("SHED_HOME", Path.home() / ".shed")


def claude_home() -> Path:
    return _env_path("SHED_CLAUDE_HOME", Path.home() / ".claude")


def memory_roots() -> list[Path]:
    """Where we look for memory files.

    By default: every ``~/.claude/projects/*/memory/`` plus
    ``~/.claude/CLAUDE.md`` (treated as one big global memory).
    """
    override = os.environ.get("SHED_MEMORY_ROOTS")
    if override:
        return [Path(p).expanduser() for p in override.split(":") if p]

    roots: list[Path] = []
    projects = claude_home() / "projects"
    if projects.is_dir():
        for proj in sorted(projects.iterdir()):
            mem = proj / "memory"
            if mem.is_dir():
                roots.append(mem)
    # The global CLAUDE.md is the single highest-signal context in the system
    # (global rules, project anchors, workflow prefs). discover() handles a
    # plain-file root, so treat it as one big always-available memory.
    global_md = claude_home() / "CLAUDE.md"
    if global_md.is_file():
        roots.append(global_md)
    return roots


def config_path() -> Path:
    return shed_home() / "config.toml"


def allowlist_path() -> Path:
    return shed_home() / "allowlist.toml"


def disabled_flag() -> Path:
    return shed_home() / "disabled"


def hooks_dir() -> Path:
    return shed_home() / "hooks"


def state_dir() -> Path:
    return shed_home() / "state"


def proposals_dir() -> Path:
    return shed_home() / "proposals"


def index_dir() -> Path:
    return shed_home() / "index"


def runs_dir() -> Path:
    """Per-`shed learn` run scratch: strategy.md, traces, iteration logs."""
    return shed_home() / "runs"


def skills_dir() -> Path:
    """Where graduated skills land when accepted. Override via config or
    ``SHED_SKILLS_DIR``; defaults to the live ``~/.claude/skills/``."""
    env = os.environ.get("SHED_SKILLS_DIR")
    if env:
        return Path(env).expanduser()
    return claude_home() / "skills"


def archive_dir() -> Path:
    return shed_home() / "archive"


def log_dir() -> Path:
    return shed_home() / "log"


def changelog_path() -> Path:
    return shed_home() / "CHANGELOG.md"


# ---------------------------------------------------------------------------
# Load / write
# ---------------------------------------------------------------------------


def ensure_dirs() -> None:
    for d in (
        shed_home(),
        hooks_dir(),
        state_dir(),
        proposals_dir(),
        index_dir(),
        archive_dir(),
        log_dir(),
        runs_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    p = config_path()
    if not p.exists():
        return Config()
    with p.open("rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)


def write_config(cfg: Config) -> None:
    ensure_dirs()
    with config_path().open("wb") as f:
        tomli_w.dump(cfg.model_dump(), f)


def load_allowlist() -> list[str]:
    p = allowlist_path()
    if not p.exists():
        return list(DEFAULT_CATEGORIES)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    cats = raw.get("categories", DEFAULT_CATEGORIES)
    return list(cats)


def write_allowlist(categories: list[str]) -> None:
    ensure_dirs()
    with allowlist_path().open("wb") as f:
        tomli_w.dump({"categories": categories}, f)


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

Mode = Literal["auto", "explore", "private"]


def current_mode(cwd: Path | None = None) -> Mode:
    """Compute the effective mode for this turn.

    Priority: ``.shed-off`` in cwd > ``SHED_MODE`` env > stored mode > "auto".
    """
    cwd = cwd or Path.cwd()
    if (cwd / ".shed-off").exists():
        return "private"
    env = os.environ.get("SHED_MODE")
    if env in ("auto", "explore", "private"):
        return env  # type: ignore[return-value]
    p = shed_home() / "mode"
    if p.exists():
        val = p.read_text().strip()
        if val in ("auto", "explore", "private"):
            return val  # type: ignore[return-value]
    return "auto"


def set_mode(mode: Mode) -> None:
    ensure_dirs()
    (shed_home() / "mode").write_text(mode)


def is_globally_disabled() -> bool:
    return disabled_flag().exists()
