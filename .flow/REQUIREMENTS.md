# shed — Requirements

## Functional Requirements

### FR-1: Memory Injection
- On every `UserPromptSubmit`, retrieve the top-K most relevant memories from the user's memory roots
- Compute relevance using cosine similarity over embeddings, re-ranked by a quality score (citation history)
- Prepend a `<shed-context>` block to the prompt if any memories score above `min_score`
- Respect `SHED_MAX_INJECT` cap when session context is heavy

### FR-2: Correction Detection
- On every `Stop` (session end), scan the user's last message for behavioral corrections
- Classify the correction into an allowlisted category (coding-preferences, workflow, tone, etc.)
- Redact PII before writing a proposal markdown to `~/.shed/proposals/`
- Confidence below threshold: optional Haiku judge invocation (off by default)

### FR-3: Permit Learning (L3)
- Track tool-use approvals via `PostToolUse` hook
- When a tool+pattern is approved N times, generate a permit proposal
- Accepted permits are stored and injected as "pre-approved" context

### FR-4: Proposal Queue (Morning Brief)
- `shed brief` walks pending proposals interactively (y/n/e/s/p/q)
- Accepting a lesson proposal writes it to the first memory root as a `.md` file
- Accepting a permit proposal calls `permit.apply_proposal()`
- Decisions write `status: accepted` / `status: rejected` to frontmatter before file removal

### FR-5: Quality Feedback Loop (L1)
- After every Stop, detect which injected memories were cited in the assistant's response
- Log injection + citation events to `~/.shed/state/quality.jsonl` with timestamp
- Exponential decay (half-life 30d) ensures stale signals don't dominate ranking

### FR-6: Self-Tuning Thresholds (L5)
- Per-kind acceptance/rejection signals feed into threshold tuning
- `tune()` recomputes per-kind min_score from the 75th percentile of recent accepted confidence scores
- Thresholds stored in `~/.shed/state/thresholds.jsonl`

### FR-7: Garbage Collection
- `shed evolve` prunes cold memories (not injected in 90d) and deduplicates near-identical entries
- Config-gated (`evolve.enabled`); on-demand only, never automatic

### FR-8: Stats & Observability
- `shed stats` prints injection hit rate, proposal accept rate, top-5 memories
- Auto-appends one stats row to `~/.shed/state/stats.jsonl` on every session end
- `shed doctor` checks hook wiring and file health

## Non-Functional Requirements

### NFR-1: Latency
- **Inject must complete in < 200ms** (hash embedder: ~1ms; ONNX embedder: ~150ms)
- Shell wrapper enforces a 2-second hard kill
- No LLM call in the hot path (inject, observe, reflect) — Haiku judge is opt-in

### NFR-2: Fail-Open
- Any exception in inject returns `""` — prompt is unmodified, Claude never stalls
- Any exception in reflect is logged to stderr and swallowed
- Stats write failure does not block session end

### NFR-3: Privacy-First / Local-Only
- All data stays in `~/.shed/` — no network calls unless opt-in sync is configured
- `mode = private` (`.shed-off` file in cwd or `SHED_MODE=private`) silences all writes and injection
- PII redaction runs before any proposal is written to disk

### NFR-4: No External Dependencies in Hot Path
- Inject only requires: numpy, (optionally) onnxruntime, pyarrow
- No torch dependency — ONNX runtime is the preferred embedder

### NFR-5: Testability
- All hot-path code runs with `SHED_EMBEDDER=hash` (no model download needed)
- Every test uses isolated `SHED_HOME` + `SHED_MEMORY_ROOTS` via `conftest.py` fixtures
- Full suite runs offline

## Out of Scope

- **Cloud sync** — opt-in only via git remote; never enabled by default
- **Auto-apply without approval** — every lesson and permit requires explicit user acceptance
- **LLM rewriting of memories** — shed observes and proposes; it never rewrites silently
- **Multi-user / team memory sharing** — single-user local store only
