"""Installer / hook wiring tests."""

import json

from shed.config import claude_home
from shed.hooks.install import install


def test_install_writes_hooks_and_settings():
    rep = install()
    # Seven hook scripts created and executable: inject, observe, reflect,
    # brief, permit_observe (v0.2), plus compact-guard + handoff-writer (v0.2).
    assert len(rep.hooks_written) == 7
    for p in rep.hooks_written:
        assert p.exists()
        assert p.stat().st_mode & 0o111

    # Settings file got patched with our hook paths.
    settings = claude_home() / "settings.json"
    assert settings.exists()
    data = json.loads(settings.read_text())
    assert "hooks" in data
    serial = json.dumps(data["hooks"])
    assert "shed" in serial


def test_install_is_idempotent():
    install()
    install()
    settings = claude_home() / "settings.json"
    data = json.loads(settings.read_text())
    # An event may legitimately carry more than one shed hook (UserPromptSubmit
    # has inject + compact-guard; Stop has reflect + handoff-writer). The real
    # invariant is that re-installing never *duplicates* a command.
    for event, entries in data["hooks"].items():
        cmds = [e.get("command") for e in entries if isinstance(e, dict)]
        assert len(cmds) == len(set(cmds)), f"duplicated hook for {event}: {cmds}"
