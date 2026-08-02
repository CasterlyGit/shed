# shed

[![CI](https://github.com/CasterlyGit/shed/actions/workflows/ci.yml/badge.svg)](https://github.com/CasterlyGit/shed/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Shed is a Claude Code hook layer that retrieves a small set of relevant local
memory files before a prompt and queues proposed learning changes for review.**

**Status:** v0.2 source tree — local hook installation, ONNX retrieval,
correction proposals, permission-pattern proposals, review CLI, and memory GC
are implemented. Shed is not a hosted service and does not autonomously accept
or publish proposed changes.

Published `v0.2.0` and `v0.3.0` tags have non-monotonic ancestry. The exact
history and prospective release gate are documented in
[docs/VERSIONING.md](docs/VERSIONING.md); no tag or release is moved or
recreated to make the history appear linear.

---

## How it works

| Hook | Trigger | What shed does | Latency |
|---|---|---|---|
| `UserPromptSubmit` | before each prompt | embed query, retrieve top-k memories, inject as `<shed-context>` block | bounded by the configured hook timeout |
| `Stop` | after correction signal | classify, redact PII, queue proposal | async |
| `PostToolUse` | after tool-call approval | log pattern, check repeat threshold | local processing |

**No LLM call is made in the retrieval path.** The default embedder is
`bge-small-en-v1.5` through ONNX Runtime. Initial model acquisition requires a
network connection; retrieval is local after the model artifacts are present.

Four systems:

1. **Auto-injection** — before each prompt, picks the 2–3 most relevant memory files from `~/.claude/projects/*/memory/` and prepends them as a `<shed-context>` block.
2. **Correction detection** — when you push back ("no, don't…", "use X instead"), shed catches the signal, classifies it, redacts PII, and queues a proposed lesson.
3. **Permission-pattern learning** *(v0.2)* — every time you approve a tool call, shed silently logs the canonical pattern. After N approvals of the same shape, it proposes adding it to `permissions.allow`.
4. **Memory GC** — `shed evolve` archives cold memories, surfaces near-duplicates, and promotes the hot ones. Pure Python, no model calls.

You see all of it the next morning via `shed brief` — a one-key (`y`/`n`/`e`/`s`/`p`) walk through pending proposals.

→ **[Design doc](docs/DESIGN.md)** — why hooks, why ONNX, failure modes, memory schema

---

## Benchmarks

Measured with hash embedder on synthetic 200-item memory set. Run `python scripts/bench.py` to reproduce on your machine.

| Operation | Cold | Warm | p95 |
|---|---|---|---|
| embed query (hash) | 7.3ms | 0.1ms | 0.4ms |
| top-k retrieval (50 memories) | 0.1ms | 0.1ms | 13.1ms |
| top-k retrieval (200 memories) | 0.1ms | 0.2ms | 4.3ms |
| top-k retrieval (500 memories) | 0.2ms | 0.4ms | 4.8ms |
| full inject round-trip (200 memories) | 0.1ms | 0.2ms | 0.4ms |

These figures cover the deterministic hash fixture, not the ONNX embedder or
end-to-end hook latency. Full scope and limitations:
[docs/benchmarks.md](docs/benchmarks.md).

---

## Setup

```bash
# install (uv recommended; pip works too)
uv pip install shed
shed init           # writes ~/.shed/, wires Claude Code hooks, builds index

shed doctor         # confirms everything is wired
```

New Claude Code sessions pick up the hooks automatically. No wrapper, no proxy.
`shed init` changes local hook configuration; it is not required to run the
repository verification path below.

## Evaluation and verification

Shed is a local developer-tool layer, not a hosted service or an autonomous
change system. Retrieval runs locally; proposed lessons and permission patterns
remain subject to human review through `shed brief` and the permit workflow.
Network sync is opt-in and disabled by default.

The repository CI uses the hash embedder so verification does not download a
model. Reproduce its focused checks with:

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check src tests
SHED_EMBEDDER=hash pytest -q
```

The hash embedder is a test fixture, not a claim about production embedding
quality. See [docs/benchmarks.md](docs/benchmarks.md) for the documented
benchmark setup and limitations.

---

## Usage

The whole point is you mostly don't *use* it — it just runs.

```bash
shed why "how should I run tests?"   # see what would be injected for a prompt
shed stats                            # injection hit rate, proposal ratios, top memories
shed brief                            # walk pending proposals (j/k navigate, y/n/e/s/p)
shed dash                             # hot/warm/cold memories + recent injections
shed evolve                           # GC: archive cold, propose merges, generate permits
shed mode private                     # session-level read-only mode

# v0.2 permit subcommands
shed permit list                      # top patterns shed has seen you approve
shed permit suggest                   # what would be proposed at current threshold
shed permit log -n 30                 # tail of recent approvals
shed permit threshold 5               # require 5 approvals before proposing
shed permit scan                      # manually run the proposal generator
```

---

## Observability

```bash
shed stats          # hit rate, accept/reject ratio, top 5 injected memories
```

Stats are written to `~/.shed/state/stats.jsonl` (one line per call). The
`injection_hit_rate` is a local smoothed score for how often an injected memory
was referenced in the response. The example below is illustrative, not a
project benchmark.

Sample output:
```
shed stats (last 7 days)
─────────────────────────────────────────────
 injection hit rate    42%
 memories injected     12
 memories cited back   5
 proposal accept rate  80%
 proposals accepted    4
 proposals rejected    1
 top injected (week)   coding-prefs, tool-choices, workflow
```

---

## Architecture

```
UserPromptSubmit hook
  └─ shed inject
       ├─ bge-small-en-v1.5 (ONNX, ~6ms) — embed query
       ├─ FAISS / cosine — retrieve top-k
       ├─ quality re-rank (L1 loop, 30d exp-decay)
       └─ print <shed-context> block → prepended to prompt

Stop hook
  └─ shed reflect
       ├─ detect corrections in response
       ├─ classify + redact PII
       └─ queue proposal to ~/.shed/proposals/

PostToolUse hook
  └─ shed observe
       ├─ cross-reference pending permits
       └─ record approval (infer from PostToolUse timing)
```

**Key properties:**
- **Fail-open.** Any exception in `shed inject` returns `""` — the prompt proceeds unmodified, Claude Code keeps running.
- **Timeout boundary.** The default inject timeout is 2000 ms and the shell
  wrapper enforces that boundary.
- **No LLM calls** in any hot path. Proposals can optionally use a Haiku judge for ambiguous corrections, but it's off by default (`use_haiku_judge = false`).
- **Manual-approve by default.** Every proposal goes through `shed brief`. `auto_apply = false`.

---

## Config

`~/.shed/config.toml`:

```toml
auto_apply = false                       # never auto-apply
categories = [                           # only these can become proposals
  "coding-preferences",
  "tool-choices",
  "workflow",
  "project-facts",
]

[inject]
top_k = 3
min_score = 0.25
timeout_ms = 2000

[observe]
use_haiku_judge = false                  # keep cost zero (default)

[evolve]
cold_days = 90
duplicate_threshold = 0.92

[privacy]
redact = true
email_whitelist_domains = ["anthropic.com", "gmail.com"]

[sync]
enabled = false                          # opt-in only
remote = "git@github.com:CasterlyGit/shed-state-private.git"
```

---

## Privacy

- **Local-only by default.** `~/.shed/` is a git repo with no remote configured.
- **Allowlist by category.** Proposals only fire for categories in `allowlist.toml`. Anything else is dropped.
- **Manual-approve by default.** Every proposal goes through `shed brief`. Auto-apply is OFF.
- **Per-session privacy mode.** `shed mode private` (or `SHED_MODE=private`, or a `.shed-off` file in cwd) disables logging, proposals, and learning.
- **Global kill switch.** `touch ~/.shed/disabled` turns off everything immediately.
- **PII redactor.** Before any write, a deterministic regex pass drops emails outside your whitelist, phone numbers, SSNs, Luhn-valid card numbers, and API key patterns.

---

## Roadmap

- [x] Memory injection via UserPromptSubmit hook
- [x] Local ONNX embeddings (bge-small-en-v1.5)
- [x] Correction detection + category-allowlisted proposals
- [x] PII redactor with Luhn-checked CC detection
- [x] Memory GC (cold archive + near-duplicate detection)
- [x] Morning brief with single-key actions
- [x] `shed doctor`, `shed undo`, `shed mode`
- [x] **v0.2: ONNX embedder**
- [x] **v0.2: `shed permit` — learns permission-prompt patterns**
- [x] **v0.2: L1 quality loop (cite-tracking → ranking weights)**
- [x] **v0.2: `shed stats` — injection hit rate, proposal ratios**
- [ ] Self-tuning per-kind thresholds (v0.2)
- [ ] Haiku judge for ambiguous corrections (v0.2)
- [ ] Statusline indicator (v0.2)
- [ ] Workflow shape detector + auto skill generator (v0.3)
- [ ] `shed dash --html` web view + citation graph (v0.3)
- [ ] `shed sync` push/pull, self-critique meta-loop (v0.4)
- [ ] Cross-session pattern memory + multi-agent reflection (v1.0)

Full roadmap: see [GitHub milestones](https://github.com/CasterlyGit/shed/milestones).

---

## Live demo

→ **[casterlygit.github.io/shed](https://casterlygit.github.io/shed/)** — split-pane simulation of a Claude Code session with the silent overlay visible.

---

## Companion repos

- **[laptop-dictation](https://github.com/CasterlyGit/laptop-dictation)** — voice in (push-to-talk Whisper)
- **[hand-signal](https://github.com/CasterlyGit/hand-signal)** — gesture in (MediaPipe Hands)
- **[curby](https://github.com/CasterlyGit/curby)** — agent dispatcher (voice → autonomous Claude Code)
- **approver** *(planned)* — attention router for Claude Code approval prompts
- **shed** — memory layer that learns from your corrections

---

## License

MIT.
