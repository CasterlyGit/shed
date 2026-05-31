# shed — Portfolio Upgrade Sprint

## Goal
Make shed look like a deep, production-grade project to a technical recruiter or Google interviewer
spending 30 seconds on the repo. Every section should be a system design talking point.

## What was done this session (2026-05-25)
- README rewritten: ONNX/150ms claim first, badges, How-it-works table with latency per hook
- docs/DESIGN.md: why hooks, why ONNX, failure modes, memory schema, proposal queue flow
- shed stats command: injection hit rate, proposal ratios, top-5 memories → stats.jsonl
- 103 tests passing, pushed to main

## What still needs to happen (the real beef-up)

### 1. Benchmark script (proves the latency claims are real)
- `scripts/bench.py`: cold encode vs warm encode, top-k retrieval time, full inject round-trip
- Print a markdown table, save to `docs/benchmarks.md`
- Numbers go in README as a real table, not claims

### 2. Smoke test (`scripts/smoke.sh`)
- `shed init` → `echo "how do I run tests" | shed inject` → assert `<shed-context>` in output
- End-to-end wiring proof, runnable by anyone

### 3. GitHub release v0.2
- `git tag v0.2.0`, push tag
- Release notes: what's new, what problems it solves, latency before/after ONNX

### 4. File 4–6 GitHub issues from the roadmap
- Not just bullet points in README — real issues with context
- Shows active project maintenance

### 5. docs/index.html live demo update
- Currently stale — should show the inject flow: prompt in → shed-context block out
- Split pane: left = raw prompt, right = what Claude actually sees with injected context

### 6. .flow/ SDD artifacts
- REQUIREMENTS.md, DESIGN.md (already done), TEST_PLAN.md
- Shows disciplined engineering process (signals seniority)

### 7. Wire write_stats() into Stop hook
- Currently shed stats only writes when you run the command manually
- Should auto-append one row per session end

### 8. Fix proposal accept/reject tracking
- shed brief walk needs to write `status: accepted` / `status: rejected` into proposal files
- Without this, shed stats proposal_accept_rate always returns None

### 9. PyPI publish (optional but strong signal)
- `uv publish` — makes `pip install shed` work
- Add PyPI badge to README

## The "Google interview" framing
Shed proves:
- You understand semantic retrieval (ONNX pipeline, cosine + quality re-ranking)
- You understand feedback loops (L1 quality loop, exp-decay scoring)
- You understand non-invasive tooling (hooks, not wrappers)
- You understand privacy-first design (local-only, PII redaction, manual approval)
- You can ship systems that degrade gracefully (fail-open, hard timeouts)

Every one of those should be a 10-second scannable claim in the README.

## Curby upgrade prompt (paste into curby session)
See CURBY_SPRINT_PROMPT.md in this directory.
