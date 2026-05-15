# shed

> Claude Code that learns you. A silent shadow layer that picks the right notes from your past, watches for corrections, and grooms its own memory.

**Status:** v0.2 — fast embedder + permission-pattern learning. Local-only by default, every proposal manually approved, single command to install.

---

## Why this exists

You tell Claude the same things over and over. *"Use uv, not pip." "Squash-merge, delete the branch." "Don't push without running pytest."* Then a week later you're saying it again. The agent has the memory of a goldfish.

`shed` is the layer in between. It runs as a set of Claude Code hooks and silently does four things:

1. **Auto-injection** — before each prompt, picks the 2-3 most relevant memory files from `~/.claude/projects/*/memory/` and prepends them as a `<shed-context>` block. Local ONNX embeddings (`bge-small-en-v1.5`), no LLM call, ~150ms encode.
2. **Correction detection** — when you push back ("no, don't…", "use X instead"), shed catches the signal, classifies it into an allowlisted category, redacts PII, and queues a proposed lesson.
3. **Permission-pattern learning** *(new in v0.2)* — every time you approve a tool call ("allow Bash(shed *)?"), shed silently logs the canonical pattern. After N approvals of the same shape, it proposes adding it to your `permissions.allow` so Claude Code stops asking.
4. **Memory GC** — `shed evolve` archives memories you haven't cited in 90 days, surfaces near-duplicates, and promotes the hot ones. Pure Python, no model calls.

You see all of it the next morning via `shed brief` — a one-key (`y`/`n`/`e`/`s`/`p`) walk through pending proposals.

The point: stop re-saying the same thing. Stop scrolling old projects to remember which CLI you settled on. Let the agent build a real model of how you actually work.

---

## Setup

```bash
# install (uv recommended; pip works too)
uv pip install shed
shed init           # writes ~/.shed/, wires Claude Code hooks, builds index

shed doctor         # confirms everything is wired
```

That's it. New Claude Code sessions pick up the hooks automatically.

---

## Usage

The whole point is you mostly don't *use* it — it just runs.

```bash
shed why "how should I run tests?"   # see what would be injected for a prompt
shed dash                             # hot/warm/cold memories + recent injections
shed brief                            # walk pending proposals (j/k navigate, y/n/e/s/p)
shed evolve                           # GC: archive cold, propose merges, generate permits
shed mode private                     # session-level read-only mode
shed pin coding-prefs                 # never archive this one
shed undo HEAD                        # revert any auto-applied change

# v0.2 permit subcommands
shed permit list                      # top patterns shed has seen you approve
shed permit suggest                   # what would be proposed at current threshold
shed permit log -n 30                 # tail of recent approvals
shed permit threshold 5               # require 5 approvals before proposing
shed permit scan                      # manually run the proposal generator
```

---

## Config

`~/.shed/config.toml`:

```toml
auto_apply = false                       # never auto-apply in v0.1
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
use_haiku_judge = false                  # v0.2 — keep cost zero in v0.1

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

This is the section that matters.

- **Local-only by default.** `~/.shed/` is a git repo with no remote configured. You must run `shed sync enable` to opt in.
- **Allowlist by category.** Proposals only fire for categories in `allowlist.toml`. Anything that doesn't match a category is dropped, full stop.
- **Manual-approve by default.** Every proposal goes through `shed brief`. Auto-apply is OFF in v0.1.
- **Per-session privacy mode.** `shed mode private` (or `SHED_MODE=private`, or a `.shed-off` file in the cwd) disables logging, proposals, and learning for that session.
- **Global kill switch.** `touch ~/.shed/disabled` turns off everything immediately.
- **Sensitive-content redactor.** Before any write, a deterministic regex pass drops lines containing emails outside your whitelist, phone numbers, SSNs, Luhn-valid card numbers, and common API key patterns. Belt-and-suspenders alongside the category allowlist.

If you ever want to see what shed knows: `cat ~/.shed/state/injections.jsonl`. Everything is human-readable.

---

## Roadmap

- [x] Memory injection via UserPromptSubmit hook
- [x] Local embeddings (sentence-transformers, hash fallback)
- [x] Correction detection + category-allowlisted proposals
- [x] PII redactor with Luhn-checked CC detection
- [x] Memory GC (cold archive + near-duplicate detection)
- [x] Morning brief with single-key actions
- [x] `shed doctor`, `shed undo`, `shed mode`
- [x] **v0.2: ONNX embedder for Intel Mac (45s → 6ms encode)**
- [x] **v0.2: `shed permit` — learns permission-prompt patterns silently**
- [ ] Closed-loop injection quality (cite-tracking → ranking weights) (v0.2)
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

shed is the fifth leg of a stack:

- **[laptop-dictation](https://github.com/CasterlyGit/laptop-dictation)** — voice in (push-to-talk Whisper)
- **[hand-signal](https://github.com/CasterlyGit/hand-signal)** — gesture in (MediaPipe Hands)
- **[curby](https://github.com/CasterlyGit/curby)** — agent dispatcher (voice → autonomous Claude Code)
- **[approver](https://github.com/CasterlyGit/approver)** — attention router for Claude Code approval prompts
- **shed** — memory layer that learns from your corrections

---

## License

MIT.
