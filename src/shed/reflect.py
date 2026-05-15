"""Session-end synthesis.

The ``Stop`` hook calls ``shed reflect`` with the recent transcript. We do
two things:

1. **Citation detection (L1).** Look at the last injection's chosen memories
   and check if the response text actually cites any of them. Logs to the
   quality.jsonl event log so future ranking can downweight memories that
   never get cited.

2. **Tail-of-transcript correction detection.** Same logic as ``observe``
   but at session granularity — catches corrections in the final user turn.
"""

from __future__ import annotations

import json
from pathlib import Path

from shed.config import Config, state_dir
from shed.memory import discover
from shed.observe import Proposal, observe_text
from shed.quality import detect_citations_in_response, log_citations


def reflect_text(
    text: str,
    cfg: Config | None = None,
    cwd: Path | None = None,
) -> Proposal | None:
    from shed.config import load_config

    cfg = cfg or load_config()
    # L1: cite-detection on the last injection.
    try:
        _detect_and_log_citations(text)
    except Exception:
        pass
    # #12: refresh statusline cache.
    if cfg.statusline.enabled and cfg.statusline.refresh_on_stop:
        try:
            from shed.statusline import write_cache

            write_cache()
        except Exception:
            pass
    return observe_text(text, cfg=cfg, cwd=cwd)


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

    # Build (id, body, title) tuples for the cite-detector.
    chosen_ids = {c.get("id") for c in chosen if c.get("id")}
    candidate_tuples = []
    for m in discover():
        if m.id in chosen_ids:
            candidate_tuples.append((m.id, m.body, m.title))

    cited = detect_citations_in_response(response_text, candidate_tuples)
    if cited:
        log_citations(cited)
    return len(cited)
