# shed
Claude Code that learns you — silent memory injection, self-updating notes, self-grooming, privacy-first local-only. Status: v0.2 shipped; L1 quality loop + permit learning active.

## Key files
- `src/shed/inject.py` — `inject_for_prompt()`: semantic search → quality re-ranking → `<shed-context>` block printed to stdout; called by `UserPromptSubmit` hook
- `src/shed/observe.py` — `observe_text()`: regex correction detection → classify → redact → write proposal `.md`; `observe_tool_use()`: cross-references PostToolUse against permits
- `src/shed/reflect.py` — `reflect_text()`: Stop hook handler; runs citation detection + refreshes statusline cache + delegates to `observe_text`
- `src/shed/quality.py` — L1 quality loop: `log_injection()` / `log_citations()` / `compute_scores()`; exponential decay scoring; `detect_citations_in_response()`
- `src/shed/thresholds.py` — L5 self-tuning: `log_feedback()` writes accept/reject decisions; `tune()` recomputes per-kind thresholds from 75th-pct of recent accepts
- `src/shed/statusline.py` — renders `[shed:●●●○ 3↓ ✓2]`; cached to `~/.shed/state/statusline.txt`; `install_to_claude_settings()` wires it in
- `src/shed/brief.py` — `walk_brief()` interactive TUI (y/n/e/s/p/q), `render_brief()` for SessionStart hook; handles both "lesson" and "permit" proposals
- `src/shed/config.py` — `Config` (pydantic), all path functions (`shed_home`, `proposals_dir`, `state_dir`, etc.), `current_mode()`, env var overrides (`SHED_HOME`, `SHED_MODE`, etc.)
- `src/shed/embeddings.py` — `Index` (FAISS or fallback), `get_embedder()`; uses BAAI/bge-small-en-v1.5 via onnxruntime (not torch)
- `src/shed/memory.py` — `Memory` dataclass, `discover()`, `save()`; scans `memory_roots()`
- `src/shed/permit.py` — L3 permission-pattern learning; `record_approval()`, `apply_proposal()`
- `src/shed/redact.py` — PII redaction before writing proposals
- `src/shed/classify.py` — maps free-text correction to an allowlisted category
- `src/shed/runtime/` — S1 conductor runtime: `governor.py` (both budget clocks → governor.json + fanout dial), `host.py` (work/AC-gated keepawake: caffeinate + pmset disablesleep), `resumed.py` (5h-wall pause→resume daemon, STOP kill-switch, morning digest); launchd `com.casterly.shed-runtime` ticks `shed runtime-tick` every 60s; contract in `docs/runtime.md`

## Architecture / patterns
- Three Claude Code hooks drive shed: `UserPromptSubmit` → inject, `PostToolUse` → observe_tool_use, `Stop` → reflect
- Fail-open everywhere: any exception in inject returns `""` (no injection, no crash)
- Hard timeout: inject must finish in <200ms (the shell wrapper enforces 2s)
- Privacy: `mode = private` (`.shed-off` file in cwd, or `SHED_MODE=private`) silences all disk writes and injection
- Memory roots: `~/.claude/projects/*/memory/` dirs; global CLAUDE.md is also read
- L1 quality loop: `inject` logs `"injected"` events; `reflect` scans response for 12-char substrings or title matches → logs `"cited"` events; scoring = `(cited_recent + 0.5) / (injected_recent + 1.0)` with exp-decay half-life 30d
- Final ranking: `cosine * (1 - quality_weight) + injection_score * quality_weight` where `quality_weight=0.3` by default
- `SHED_MAX_INJECT` env var (set by inject hook when session is heavy) caps `top_k` to save context
- Proposals are markdown files in `~/.shed/proposals/`; `brief` walks them; accepted lessons land in first `memory_root()`
- Permit proposals prefix filename with `permit-`; accepting them calls `shed.permit.apply_proposal()`
- Config at `~/.shed/config.toml` (TOML/pydantic); all path dirs overridable via `SHED_*` env vars for tests
- `statusline.enabled = False` by default (opt-in, since writing to `settings.json` is intrusive)

## Run / test
```bash
cd /Users/casterly/Documents/Dev/shed
uv run pytest                    # full suite
uv run python -m shed brief      # interactive proposal walk-through
uv run python -m shed inject     # test inject (reads stdin as prompt)
uv run python -m shed status     # show current mode + pending count
```

## Current state & active work
- Working: inject, observe, reflect, quality L1, permits L3, thresholds L5, statusline, brief, S1 runtime (governor/host/resumed — built 2026-06-04, E2E wall-crossing verified; lid-close survival needs the one sudoers line install-runtime.sh prints)
- Haiku judge (`observe.use_haiku_judge`) is wired but off by default — keep cost zero
- Embedder is onnxruntime/bge-small (not torch) — do not introduce torch dependency (#8 fix)
- `evolve` (cold-memory pruning + dedup) is config-gated; `evolve.enabled=True` but only runs on-demand via `shed evolve`
- Sync (`sync.enabled=False`) is wired but not activated — git remote is pre-configured for private state repo
