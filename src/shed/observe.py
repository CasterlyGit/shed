"""Correction detection.

``PostToolUse`` and ``Stop`` hooks pipe the user's recent message into
``shed observe``. We:

1. Cheap regex prefilter to spot a correction signal.
2. (v0.2) optional Haiku judge for ambiguous cases. Stubbed; off by default.
3. Classify into an allowlisted category. Drop if no match.
4. Run the redactor.
5. Write a proposal markdown file under ``~/.shed/proposals/``.

Everything is local. No model calls fire in v0.1 unless the user explicitly
flips ``observe.use_haiku_judge = true`` in config.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from shed.classify import classify
from shed.config import (
    Config,
    current_mode,
    is_globally_disabled,
    load_allowlist,
    load_config,
    proposals_dir,
    state_dir,
)
from shed.memory import now_iso, slugify
from shed.redact import redact

CORRECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(no|nope|wrong|stop)\b", re.I),
    re.compile(r"\b(don'?t|do not)\b", re.I),
    re.compile(r"\b(instead|actually|prefer|use\b.*\binstead)\b", re.I),
    re.compile(r"\b(stop doing|stop using|never)\b", re.I),
    re.compile(r"\b(I told you|I said|please don'?t)\b", re.I),
]


@dataclass
class CorrectionSignal:
    text: str
    confidence: float  # 0..1
    matches: list[str]


@dataclass
class Proposal:
    slug: str
    category: str
    title: str
    body: str
    source_text: str
    path: Path
    created_at: str


def detect(text: str) -> CorrectionSignal | None:
    if not text or not text.strip():
        return None
    matches: list[str] = []
    for p in CORRECTION_PATTERNS:
        m = p.search(text)
        if m:
            matches.append(m.group(0))
    if not matches:
        return None
    # Confidence: more independent pattern hits → higher confidence.
    conf = min(0.4 + 0.2 * len(matches), 1.0)
    return CorrectionSignal(text=text.strip(), confidence=conf, matches=matches)


def observe_text(
    text: str,
    cfg: Config | None = None,
    cwd: Path | None = None,
) -> Proposal | None:
    cfg = cfg or load_config()

    if is_globally_disabled():
        return None
    if current_mode(cwd) == "private":
        return None
    if not cfg.observe.enabled:
        return None

    sig = detect(text)
    if not sig:
        return None

    _log_correction(sig)

    allowlist = load_allowlist() or list(cfg.categories)
    category = classify(text, allowlist)
    if category is None:
        # Privacy default: drop anything we can't slot into an allowlisted
        # category. Better to miss a lesson than to silently learn something
        # the user didn't opt into.
        return None

    cleaned = redact(text, cfg.privacy.email_whitelist_domains) if cfg.privacy.redact else text

    title = _title_from(cleaned)
    slug = slugify(title)
    body = _format_body(cleaned, category, sig)
    return _write_proposal(slug=slug, category=category, title=title, body=body, source=cleaned)


# ---------------------------------------------------------------------------


def _title_from(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text.strip())
    return line[:80]


def _format_body(cleaned: str, category: str, sig: CorrectionSignal) -> str:
    return (
        f"<!-- shed proposal -->\n"
        f"category: {category}\n"
        f"signal_confidence: {sig.confidence:.2f}\n"
        f"matched: {', '.join(sig.matches)}\n\n"
        f"## What to remember\n\n"
        f"{cleaned.strip()}\n"
    )


def _write_proposal(
    slug: str, category: str, title: str, body: str, source: str
) -> Proposal:
    proposals_dir().mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = proposals_dir() / f"{ts}-{slug}.md"
    path.write_text(body)
    return Proposal(
        slug=slug,
        category=category,
        title=title,
        body=body,
        source_text=source,
        path=path,
        created_at=now_iso(),
    )


def _log_correction(sig: CorrectionSignal) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "text": sig.text[:500],
        "confidence": sig.confidence,
        "matches": sig.matches,
    }
    with (state_dir() / "corrections.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")
