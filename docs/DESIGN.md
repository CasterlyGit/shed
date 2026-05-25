# shed — Design Decisions

This document records the non-obvious choices in shed's architecture.
Reading code is faster when you know *why* before you read *what*.

---

## Why hooks, not a wrapper

The alternative to hooks is wrapping the `claude` binary — intercepting stdin/stdout at the process level. Shed deliberately avoids this.

**The hook approach:**
- Non-invasive. Hooks are registered in `~/.claude/settings.json` and fire as subprocesses. Claude Code remains the canonical binary; shed is just a side-effect layer.
- Session-transparent. Any Claude Code session — IDE extension, CLI, web — picks up the same hooks without extra config.
- Crash-isolated. A hook process dying kills the hook, not the session. Claude Code keeps running; shed just misses that turn.
- Debuggable in isolation. `echo "my prompt" | shed inject` runs the hook logic standalone.

The tradeoff: hooks can't intercept mid-stream responses, so citation detection is heuristic (12-char substring match) rather than exact. This is acceptable; the quality loop is resilient to false positives.

---

## Why ONNX + bge-small over an embedding API

Shed's retrieval path has a hard rule: **no LLM call, no network call**.

Reasons:
- **Latency.** A round-trip to OpenAI/Anthropic embeddings adds 300–800ms. The `UserPromptSubmit` hook fires synchronously — users notice any delay over ~200ms. ONNX on CPU: ~6ms encode after warmup, ~150ms cold.
- **Privacy.** Memory files contain personal workflow notes. Sending them to an external service on every prompt is a significant trust surface. Local inference eliminates this entirely.
- **Cost.** Embeddings at ~2 queries/minute across all sessions over a year is non-trivial at API pricing. ONNX costs nothing per query.
- **Availability.** No network dependency means shed works offline, on trains, in air-gapped environments.

Model choice (bge-small-en-v1.5):
- 33M params, 117MB. Fits comfortably in RAM.
- Strong MTEB retrieval scores for its size class.
- Exportable to ONNX without patching.

Fallback: if the ONNX model isn't present (`SHED_EMBEDDER=hash`), shed degrades to a deterministic hash-based fake embedder. Retrieval quality is poor but the pipeline stays functional. CI uses this mode to avoid pulling 400MB.

---

## Why manual approval for all proposals

Shed could auto-apply proposals the moment they pass the confidence threshold. It doesn't.

- **Trust.** Claude makes mistakes. Shed's correction-detection classifier makes mistakes. Auto-applying a mis-classified proposal corrupts your memory silently.
- **Auditability.** `shed brief` gives you a clear record of what shed wants to change and why. You can edit before accepting.
- **Scope control.** Memory files guide the agent's behavior in every future session. Getting one wrong is higher-stakes than getting a single code edit wrong.

`auto_apply = false` is the default and is intentionally hard to flip. The flag exists for power users who trust their classifier tuning.

---

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| Hook process crashes | That turn: no injection / no logging | Next turn is clean; `shed doctor` will surface the error |
| Embedding timeout (>2s) | Injection returns `""` — prompt proceeds without context | Fallback is silent; no user interruption |
| ONNX model missing | Falls back to hash embedder; retrieval quality drops | `shed doctor` warns; `shed init` re-downloads |
| FAISS not installed | Pure-Python cosine fallback; slightly slower (~50ms) | Transparent; no functionality lost |
| `~/.shed/` disk full | All appends fail silently (swallowed exceptions) | `shed doctor` checks available space |
| Proposal file corrupt | That proposal is skipped in `shed brief` | Manual: `rm ~/.shed/proposals/<bad-file>` |
| `settings.json` write fails during permit apply | Atomic write: original is unchanged | Error surfaced; user retries |

The core design principle: **any shed failure is a silent no-op, never a blocker.** The agent's session continues regardless.

---

## Memory schema decisions

Memory files live in `~/.claude/projects/*/memory/` (one dir per project root, matching Claude Code's convention). Format:

```markdown
---
name: <short-kebab-slug>
description: <one-line summary — used for retrieval>
metadata:
  type: user | feedback | project | reference
---

<body text>
```

Key decisions:

- **Frontmatter description field drives retrieval**, not the body. This is intentional: the description is a deliberate human-written summary. Embedding it gives better recall than embedding the full body (which may be verbose or contain noise).
- **Files are the unit of memory**, not rows in a database. This keeps the store human-readable, git-diffable, and editable with any text editor.
- **IDs are content hashes** of (slug, project root). Stable across renames as long as slug doesn't change; changes after rename trigger re-embedding on next `shed init`.
- **Proposals write to `~/.shed/proposals/`**, not directly to memory. Memory files are only created/modified after explicit user approval. The proposals dir is the staging area.
- **Quality events reference memory IDs**, not slugs. IDs survive slug renames; quality history is preserved.

---

## Proposal queue flow

```
Correction in response
        │
        ▼
  observe_text()         ← Stop hook
        │
        ├─ classify() → allowlist check → drop if no match
        │
        ├─ redact() → PII pass (email, phone, SSN, CC, API keys)
        │
        └─ write ~/.shed/proposals/<timestamp>-<kind>.md
                │
                ▼
          shed brief        ← user-facing
                │
    y → save_memory()   n → delete file   e → $EDITOR   s → skip   p → pin
```

The permit flow is parallel:

```
Tool call observed (PreToolUse)
        │
  canonicalize()  →  is_blocked() check  →  append permits-pending.jsonl
        │
PostToolUse fires within 30s
        │
  record_approval()  →  append permits-approved.jsonl
        │
  load_approval_counts() ≥ threshold
        │
  write ~/.shed/proposals/permit-<pattern>.md
        │
  shed brief → apply_proposal() → patch ~/.claude/settings.json
```

---

## Observability hooks

`shed stats` writes to `~/.shed/state/stats.jsonl` (one line per call, typically once per session end). Schema:

```json
{
  "ts": 1748000000,
  "injection_hit_rate": 0.42,
  "injected_total": 12,
  "cited_total": 5,
  "proposal_accept_rate": 0.80,
  "proposals_accepted": 4,
  "proposals_rejected": 1,
  "top_injected": ["coding-prefs", "tool-choices", "workflow"]
}
```

The `injection_hit_rate` is a smoothed Bayesian score across all memories, not a raw ratio — a memory with no data contributes 0.5 (neutral), not 0 or 1. This prevents outliers from dominating the aggregate.
