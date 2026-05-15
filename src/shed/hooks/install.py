"""Hook installer.

Idempotently:
* Creates ``~/.shed/`` and required subdirs.
* Renders the four hook shell wrappers from jinja templates.
* Patches ``~/.claude/settings.json`` to register them under the right events.
* Initializes a local-only git repo at ``~/.shed/`` if not present.

All operations are safe to re-run.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Template

from shed.config import (
    Config,
    allowlist_path,
    claude_home,
    config_path,
    ensure_dirs,
    hooks_dir,
    load_allowlist,
    log_dir,
    shed_home,
    write_allowlist,
    write_config,
)

HOOK_EVENTS = {
    "inject.sh": "UserPromptSubmit",
    "observe.sh": "PostToolUse",
    "reflect.sh": "Stop",
    "brief.sh": "SessionStart",
}


@dataclass
class InstallReport:
    shed_home: Path
    hooks_written: list[Path]
    settings_patched: Path
    git_initialized: bool
    indexed: int


def install(force: bool = False) -> InstallReport:
    ensure_dirs()
    cp = config_path()
    if not cp.exists() or force:
        write_config(Config())
    if not allowlist_path().exists() or force:
        write_allowlist(load_allowlist())

    written = _render_hooks()
    patched = _patch_settings()
    git_initd = _init_git()
    indexed = _build_initial_index()

    return InstallReport(
        shed_home=shed_home(),
        hooks_written=written,
        settings_patched=patched,
        git_initialized=git_initd,
        indexed=indexed,
    )


def _template_dir() -> Path:
    return Path(__file__).parent


def _render_hooks() -> list[Path]:
    out: list[Path] = []
    log_file = log_dir() / "shed.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    python = sys.executable

    for tmpl_name, dest_name in (
        ("inject.sh.j2", "inject.sh"),
        ("observe.sh.j2", "observe.sh"),
        ("reflect.sh.j2", "reflect.sh"),
        ("brief.sh.j2", "brief.sh"),
    ):
        tmpl_path = _template_dir() / tmpl_name
        text = Template(tmpl_path.read_text()).render(python=python, log_file=str(log_file))
        dest = hooks_dir() / dest_name
        dest.write_text(text)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        out.append(dest)
    return out


def _patch_settings() -> Path:
    settings = claude_home() / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            # Backup first time we modify.
            backup = settings.with_suffix(".json.shed-bak")
            if not backup.exists():
                shutil.copy2(settings, backup)
        except Exception:
            data = {}
    else:
        data = {}

    hooks = data.get("hooks") or {}
    for fname, event in HOOK_EVENTS.items():
        path = str(hooks_dir() / fname)
        entries = hooks.get(event)
        # Claude Code hook configs are commonly a list of {"command": "..."}.
        # Be tolerant of dicts and strings: normalize to a list.
        if entries is None:
            entries = []
        elif isinstance(entries, dict):
            entries = [entries]
        elif isinstance(entries, str):
            entries = [{"command": entries}]
        if not any(_entry_command(e) == path for e in entries):
            entries.append({"command": path})
        hooks[event] = entries
    data["hooks"] = hooks
    settings.write_text(json.dumps(data, indent=2) + "\n")
    return settings


def _entry_command(entry: object) -> str | None:
    if isinstance(entry, dict):
        return entry.get("command")
    if isinstance(entry, str):
        return entry
    return None


def _init_git() -> bool:
    sh = shed_home()
    git_dir = sh / ".git"
    if git_dir.exists():
        return False
    try:
        subprocess.run(
            ["git", "-C", str(sh), "init", "-q", "-b", "main"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        gitignore = sh / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("disabled\nmode\n*.lock\n")
        # Best-effort initial commit so `shed undo` has something to walk.
        env = {**os.environ, "GIT_AUTHOR_NAME": "shed", "GIT_AUTHOR_EMAIL": "shed@localhost",
               "GIT_COMMITTER_NAME": "shed", "GIT_COMMITTER_EMAIL": "shed@localhost"}
        subprocess.run(["git", "-C", str(sh), "add", "-A"], check=False, env=env)
        subprocess.run(
            ["git", "-C", str(sh), "commit", "-q", "-m", "shed: initial state"],
            check=False,
            env=env,
        )
        return True
    except FileNotFoundError:
        return False
    except subprocess.CalledProcessError:
        return False


def _build_initial_index() -> int:
    """Best-effort index build at install time. Returns # embedded."""
    try:
        from shed.embeddings import Index, get_embedder
        from shed.memory import discover

        mems = discover()
        if not mems:
            return 0
        embedder = get_embedder()
        idx = Index(embedder)
        idx.load()
        n = idx.upsert(mems)
        idx.save()
        return n
    except Exception:
        return 0
