"""Tests for shed permit (L3 loop)."""

from __future__ import annotations

import json
import time

from shed.config import claude_home, load_config, write_config
from shed.permit import (
    apply_proposal,
    canonicalize,
    generate_proposals,
    is_blocked,
    load_approval_counts,
    log_pending,
    record_approval,
    suggest,
)

# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_canonicalize_bash_with_known_verb():
    assert canonicalize("Bash", {"command": "shed why foo"}) == "Bash(shed why *)"
    assert canonicalize("Bash", {"command": "shed why"}) == "Bash(shed why)"
    assert canonicalize("Bash", {"command": "git status"}) == "Bash(git status)"
    assert canonicalize("Bash", {"command": "git push origin main"}) == "Bash(git push *)"


def test_canonicalize_bash_with_env_prefix():
    assert canonicalize("Bash", {"command": "FOO=bar git status"}) == "Bash(git status)"


def test_canonicalize_bash_unknown_verb():
    # Standalone unix utility — keep verb only.
    assert canonicalize("Bash", {"command": "ls -la /tmp"}) == "Bash(ls *)"


def test_canonicalize_bash_empty():
    assert canonicalize("Bash", {"command": ""}) == "Bash()"
    assert canonicalize("Bash", {}) == "Bash()"


def test_canonicalize_path_under_dev(monkeypatch, tmp_path):
    # Set HOME to tmp so the test is portable.
    monkeypatch.setenv("HOME", str(tmp_path))
    p = str(tmp_path / "Documents/Dev/myrepo/src/main.py")
    out = canonicalize("Read", {"file_path": p})
    assert out == "Read(~/Documents/Dev/myrepo/**)"


def test_canonicalize_path_outside_dev(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = "/var/log/something.log"
    out = canonicalize("Read", {"file_path": p})
    assert out.startswith("Read(/var/log/")
    assert out.endswith("/**)")


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------


def test_blocklist_rejects_destructive_patterns():
    for bad in ["Bash(rm -rf /tmp/foo)", "Bash(sudo brew install)", "Bash(git push --force origin)"]:
        assert is_blocked(bad), f"should block: {bad}"


def test_blocklist_rejects_overbroad_patterns():
    for bad in ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)"]:
        assert is_blocked(bad)


def test_blocklist_allows_specific_safe_patterns():
    for ok in ["Bash(shed why)", "Bash(git status)", "Read(~/Documents/Dev/shed/**)"]:
        assert not is_blocked(ok)


# ---------------------------------------------------------------------------
# Approval inference + proposal generation
# ---------------------------------------------------------------------------


def _approve_n_times(tool, args, n=3):
    """Helper: simulate N pending+post pairs for the same call."""
    for _ in range(n):
        log_pending(tool, args)
        # simulate immediate post-tool-use
        assert record_approval(tool, args)


def test_record_approval_matches_recent_pending(shed_home_path):
    log_pending("Bash", {"command": "shed why test"})
    matched = record_approval("Bash", {"command": "shed why other"})
    assert matched, "same canonical pattern should match across different args"


def test_record_approval_no_match_outside_window(shed_home_path, monkeypatch):
    log_pending("Bash", {"command": "shed why old"})
    # Simulate elapsed time by using a very small window.
    matched = record_approval("Bash", {"command": "shed why new"}, window_seconds=0.0)
    assert not matched


def test_load_approval_counts_aggregates_correctly(shed_home_path):
    _approve_n_times("Bash", {"command": "shed why x"}, n=4)
    _approve_n_times("Bash", {"command": "git status"}, n=2)
    counts = load_approval_counts()
    assert counts["Bash(shed why *)"]["count"] == 4
    assert counts["Bash(git status)"]["count"] == 2


def test_suggest_filters_by_threshold(shed_home_path):
    cfg = load_config()
    cfg.permits.threshold = 3
    write_config(cfg)
    _approve_n_times("Bash", {"command": "shed why x"}, n=4)  # above
    _approve_n_times("Bash", {"command": "git status"}, n=2)  # below
    rows = suggest()
    patterns = [r["pattern"] for r in rows]
    assert "Bash(shed why *)" in patterns
    assert "Bash(git status)" not in patterns


def test_suggest_excludes_blocked_patterns(shed_home_path):
    # Forge approvals for a destructive pattern by writing the log directly.
    from shed.config import state_dir

    state_dir().mkdir(parents=True, exist_ok=True)
    path = state_dir() / "permits-approved.jsonl"
    for _ in range(10):
        path.open("a").write(json.dumps({
            "ts": time.time(),
            "tool_name": "Bash",
            "canonical": "Bash(rm -rf /tmp/x)",
        }) + "\n")
    rows = suggest()
    patterns = [r["pattern"] for r in rows]
    assert "Bash(rm -rf /tmp/x)" not in patterns


def test_generate_proposals_writes_files(shed_home_path):
    cfg = load_config()
    cfg.permits.threshold = 3
    write_config(cfg)
    _approve_n_times("Bash", {"command": "shed why z"}, n=3)
    written = generate_proposals()
    assert written, "should have written at least one proposal"
    assert all(p.path.exists() for p in written)
    text = written[0].path.read_text()
    assert "kind: permit" in text
    assert "Bash(shed why *)" in text


def test_generate_proposals_idempotent(shed_home_path):
    cfg = load_config()
    cfg.permits.threshold = 3
    write_config(cfg)
    _approve_n_times("Bash", {"command": "shed why w"}, n=3)
    first = generate_proposals()
    second = generate_proposals()
    assert first, "first run should produce proposals"
    assert not second, "second run should produce nothing new"


# ---------------------------------------------------------------------------
# Application — atomic settings.json edit
# ---------------------------------------------------------------------------


def test_apply_proposal_adds_pattern_to_settings(shed_home_path, tmp_path):
    # Seed a settings.json with no permissions.
    settings = claude_home() / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"existing": "key"}, indent=2))

    # Write a proposal manually.
    from shed.config import proposals_dir

    proposals_dir().mkdir(parents=True, exist_ok=True)
    proposal = proposals_dir() / "permit-test.md"
    proposal.write_text(
        "---\n"
        "kind: permit\n"
        "pattern: Bash(shed why *)\n"
        "count: 5\n"
        "---\n\nbody\n"
    )

    ok, msg = apply_proposal(proposal)
    assert ok, msg
    data = json.loads(settings.read_text())
    assert "Bash(shed why *)" in data["permissions"]["allow"]
    # Original keys preserved
    assert data["existing"] == "key"


def test_apply_proposal_refuses_blocked(shed_home_path):
    settings = claude_home() / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")

    from shed.config import proposals_dir

    proposals_dir().mkdir(parents=True, exist_ok=True)
    proposal = proposals_dir() / "permit-bad.md"
    proposal.write_text(
        "---\n"
        "kind: permit\n"
        "pattern: Bash(*)\n"
        "count: 100\n"
        "---\n"
    )
    ok, msg = apply_proposal(proposal)
    assert not ok
    assert "block" in msg.lower()


def test_apply_proposal_creates_backup(shed_home_path):
    settings = claude_home() / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"x": 1}, indent=2)
    settings.write_text(original)

    from shed.config import proposals_dir

    proposals_dir().mkdir(parents=True, exist_ok=True)
    proposal = proposals_dir() / "permit-x.md"
    proposal.write_text(
        "---\nkind: permit\npattern: Bash(shed why *)\ncount: 3\n---\n"
    )
    ok, _ = apply_proposal(proposal)
    assert ok
    # A backup file must exist.
    backups = list(claude_home().glob("settings.json.shed-permit-bak-*"))
    assert backups, "backup not created"
    assert backups[0].read_text() == original


# ---------------------------------------------------------------------------
# Privacy guards
# ---------------------------------------------------------------------------


def test_log_pending_skipped_in_private_mode(shed_home_path, monkeypatch):
    monkeypatch.setenv("SHED_MODE", "private")
    log_pending("Bash", {"command": "shed why test"})
    from shed.config import state_dir

    p = state_dir() / "permits-pending.jsonl"
    # Either the file doesn't exist, or it exists empty.
    assert not p.exists() or not p.read_text().strip()


def test_log_pending_skipped_when_globally_disabled(shed_home_path):
    from shed.config import disabled_flag, state_dir

    disabled_flag().parent.mkdir(parents=True, exist_ok=True)
    disabled_flag().touch()
    log_pending("Bash", {"command": "shed why test"})
    p = state_dir() / "permits-pending.jsonl"
    assert not p.exists() or not p.read_text().strip()
