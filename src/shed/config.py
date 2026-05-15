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
