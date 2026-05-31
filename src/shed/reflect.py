"""Session-end synthesis.

The ``Stop`` hook calls ``shed reflect`` with the recent transcript. We do
two things:

1. **Citation detection (L1).** Look at the last injection's chosen memories
   and check if the **assistant response** actually cites any of them. Logs to
   ``quality.jsonl`` so future ranking can downweight memories that never get
   cited.

2. **Correction detection.** Run ``observe_text`` against the **user's last
   message** — that's where corrections live. Writes a proposal markdown if a
   correction signal trips.

The Stop hook payload from Claude Code contains ``transcript_path`` and
``session_id`` — NOT the message text. ``reflect_from_stop_payload`` parses
the transcript JSONL, picks the last assistant turn (response text) and last
user turn (correction-candidate text), and dispatches both.

The legacy ``reflect_text(text)`` is kept for backward compat / unit tests,
but it can only do one half of the work cleanly. Prefer the new entrypoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from shed.config import Config, state_dir
from shed.memory import discover
from shed.observe import Proposal, observe_text
from shed.quality import detect_citations_in_response, log_citations


def reflect_from_stop_payload(
    payload: dict,
    cfg: Config | None = None,
    cwd: Path | None = None,
) -> tuple[int, Proposal | None]:
    """Stop-hook entrypoint. Parses ``transcript_path`` and runs both halves.

    Returns ``(num_cited, correction_proposal_or_None)``.

    The payload is the JSON Claude Code sends to the Stop hook. Expected
    keys: ``transcript_path``, ``session_id``, ``stop_hook_active``.
    """
    from shed.config import load_config

    cfg = cfg or load_config()

    transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not transcript_path:
        return 0, None

    assistant_text, user_text = _extract_last_turns(Path(transcript_path))

    # L1: citation detection on the ASSISTANT response.
    n_cited = 0
    if assistant_text:
        try:
            n_cited = _detect_and_log_citations(assistant_text)
        except Exception:
            pass

    # Statusline refresh.
    if cfg.statusline.enabled and cfg.statusline.refresh_on_stop:
        try:
            from shed.statusline import write_cache

            write_cache()
        except Exception:
            pass

    # Correction detection on the USER message.
    proposal = None
    if user_text:
        try:
            proposal = observe_text(user_text, cfg=cfg, cwd=cwd)
        except Exception:
            proposal = None

    # Auto-write stats row at session end (fail-open).
    try:
        from shed.stats import write_stats
        write_stats()
    except Exception:
        pass

    # Self-maintenance: trim runaway state logs once any is genuinely large.
    # stat-gated so the common (small-log) case is nearly free; keeps the inject
    # hot path fast on a months-old install. Fail-open.
    try:
        from shed.maintenance import maybe_groom
        maybe_groom()
    except Exception:
        pass

    return n_cited, proposal


def reflect_text(
    text: str,
    cfg: Config | None = None,
    cwd: Path | None = None,
) -> Proposal | None:
    """Legacy single-text entrypoint.

    Kept for backward compatibility. Runs citation detection AND correction
    detection on the same string — which is only useful for tests where the
    caller doesn't have separate assistant/user texts. Real Stop-hook callers
    should use ``reflect_from_stop_payload``.
    """
    from shed.config import load_config

    cfg = cfg or load_config()
    try:
        _detect_and_log_citations(text)
    except Exception:
        pass
    if cfg.statusline.enabled and cfg.statusline.refresh_on_stop:
        try:
            from shed.statusline import write_cache

            write_cache()
        except Exception:
            pass
    return observe_text(text, cfg=cfg, cwd=cwd)


# ---------------------------------------------------------------------------


def _extract_last_turns(transcript_path: Path) -> tuple[str, str]:
    """Walk the transcript JSONL backwards; return (assistant_text, user_text).

    Skips tool-use / tool-result / thinking blocks — we only want the
    natural-language text the model spoke and the natural-language text the
    user typed. Each returned string is the concatenation of all ``text``
    blocks from the most recent matching turn.
    """
    if not transcript_path or not transcript_path.exists():
        return "", ""

    assistant_text = ""
    user_text = ""

    try:
        lines = transcript_path.read_text(errors="ignore").splitlines()
    except Exception:
        return "", ""

    # Walk newest → oldest so we stop as soon as both are filled.
    for line in reversed(lines):
        if assistant_text and user_text:
            break
        try:
            row = json.loads(line)
        except Exception:
            continue
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        text = _text_from_content(content)
        if not text:
            continue
        if role == "assistant" and not assistant_text:
            assistant_text = text
        elif role == "user" and not user_text:
            # Skip Claude Code hook-feedback messages that get injected as
            # user-role turns (e.g. "Stop hook feedback: ..."). They are not
            # corrections — and the literal word "Stop" trips the correction
            # prefilter, which is what was flooding the proposal queue.
            if _is_hook_feedback(text):
                continue
            user_text = text

    return assistant_text, user_text


def _text_from_content(content) -> str:
    """Pull natural-language text out of an Anthropic message content payload.

    Returns ``""`` when the content is empty / only tool_use / only
    tool_result / only thinking blocks.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
        # Skip tool_use, tool_result, thinking, image, etc.
    return "\n".join(parts).strip()


_HOOK_FEEDBACK_RE = re.compile(
    r"^(stop|pretooluse|posttooluse|userpromptsubmit|sessionstart|"
    r"subagentstop|notification|precompact)\s+hook\s+feedback\b",
    re.I,
)
_HOOK_PATH_RE = re.compile(r"^\[[^\]]*hooks?[^\]]*\]:", re.I)


def _is_hook_feedback(text: str) -> bool:
    """True if a 'user' turn is actually Claude Code hook-feedback noise.

    Claude Code echoes hook stdout/stderr back into the transcript as a
    user-role message ("Stop hook feedback: ...", "[/.../hooks/x.sh]: No
    stderr output"). These must never be treated as corrections — left
    unfiltered they dominate corrections.jsonl and the proposal queue.
    """
    if not text:
        return False
    s = text.lstrip()
    if _HOOK_FEEDBACK_RE.match(s):
        return True
    first = s.split("\n", 1)[0]
    return bool(_HOOK_PATH_RE.match(first))


def _detect_and_log_citations(response_text: str) -> int:
    """Read the last injection from injections.jsonl, scan response for
    citations of those memories, log to quality.jsonl. Returns count cited.
    """
    if not response_text or not response_text.strip():
        return 0
    log = state_dir() / "injections.jsonl"
    if not log.exists():
        return 0
    last = None
    for line in log.read_text().splitlines():
        try:
            last = json.loads(line)
        except Exception:
            continue
    if not last:
        return 0
    chosen = last.get("chosen") or []
    if not chosen:
        return 0

    chosen_ids = {c.get("id") for c in chosen if c.get("id")}
    candidate_tuples = []
    by_id = {}
    for m in discover():
        if m.id in chosen_ids:
            candidate_tuples.append((m.id, m.body, m.title))
            by_id[m.id] = m

    cited = detect_citations_in_response(response_text, candidate_tuples)
    if cited:
        log_citations(cited)
        # Close the loop into the memory file's own metadata so GC ranks by
        # real usage (cite_count / last_cited_at), not just creation date.
        # Best-effort — a write error must never break reflect.
        from shed.memory import record_citation

        for mid in cited:
            m = by_id.get(mid)
            if m is not None:
                try:
                    record_citation(m)
                except Exception:
                    pass
    return len(cited)
