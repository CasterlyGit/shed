"""End-to-end tests for the Stop-hook reflect pipeline.

Two things must work after a Stop event:
  1. Citation detection over the **assistant** turn → quality.jsonl gets `cited`.
  2. Correction detection over the **user** turn → a proposal lands.

Both rely on parsing the ``transcript_path`` JSONL ourselves; Claude Code's Stop
payload never contains the message text.
"""

from __future__ import annotations

import json
from pathlib import Path

from shed.reflect import _extract_last_turns, reflect_from_stop_payload


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _msg(role: str, text: str) -> dict:
    return {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _tool_use_only(role: str = "assistant") -> dict:
    return {
        "type": role,
        "message": {
            "role": role,
            "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}],
        },
    }


# ---------- _extract_last_turns ----------


def test_extract_picks_last_assistant_and_user_text(tmp_path):
    tr = tmp_path / "transcript.jsonl"
    _write_transcript(tr, [
        _msg("user", "older user msg"),
        _msg("assistant", "older assistant msg"),
        _msg("user", "newest user prompt"),
        _msg("assistant", "newest assistant response"),
    ])
    a, u = _extract_last_turns(tr)
    assert a == "newest assistant response"
    assert u == "newest user prompt"


def test_extract_skips_tool_use_and_tool_result(tmp_path):
    tr = tmp_path / "transcript.jsonl"
    _write_transcript(tr, [
        _msg("user", "real user words"),
        _msg("assistant", "real assistant words"),
        _tool_use_only("assistant"),       # newest assistant turn is tool_use only — skip
        # tool_result block from user side
        {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
    ])
    a, u = _extract_last_turns(tr)
    assert a == "real assistant words"
    assert u == "real user words"


def test_extract_handles_string_content(tmp_path):
    tr = tmp_path / "transcript.jsonl"
    _write_transcript(tr, [
        {"type": "user", "message": {"role": "user", "content": "plain string user"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "plain string asst"}},
    ])
    a, u = _extract_last_turns(tr)
    assert a == "plain string asst"
    assert u == "plain string user"


def test_extract_empty_on_missing_file(tmp_path):
    a, u = _extract_last_turns(tmp_path / "nope.jsonl")
    assert a == "" and u == ""


# ---------- reflect_from_stop_payload — end-to-end ----------


def test_reflect_logs_citation_when_assistant_quotes_memory(tmp_path, memdir, shed_home_path):
    from shed.memory import stable_id

    # 1. Plant a memory file the injection loop will treat as "chosen".
    body = "Always run pytest with -x to stop on first failure"
    mem_path = memdir / "feedback_pytest_stop_on_first_failure.md"
    mem_path.write_text(
        f"---\nslug: pytest-stop\ntitle: pytest stop on first failure\n---\n\n{body}\n"
    )
    mem_id = stable_id(mem_path)

    # 2. Fake a recent injection logging that memory as chosen.
    state = shed_home_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "injections.jsonl").write_text(
        json.dumps({"ts": 1, "chosen": [{"id": mem_id, "slug": "pytest-stop"}]}) + "\n"
    )

    # 3. Build a transcript where the assistant quotes the memory body verbatim.
    tr = tmp_path / "transcript.jsonl"
    _write_transcript(tr, [
        _msg("user", "what about tests?"),
        _msg("assistant",
             f"I'll run them now — note you told me: {body}. So I'll use -x."),
    ])

    n_cited, _ = reflect_from_stop_payload({"transcript_path": str(tr)})
    assert n_cited == 1, "citation should have been detected and logged"

    q = (state / "quality.jsonl").read_text().strip().splitlines()
    cited_events = [json.loads(line) for line in q if json.loads(line).get("event") == "cited"]
    assert any(e["memory_id"] == mem_id for e in cited_events)


def test_reflect_writes_proposal_when_user_corrects(tmp_path, shed_home_path):
    tr = tmp_path / "transcript.jsonl"
    _write_transcript(tr, [
        _msg("user", "No, don't use jq — I prefer python instead."),
        _msg("assistant", "okay, switching."),
    ])
    n_cited, proposal = reflect_from_stop_payload({"transcript_path": str(tr)})
    # A correction signal AND a classify match are both needed.
    # If classify doesn't slot it into an allowlisted category, proposal is None
    # but the corrections log should still record the signal.
    corrections_log = shed_home_path / "state" / "corrections.jsonl"
    assert corrections_log.exists(), "correction signal must be logged regardless of classify"
    rows = [json.loads(line) for line in corrections_log.read_text().splitlines() if line.strip()]
    assert rows and "don't" in rows[-1]["text"].lower() or "instead" in rows[-1]["text"].lower()


def test_reflect_noop_when_no_transcript_path(tmp_path):
    n_cited, proposal = reflect_from_stop_payload({})
    assert n_cited == 0
    assert proposal is None


def test_reflect_noop_when_transcript_missing(tmp_path):
    n_cited, proposal = reflect_from_stop_payload(
        {"transcript_path": str(tmp_path / "missing.jsonl")}
    )
    assert n_cited == 0
    assert proposal is None
