#!/usr/bin/env python
"""shed benchmark — measures real inject-pipeline latency.

Usage:
    python scripts/bench.py

Environment:
    SHED_EMBEDDER=hash   (default fallback; set to onnx for real embedder)

Saves results to docs/benchmarks.md and prints a markdown table.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

# Add src to path so we can import shed without install
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

os.environ.setdefault("SHED_EMBEDDER", "hash")

# Isolated shed home so we don't touch the user's real ~/.shed
import tempfile
_tmp = tempfile.mkdtemp(prefix="shed_bench_")
os.environ["SHED_HOME"] = _tmp
os.environ["SHED_CLAUDE_HOME"] = _tmp
os.environ["SHED_MEMORY_ROOTS"] = _tmp + "/memory"
Path(_tmp + "/memory").mkdir(parents=True, exist_ok=True)

import numpy as np

from shed.embeddings import get_embedder, Index
from shed.memory import Memory


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_memories(n: int) -> list[Memory]:
    """Synthetic in-memory Memory objects — no disk needed."""
    mems = []
    phrases = [
        "use pytest fixtures for isolation",
        "prefer pathlib over os.path",
        "write fail-open hooks in bash wrappers",
        "cosine similarity for semantic search",
        "exponential decay for time-weighted scoring",
        "ONNX runtime for local inference without torch",
        "pydantic models for config validation",
        "JSONL for append-only event logs",
        "markdown frontmatter for proposal metadata",
        "lazy imports avoid startup cost",
    ]
    for i in range(n):
        body = phrases[i % len(phrases)] + f" (variant {i})"
        mems.append(Memory(
            path=Path(_tmp) / "memory" / f"mem_{i}.md",
            slug=f"mem-{i}",
            title=f"Memory {i}",
            body=body,
            category="coding",
            pinned=False,
            created_at="2026-01-01T00:00:00Z",
        ))
    return mems


class Stats(NamedTuple):
    cold_ms: float
    warm_ms: float
    p95_ms: float


def _time_n(fn, n_runs=20) -> Stats:
    """Run fn n_runs times; return (first, median-warm, p95)."""
    times = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    cold = times[0]  # first warmup is actually warm; use min as "warm" floor
    warm = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95)]
    return Stats(cold_ms=times[0], warm_ms=warm, p95_ms=p95)


# ---------------------------------------------------------------------------
# benchmarks
# ---------------------------------------------------------------------------

def bench_encode(embedder) -> Stats:
    query = "how do I run tests in pytest"
    # cold: first call
    cold_times = []
    warm_times = []
    for i in range(25):
        t0 = time.perf_counter()
        embedder.encode([query])
        elapsed = (time.perf_counter() - t0) * 1000
        if i == 0:
            cold_times.append(elapsed)
        else:
            warm_times.append(elapsed)
    warm_times.sort()
    all_times = cold_times + warm_times
    all_times.sort()
    p95 = all_times[int(len(all_times) * 0.95)]
    return Stats(
        cold_ms=cold_times[0],
        warm_ms=warm_times[len(warm_times) // 2],
        p95_ms=p95,
    )


def bench_topk(embedder, n_mems: int) -> Stats:
    mems = _make_memories(n_mems)
    idx = Index(embedder)
    idx.upsert(mems)
    query = "pytest isolation fixture"

    def _run():
        idx.search(query, top_k=5)

    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        _run()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return Stats(
        cold_ms=times[0],
        warm_ms=times[len(times) // 2],
        p95_ms=times[int(len(times) * 0.95)],
    )


def bench_inject_roundtrip(embedder) -> Stats:
    """embed + cosine over 200-item index + format output."""
    mems = _make_memories(200)
    mem_by_slug = {m.slug: m for m in mems}
    idx = Index(embedder)
    idx.upsert(mems)
    query = "how do I write a pytest fixture"

    def _run():
        hits = idx.search(query, top_k=5)
        # format block (matches inject.py _format_block logic)
        lines = ["<shed-context>"]
        for _id, slug, _score in hits:
            m = mem_by_slug.get(slug)
            body = m.body[:200] if m else ""
            lines.append(f"<!-- {slug} -->")
            lines.append(body)
        lines.append("</shed-context>")
        return "\n".join(lines)

    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        _run()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return Stats(
        cold_ms=times[0],
        warm_ms=times[len(times) // 2],
        p95_ms=times[int(len(times) * 0.95)],
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _fmt(s: Stats) -> tuple[str, str, str]:
    return f"{s.cold_ms:.1f}ms", f"{s.warm_ms:.1f}ms", f"{s.p95_ms:.1f}ms"


def main():
    embedder = get_embedder(os.environ.get("SHED_EMBEDDER", "hash"))
    backend = embedder.backend_name()

    today = __import__("datetime").date.today().isoformat()

    print(f"Running shed benchmarks (embedder: {backend}) ...")

    enc = bench_encode(embedder)
    tk50 = bench_topk(embedder, 50)
    tk200 = bench_topk(embedder, 200)
    tk500 = bench_topk(embedder, 500)
    rt = bench_inject_roundtrip(embedder)

    rows = [
        (f"embed query ({backend})",             _fmt(enc)),
        ("top-k retrieval (50 memories)",        _fmt(tk50)),
        ("top-k retrieval (200 memories)",       _fmt(tk200)),
        ("top-k retrieval (500 memories)",       _fmt(tk500)),
        ("full inject round-trip (200 memories)", _fmt(rt)),
    ]

    header = f"## shed benchmarks ({today})\n\nembedder: `{backend}`\n"
    table_header = "\n| Operation | Cold | Warm | p95 |\n|---|---|---|---|\n"
    table_rows = "".join(f"| {op} | {c} | {w} | {p} |\n" for op, (c, w, p) in rows)
    note = "\n> Measured on synthetic in-memory dataset. Cold = first call; Warm = median of 20 runs; p95 = 95th percentile.\n"
    full = header + table_header + table_rows + note

    print(full)

    docs_dir = _ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "benchmarks.md"
    out.write_text(full)
    print(f"→ saved to docs/benchmarks.md")


if __name__ == "__main__":
    main()
