"""Installer / hook wiring tests."""

import json

from shed.config import claude_home
from shed.hooks.install import install


def test_install_writes_hooks_and_settings():
    rep = install()
    # Four hook scripts created and executable.
    assert len(rep.hooks_written) == 4
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
    # Each event should still have exactly one shed entry.
    for event, entries in data["hooks"].items():
        shed_entries = [e for e in entries if "shed" in json.dumps(e)]
        assert len(shed_entries) == 1, f"duplicated hook for {event}: {shed_entries}"
