"""Health audit. Prints what's wired, what isn't, what to do about it."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from shed.config import (
    claude_home,
    config_path,
    hooks_dir,
    is_globally_disabled,
    memory_roots,
    shed_home,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor(console: Console | None = None) -> tuple[int, list[Check]]:
    """Returns (score 0-100, checks). Higher is healthier."""
    console = console or Console()
    checks: list[Check] = []

    # ~/.shed/ exists
    sh = shed_home()
    checks.append(Check("shed_home_exists", sh.exists(), str(sh)))

    # config.toml present
    cp = config_path()
    checks.append(Check("config_present", cp.exists(), str(cp)))

    # hooks installed
    settings = claude_home() / "settings.json"
    hook_ok = False
    hook_detail = "no settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            hook_ok = "hooks" in data and any("shed" in json.dumps(v) for v in data["hooks"].values())
            hook_detail = "wired" if hook_ok else "settings.json present but no shed hooks"
        except Exception as e:
            hook_detail = f"settings.json unreadable: {e}"
    checks.append(Check("hooks_wired", hook_ok, hook_detail))

    # hook shell wrappers exist
    h = hooks_dir()
    expected = ["inject.sh", "observe.sh", "reflect.sh", "brief.sh"]
    have_all = all((h / n).exists() for n in expected)
    checks.append(Check("hook_wrappers_present", have_all, str(h)))

    # git repo
    git_ok = (sh / ".git").exists()
    checks.append(Check("git_repo", git_ok, str(sh / ".git")))

    # embedding model importable
    try:
        import sentence_transformers  # noqa: F401

        st_ok = True
        st_detail = "sentence-transformers importable"
    except Exception as e:
        st_ok = False
        st_detail = f"falling back to hash embedder: {e}"
    checks.append(Check("embedder", st_ok, st_detail))

    # memory roots populated
    roots = memory_roots()
    checks.append(
        Check(
            "memory_roots",
            bool(roots),
            f"{len(roots)} root(s)",
        )
    )

    # kill switch
    checks.append(
        Check(
            "kill_switch",
            not is_globally_disabled(),
            "active" if is_globally_disabled() else "off (good)",
        )
    )

    # gh available (informational)
    checks.append(
        Check(
            "gh_cli",
            shutil.which("gh") is not None,
            "for shed sync — optional",
        )
    )

    score = round(100 * sum(1 for c in checks if c.ok) / len(checks))

    table = Table(title=f"shed doctor — score {score}/100")
    table.add_column("check")
    table.add_column("status", width=4)
    table.add_column("detail", style="dim")
    for c in checks:
        table.add_row(c.name, "[green]ok[/green]" if c.ok else "[red]x[/red]", c.detail)
    console.print(table)
    return score, checks


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(shed_home()), *args], text=True).strip()
