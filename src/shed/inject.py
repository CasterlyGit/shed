"""Memory injection.

The ``UserPromptSubmit`` hook calls ``shed inject`` with the prompt on stdin.
We pick the top-K most relevant memories, format them as a ``<shed-context>``
block, and print to stdout. Claude Code prepends our stdout to the prompt.

Hard rules:
* fail open — any error returns "" (no injection) and logs.
* hard timeout — caller (the shell wrapper) enforces 2s. We aim for <200ms.
* respect modes — ``private`` returns "" without touching disk.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from shed.config import (
    Config,
    current_mode,
    is_globally_disabled,
    load_config,
    state_dir,
)
from shed.embeddings import Index, get_embedder
from shed.memory import Memory, discover
from shed.quality import compute_scores
from shed.quality import log_injection as log_quality_injection
from shed.redact import redact


@dataclass
class Candidate:
    id: str
    slug: str
    score: float
    chosen: bool
    cosine: float = 0.0
    quality: float = 0.5


@dataclass
class InjectionResult:
    prompt: str
    chosen: list[Memory]
    candidates: list[Candidate]
    block: str
    elapsed_ms: float
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------


def inject_for_prompt(
    prompt: str,
    cfg: Config | None = None,
    cwd: Path | None = None,
    log: bool = True,
) -> InjectionResult:
    start = time.perf_counter()
    cfg = cfg or load_config()

    if is_globally_disabled():
        return _skip(prompt, "globally-disabled", start)
    mode = current_mode(cwd)
    if mode == "private":
        return _skip(prompt, f"mode={mode}", start)
    if not cfg.inject.enabled:
        return _skip(prompt, "inject.enabled=false", start)
    if not prompt or not prompt.strip():
        return _skip(prompt, "empty-prompt", start)

    # Auto-handoff: handoff-writer drops pending-inject.md when the prior session
    # was heavy. Consume it one-shot so a fresh session auto-loads that context
    # without the user pasting a shed-ho- ID. Fail-open: any error falls through to
    # normal memory injection.
    try:
        import os as _os
        from .config import state_dir as _state_dir

        _pending = Path(_state_dir()) / "pending-inject.md"
        if _pending.exists():
            _doc = _pending.read_text()
            _pending.unlink()  # one-shot — never re-inject the same handoff
            if _doc.strip():
                # Redact before injecting — the handoff doc is built from raw
                # transcript text and may carry PII the observe pipeline never saw.
                _block = "<shed-context>\n" + redact(_doc.strip()) + "\n</shed-context>\n"
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                return InjectionResult(
                    prompt=prompt,
                    chosen=[],
                    candidates=[],
                    block=_block,
                    elapsed_ms=elapsed_ms,
                    skipped_reason="pending-handoff-injected",
                )
    except Exception:
        pass

    mems = discover()
    if not mems:
        return _skip(prompt, "no-memories", start)

    embedder = get_embedder(cfg.embedding_model)
    index = Index(embedder)
    index.load()
    index.upsert(mems)

    # Token-pressure override: inject.sh exports SHED_MAX_INJECT when the
    # session is heavy. Caps top_k to avoid burning context on memories.
    import os as _os
    _max_inject = _os.environ.get("SHED_MAX_INJECT")
    effective_top_k = min(cfg.inject.top_k, int(_max_inject)) if _max_inject else cfg.inject.top_k

    # Pull a wider slate so quality re-ranking has room to move things.
    raw_top_k = max(effective_top_k * 4, effective_top_k)
    hits = index.search(prompt, top_k=raw_top_k)

    # L1 closed-loop quality re-ranking.
    qw = max(0.0, min(1.0, cfg.inject.quality_weight))
    qscores = compute_scores(decay_days=cfg.inject.quality_decay_days)

    by_id = {m.id: m for m in mems}
    blended: list[tuple[str, str, float, float, float]] = []  # (id, slug, final, cosine, quality)
    for mid, slug, cosine in hits:
        q = qscores[mid].injection_score if mid in qscores else 0.5
        final = cosine * (1.0 - qw) + q * qw
        blended.append((mid, slug, final, cosine, q))
    # Re-sort by final blended score.
    blended.sort(key=lambda r: r[2], reverse=True)

    candidates: list[Candidate] = []
    chosen: list[Memory] = []
    for mid, slug, final, cosine, q in blended:
        keep = final >= cfg.inject.min_score and len(chosen) < effective_top_k
        candidates.append(
            Candidate(id=mid, slug=slug, score=final, chosen=keep, cosine=cosine, quality=q)
        )
        if keep and mid in by_id:
            chosen.append(by_id[mid])

    block = format_block(chosen, cfg.inject.max_chars_per_memory)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # Persist + log
    try:
        index.save()
    except Exception:
        pass
    if mode != "explore" and chosen:
        # L1: log this as an INJECTION event (not a citation — that comes
        # from Stop hook scanning the response).
        try:
            log_quality_injection([m.id for m in chosen])
        except Exception:
            pass
    if log:
        _log_injection(prompt, candidates, chosen, elapsed_ms)

    return InjectionResult(
        prompt=prompt,
        chosen=chosen,
        candidates=candidates,
        block=block,
        elapsed_ms=elapsed_ms,
    )


def format_block(chosen: list[Memory], max_chars: int) -> str:
    if not chosen:
        return ""
    parts: list[str] = ["<shed-context>"]
    parts.append(
        "<!-- Auto-injected by shed. These are your past notes most likely relevant to "
        "this prompt. Use them silently; do not mention shed unless asked. -->"
    )
    for m in chosen:
        # Redact memory bodies before they enter the prompt context — some
        # memory files predate shed's observe/redact pipeline.
        body = redact(m.body.strip())
        if len(body) > max_chars:
            body = body[: max_chars - 3].rstrip() + "..."
        cat = f" ({m.category})" if m.category else ""
        parts.append(f"\n## {m.title}{cat}\n_source: {m.path}_\n\n{body}")
    parts.append("</shed-context>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------


def _skip(prompt: str, reason: str, start: float) -> InjectionResult:
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return InjectionResult(
        prompt=prompt,
        chosen=[],
        candidates=[],
        block="",
        elapsed_ms=elapsed_ms,
        skipped_reason=reason,
    )


def _log_injection(
    prompt: str,
    candidates: list[Candidate],
    chosen: list[Memory],
    elapsed_ms: float,
) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "elapsed_ms": round(elapsed_ms, 2),
        "prompt_preview": prompt[:200],
        "candidates": [asdict(c) for c in candidates],
        "chosen": [{"id": m.id, "slug": m.slug, "path": str(m.path)} for m in chosen],
    }
    path = state_dir() / "injections.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
