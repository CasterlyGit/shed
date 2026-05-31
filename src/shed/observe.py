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

# Patterns that *might* be corrections — too weak for the regex prefilter alone,
# strong enough to escalate to the Haiku judge if it's enabled.
AMBIGUOUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(actually,|hmm|wait,|let'?s|maybe|or rather)\b", re.I),
    re.compile(r"\b(would prefer|would rather|next time|going forward)\b", re.I),
    re.compile(r"\b(why did you|why are you|why would)\b", re.I),
]

JUDGE_CALL_BUDGET_FILE = "haiku-judge-budget.json"
JUDGE_DAILY_BUDGET = 100  # max calls per day to keep cost bounded


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


def detect_ambiguous(text: str) -> CorrectionSignal | None:
    """Return a candidate signal for the Haiku judge to evaluate.

    Returns None if the text doesn't trip any ambiguous pattern (so we don't
    waste judge calls on obvious non-corrections).
    """
    if not text or not text.strip():
        return None
    matches: list[str] = []
    for p in AMBIGUOUS_PATTERNS:
        m = p.search(text)
        if m:
            matches.append(m.group(0))
    if not matches:
        return None
    return CorrectionSignal(text=text.strip(), confidence=0.4, matches=matches)


def observe_tool_use(
    tool_name: str,
    args: dict | None,
    session_id: str | None = None,
    cfg: Config | None = None,
) -> bool:
    """Cross-reference a PostToolUse against permits-pending → record approval.

    Called by the PostToolUse hook before correction detection. Returns True
    if an approval was recorded (matched a recent PreToolUse).
    """
    cfg = cfg or load_config()
    if is_globally_disabled():
        return False
    if current_mode() == "private":
        return False
    if not cfg.permits.enabled:
        return False
    if not tool_name:
        return False
    try:
        from shed.permit import record_approval

        return record_approval(tool_name, args or {}, session_id=session_id)
    except Exception:
        return False


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
    if not sig and cfg.observe.use_haiku_judge:
        # Try the ambiguous-pattern + Haiku-judge path.
        amb = detect_ambiguous(text)
        if amb is not None:
            try:
                from shed.judge import judge

                verdict = judge(text, model=cfg.observe.haiku_model)
                if verdict.is_correction and verdict.confidence >= 0.6:
                    sig = CorrectionSignal(
                        text=text.strip(),
                        confidence=verdict.confidence,
                        matches=amb.matches + [f"haiku:{verdict.rationale[:40]}"],
                    )
            except Exception:
                pass
    if not sig:
        return None

    # A correction is short and leads with the pushback. A multi-KB paste
    # (tweet, log, transcript) that merely happens to contain "no"/"stop"/
    # "actually" is not a correction — drop it before it pollutes the queue.
    if len(text) > cfg.observe.max_correction_chars:
        return None

    # L5 self-tuning gate: thresholds.tune_all() raises this from the user's
    # accept/reject history, so repeatedly-rejected low-confidence noise stops
    # being queued over time. Default 0.0 keeps v0.2 behaviour.
    try:
        from shed.thresholds import get_threshold

        min_conf = get_threshold("lesson", cfg.observe.min_confidence)
    except Exception:
        min_conf = cfg.observe.min_confidence
    if sig.confidence < min_conf:
        return None

    # Redact BEFORE anything touches disk — corrections.jsonl must never store
    # raw (possibly-PII) user text.
    cleaned = redact(text, cfg.privacy.email_whitelist_domains) if cfg.privacy.redact else text
    _log_correction(sig, cleaned)

    allowlist = load_allowlist() or list(cfg.categories)
    category = classify(text, allowlist)
    if category is None:
        # Privacy default: drop anything we can't slot into an allowlisted
        # category. Better to miss a lesson than to silently learn something
        # the user didn't opt into.
        return None

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
    pdir = proposals_dir()
    pdir.mkdir(parents=True, exist_ok=True)

    # Write-time dedup. The Stop hook re-observes the same last-user message
    # every turn, so a single correction (e.g. one handoff request) used to
    # spawn one timestamped proposal *per turn* — 9 identical files from one
    # ask. Filenames are ``{ts}-{slug}.md``; an existing file with the same
    # slug means this lesson is already queued. Return it instead of writing
    # a duplicate. (Pending only — accepted/rejected proposals leave the dir,
    # so a genuinely-new instance of the same correction can re-queue later.)
    existing = sorted(pdir.glob(f"*-{slug}.md"))
    if existing:
        path = existing[0]
        return Proposal(
            slug=slug,
            category=category,
            title=title,
            body=path.read_text(errors="ignore") if path.exists() else body,
            source_text=source,
            path=path,
            created_at=now_iso(),
        )

    ts = time.strftime("%Y%m%d-%H%M%S")
    path = pdir / f"{ts}-{slug}.md"
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


def _log_correction(sig: CorrectionSignal, text: str | None = None) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "text": (text if text is not None else sig.text)[:500],
        "confidence": sig.confidence,
        "matches": sig.matches,
    }
    with (state_dir() / "corrections.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")
