"""Tests for the statusline indicator."""

from __future__ import annotations

import json

from shed.statusline import (
    install_to_claude_settings,
    memory_health_quartile,
    pending_count,
    read_cache,
    render,
    uninstall_from_claude_settings,
    write_cache,
)


def test_render_with_no_data(shed_home_path):
    out = render()
    assert out.startswith("[shed:")
    assert out.endswith("]")
    # No memories → empty quartile
    assert "○○○○" in out


def test_pending_count_counts_proposals(shed_home_path):
    from shed.config import proposals_dir

    proposals_dir().mkdir(parents=True, exist_ok=True)
    (proposals_dir() / "a.md").write_text("body")
    (proposals_dir() / "b.md").write_text("body")
    assert pending_count() == 2


def test_render_includes_pending_count(shed_home_path):
    from shed.config import proposals_dir

    proposals_dir().mkdir(parents=True, exist_ok=True)
    (proposals_dir() / "x.md").write_text("body")
    out = render()
    assert "1↓" in out


def test_write_cache_creates_file(shed_home_path):
    p = write_cache()
    assert p.exists()
    assert p.read_text().startswith("[shed:")


def test_read_cache_returns_empty_when_missing(shed_home_path):
    assert read_cache() == ""


def test_memory_health_quartile_no_memories(shed_home_path):
    assert memory_health_quartile() == 0


def test_install_writes_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    changed = install_to_claude_settings(settings)
    assert changed
    data = json.loads(settings.read_text())
    assert data["statusLine"]["type"] == "command"
    assert "cat " in data["statusLine"]["command"]


def test_install_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    install_to_claude_settings(settings)
    changed_again = install_to_claude_settings(settings)
    assert not changed_again


def test_install_backs_up_existing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": "echo my custom"}))
    install_to_claude_settings(settings)
    data = json.loads(settings.read_text())
    assert data["statusLine_pre_shed_backup"] == "echo my custom"


def test_uninstall_restores_backup(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": "echo orig"}))
    install_to_claude_settings(settings)
    uninstall_from_claude_settings(settings)
    data = json.loads(settings.read_text())
    assert data["statusLine"] == "echo orig"
    assert "statusLine_pre_shed_backup" not in data


def test_uninstall_skips_unrelated_statusline(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": "echo not ours"}))
    changed = uninstall_from_claude_settings(settings)
    assert not changed
    data = json.loads(settings.read_text())
    # Original statusLine preserved.
    assert data["statusLine"] == "echo not ours"
