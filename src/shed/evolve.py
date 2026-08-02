"""Memory garbage collection.

* Promote hot memories — bump their cite_count display, no destructive op.
* Archive cold memories — anything not cited in `cold_days` (default 90) and
  not pinned, gets moved to ``~/.shed/archive/``.
* Detect near-duplicates — propose merges for memory pairs whose embedding
  cosine similarity exceeds `duplicate_threshold` (default 0.92).

Pure Python. No LLM calls. Idempotent — running twice in a row is a no-op the
second time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from shed.config import Config, load_config, proposals_dir, state_dir
from shed.embeddings import Index, get_embedder
from shed.memory import Memory, archive, discover, slugify


@dataclass
class EvolveReport:
    archived: list[str]  # slugs
    duplicates: list[tuple[str, str, float]]  # (slug_a, slug_b, score)
    hot: list[str]  # top-cited slugs
    proposals_written: list[Path]


def run_evolve(
    cfg: Config | None = None, *, now: datetime | None = None
) -> EvolveReport:
    cfg = cfg or load_config()
    mems = discover()

    archived = _archive_cold(mems, cfg.evolve.cold_days, now=now)
    # Refresh after archiving — paths changed.
    mems = [m for m in discover() if m.path.exists()]
    dups = _find_duplicates(mems, cfg) if cfg.evolve.enabled else []
    hot = _hot_list(mems, top=5)

    proposals_written: list[Path] = []
    for a, b, score in dups:
        proposals_written.append(_write_merge_proposal(a, b, score))

    # Generate permit proposals (L3 loop). Best-effort.
    try:
        from shed.permit import generate_proposals as gen_permits

        for pp in gen_permits(cfg=cfg):
            proposals_written.append(pp.path)
    except Exception:
        pass

    # L5: self-tune thresholds from feedback. Best-effort.
    try:
        from shed.thresholds import tune_all

        tune_all()
    except Exception:
        pass

    report = EvolveReport(
        archived=[m.slug for m in archived],
        duplicates=[(a.slug, b.slug, s) for a, b, s in dups],
        hot=[m.slug for m in hot],
        proposals_written=proposals_written,
    )
    _log_report(report)
    return report


# ---------------------------------------------------------------------------


def _archive_cold(
    mems: list[Memory], cold_days: int, *, now: datetime | None = None
) -> list[Memory]:
    current = now or datetime.now().astimezone()
    cutoff = current - timedelta(days=cold_days)
    archived: list[Memory] = []
    for m in mems:
        if m.pinned:
            continue
        last = _parse_iso(m.last_cited_at) or _parse_iso(m.created_at)
        if last is None:
            continue
        if last < cutoff:
            archive(m)
            archived.append(m)
    return archived


def _find_duplicates(
    mems: list[Memory], cfg: Config
) -> list[tuple[Memory, Memory, float]]:
    if len(mems) < 2:
        return []
    embedder = get_embedder(cfg.embedding_model)
    index = Index(embedder)
    index.upsert(mems)

    out: list[tuple[Memory, Memory, float]] = []
    by_id = {m.id: m for m in mems}
    vecs = index.vectors
    ids = index.ids
    if vecs.size == 0:
        return []
    sim = vecs @ vecs.T
    n = len(ids)
    seen: set[tuple[str, str]] = set()
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if s >= cfg.evolve.duplicate_threshold:
                a, b = by_id[ids[i]], by_id[ids[j]]
                key = tuple(sorted([a.slug, b.slug]))
                if key in seen:
                    continue
                seen.add(key)
                out.append((a, b, s))
    return out


def _hot_list(mems: list[Memory], top: int = 5) -> list[Memory]:
    return sorted(mems, key=lambda m: m.cite_count, reverse=True)[:top]


def _write_merge_proposal(a: Memory, b: Memory, score: float) -> Path:
    proposals_dir().mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = slugify(f"merge-{a.slug}-{b.slug}")
    path = proposals_dir() / f"{ts}-{slug}.md"
    body = (
        f"<!-- shed proposal: merge -->\n"
        f"kind: merge\n"
        f"similarity: {score:.3f}\n\n"
        f"## Two memories look near-identical\n\n"
        f"### A — `{a.slug}`\n_path: {a.path}_\n\n{a.body.strip()}\n\n"
        f"### B — `{b.slug}`\n_path: {b.path}_\n\n{b.body.strip()}\n"
    )
    path.write_text(body)
    return path


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _log_report(rep: EvolveReport) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "archived": rep.archived,
        "duplicates": [{"a": a, "b": b, "score": s} for a, b, s in rep.duplicates],
        "hot": rep.hot,
    }
    with (state_dir() / "evolve.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")
