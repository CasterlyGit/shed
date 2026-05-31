"""Self-compaction: managed CLAUDE.md block + PreCompact handler.

The autouse ``isolate_env`` fixture (conftest) already points SHED_CLAUDE_HOME
and SHED_HOME at a fresh tmp dir, so these tests just use ``claude_home()`` /
``state_dir()`` directly.
"""

import json

from shed import selfcompact as sc
from shed.config import claude_home, state_dir


def _write_transcript(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _user(text):
    return {"message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_block_inserted_and_idempotent():
    md = claude_home() / "CLAUDE.md"
    md.write_text("# Existing\n\nuser stuff\n")

    _, wrote = sc.refresh_compact_instructions(None)
    assert wrote is True
    text = md.read_text()
    assert sc.BEGIN in text and sc.END in text
    assert "# Existing" in text  # user content preserved
    assert "Compact Instructions" in text

    _, wrote2 = sc.refresh_compact_instructions(None)
    assert wrote2 is False
    assert text.count(sc.BEGIN) == 1


def test_active_task_extracted_and_noise_skipped(tmp_path):
    (claude_home() / "CLAUDE.md").write_text("# G\n")
    t = tmp_path / "transcript.jsonl"
    _write_transcript(
        t,
        [
            _user("build a self-compacting pipeline in shed"),
            _assistant("on it"),
            _user("<shed-context>\nnoise\n</shed-context>"),  # skipped as noise
        ],
    )
    task, _ = sc.refresh_compact_instructions(t)
    assert task == "build a self-compacting pipeline in shed"
    assert "Active task this session" in (claude_home() / "CLAUDE.md").read_text()


def test_on_precompact_logs_event(tmp_path):
    (claude_home() / "CLAUDE.md").write_text("# G\n")
    t = tmp_path / "transcript.jsonl"
    _write_transcript(t, [_user("fix the dedup bug")])

    ok = sc.on_precompact({"trigger": "auto", "transcript_path": str(t)})
    assert ok is True
    log = state_dir() / "compactions.jsonl"
    assert log.exists()
    row = json.loads(log.read_text().splitlines()[-1])
    assert row["event"] == "compact" and row["trigger"] == "auto"
    assert row["active_task"] == "fix the dedup bug"


def test_install_uninstall_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": []}}))
    assert sc.install_to_claude_settings(settings) is True
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["PreCompact"] for h in g["hooks"]]
    assert any("precompact.sh" in c for c in cmds)
    assert sc.install_to_claude_settings(settings) is False  # idempotent
    assert sc.uninstall_from_claude_settings(settings) is True
    assert "PreCompact" not in json.loads(settings.read_text()).get("hooks", {})
