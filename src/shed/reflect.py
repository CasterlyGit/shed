"""Session-end synthesis.

The ``Stop`` hook calls ``shed reflect`` with the recent transcript. We just
re-run correction detection on the tail of the transcript — same module as
``observe`` but at session granularity. Lets us catch corrections that show up
between tool uses (e.g. user types a long final message).

This file exists so the wiring is symmetric and obvious; the actual logic is
shared with ``observe``.
"""

from __future__ import annotations

from pathlib import Path

from shed.config import Config
from shed.observe import Proposal, observe_text


def reflect_text(
    text: str,
    cfg: Config | None = None,
    cwd: Path | None = None,
) -> Proposal | None:
    return observe_text(text, cfg=cfg, cwd=cwd)
