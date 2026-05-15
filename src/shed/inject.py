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
from shed.memory import Memory, discover, record_citation


@dataclass
class Candidate:
    id: str
    slug: str
    score: float
    chosen: bool


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

    mems = discover()
    if not mems:
        return _skip(prompt, "no-memories", start)

    embedder = get_embedder(cfg.embedding_model)
    index = Index(embedder)
    index.load()
    index.upsert(mems)

    hits = index.search(prompt, top_k=max(cfg.inject.top_k * 2, cfg.inject.top_k))

    by_id = {m.id: m for m in mems}
    candidates: list[Candidate] = []
    chosen: list[Memory] = []
    for mid, slug, score in hits:
        keep = score >= cfg.inject.min_score and len(chosen) < cfg.inject.top_k
        candidates.append(Candidate(id=mid, slug=slug, score=score, chosen=keep))
        if keep and mid in by_id:
            chosen.append(by_id[mid])

    block = format_block(chosen, cfg.inject.max_chars_per_memory)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # Persist + cite + log
    try:
        index.save()
    except Exception:
        pass
    if mode != "explore":
        for m in chosen:
            try:
                record_citation(m)
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
        body = m.body.strip()
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
