# S1 Autonomous Runtime — the conductor's foundation

Status: live. Four epics from `shed-ho-5e9a0001`: always-on host, budget
governor, pause→handoff→continuation, remote ingress. One launchd heartbeat
(`com.casterly.shed-runtime` → `shed runtime-tick`, every 60s) drives the
first three; workflow-watcher (`com.casterly.workflow-watcher`) carries the
fourth and cooperates through the state-file contract below.

```
                       ┌────────────────────────────────────────────┐
 phone ── Gmail ──────▶│ workflow-watcher tick (120s)               │
  "workflow …"  build  │  controls: stop / go / status (always)     │
  "workflow stop" etc. │  builds: serial, idle-only, STOP-gated     │
                       └──────────────┬─────────────────────────────┘
                                      │ claude -p (detached, hours)
                                      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │ shed runtime-tick (60s):                                        │
 │  governor → governor.json   (both clocks, pace, fanout dial)    │
 │  host     → caffeinate + pmset disablesleep (work- & AC-gated)  │
 │  continuation worker  → wall pause / scheduled continuation / STOP / morning digest │
 └─────────────────────────────────────────────────────────────────┘
```

## The state-file contract (`~/.shed/state/`)

| file | writer → reader | meaning |
|---|---|---|
| `governor.json` | runtime-tick → S2, watcher, anyone | budget report; `fanout_scale` is the dial S2 sizes agent fleets with |
| `STOP` | `shed stop`, email "workflow stop" → everyone | kill-switch: no respawns, no new builds, keepawake releases |
| continuation queue entry | runtime workers | runs waiting for the 5h window to reset |
| `keepawake-leases/*.json` | any job (`shed lease NAME --ttl N`) → host | "keep the laptop awake while I run"; expired leases groomed automatically |
| `digest.jsonl` | all runtime modules → morning digest | append-only event log |

External cooperators: `~/.claude/state/rate-limits.json` (budget data plane —
statusline capture hook + mitm sniffer), workflow-watcher's `active_run.json`
(live headless run; the host treats it as work, and the continuation worker serializes around it).

## governor.json shape

```json
{
  "computed_at": 1780600000, "fresh": true, "data_age_s": 12,
  "five_hour": {"used_pct": 42.0, "resets_at": 1780610000, "minutes_to_reset": 166},
  "seven_day": {"used_pct": 30.0, "resets_at": 1780851600, "minutes_to_reset": 4193,
                 "elapsed_fraction": 0.58},
  "pace_delta": -28.0,
  "wall_imminent": false,
  "fanout_scale": 1.56,
  "wall_pct": 95.0
}
```

- **Gate vs dial (locked-in model):** per-session token burn is the GATE
  (compact-guard owns it); the quota here is only a DIAL. `fanout_scale`
  scales ambition (0 = wall, 2 = far behind weekly pace); `wall_imminent`
  is a checkpoint signal, not a work gate.
- `wall_imminent` is **never** true on stale data — the reactive path
  (watch.py parsing a walled run's output) covers the blind stretch when no
  interactive session refreshes rate-limits.json.

## Trigger policy — who may touch what (locked-in contract)

Every Claude session has exactly one **origin**, and every lifecycle trigger
keys off it. The tag is `SHED_ORIGIN=phone`, set by watch.py on the claude
processes it spawns; absence of the tag = interactive.

| trigger | `interactive` (your terminals) | `phone` (watcher-spawned headless) |
|---|---|---|
| wall imminent (≥95%, fresh) | compact-guard warns + handoff ID — **nothing is killed, ever** | runtime SIGINTs the pgid → Stop-hook handoff + git checkpoint → continuation queue |
| hit wall blind | compact-guard covers next prompt; you decide | watch.py parses the corpse → walled re-queue |
| window resets | nothing — you continue when you want | auto-respawn `--continue` at resets_at+120s |
| Stop hook (turn end) | handoff doc + pending-inject.md + threshold learning | handoff doc ONLY (no pending-inject, no learner) |
| pending-inject.md | consumed one-shot on next prompt | **never read, never written** |
| compact-guard interjections | yes (token warn + /fresh nudge) | **exit 0** — no human exists to answer them |
| long iteration | n/a (you drive) | `kind=goal`: relaunch cycles until `GOAL-COMPLETE` or deadline |
| respawn cap | n/a | build: 8 attempts; goal: deadline-bound (`expires_at`, default 48h) |
| closing the session | **only you** — explicit `shed park` is the sole handover verb | runtime owns the lifecycle; STOP kills |

**Invariant:** the runtime only ever signals pids it spawned (the
`active_run.json` lock written by watch.py). There is no code path that
closes, signals, or recycles an interactive terminal. `shed park [DIR]
[--goal "..." --hours N]` is the explicit opt-in: it queues the workspace and
the respawn's `claude --continue` carries that conversation on headlessly —
the terminal window itself stays yours.

**Goal runs** (`workflow goal: <title>` from the phone, or `shed park --goal`):
the outer loop lives in the harness, not the session — each cycle ends, watch.py
re-queues (+180s) unless the final message's last line is `GOAL-COMPLETE` or
`expires_at` passed (body directives `hours:`/`days:` override the 48h default).
Wall pauses interleave transparently; email-back on cycle 0 and every 10th.

## The pause→continuation loop (S1-E3)

1. **Proactive:** fresh data + `five_hour.used_pct ≥ 95` + a live headless run
   → SIGINT its process group. claude checkpoints (Stop hook → handoff-writer),
   the watch.py wrapper traps SIGINT (no SIGKILL teardown) and finalizes as
   `paused`, the continuation worker git-snapshots the workspace and queues
   a continuation time of `resets_at + 120s`.
2. **Reactive:** a run that dies with "usage limit reached" in its output is
   re-queued by watch.py itself (`walled`), timed from governor's last fresh
   `resets_at`, else exponential backoff capped at 4h.
3. **Respawn:** first eligible tick, with no active run and no STOP →
   relaunch through `watch.py --run` with a continuation flag →
   `claude --continue -p` in the same workspace: full conversation context
   back, email-back summary on completion, attempts capped at 8.

## Always-on host (S1-E1)

- Layer 1 `caffeinate -ims` child — lid-open protection, no root.
- Layer 2 `sudo -n pmset -a disablesleep 1` — the only thing that survives a
  closed lid. Asserted **only** while (tracked work ∧ AC power ∧ no STOP);
  cleared otherwise, every tick, and re-asserted after reboots. Needs the
  one-line sudoers rule `scripts/install-runtime.sh` prints (scoped to the
  two exact pmset commands). On battery the runtime never disables sleep —
  closed-lid-in-a-backpack thermal safety beats continuity.

## Remote ingress (S1-E4)

Email subjects (from the owner's own address only):
- `workflow <title>` + body → autonomous build run (serial queue, unchanged)
- `workflow stop` → STOP + SIGINT the active run; ack emailed back
- `workflow go` / continuation command → clear STOP; acknowledgement is sent back
- `workflow status` → active run, kill-switch, budget, continuation queue, last runs

## CLI

```bash
shed runtime-tick        # one heartbeat (what launchd calls)
shed governor [--json]   # budget report, both clocks
shed stop [--keep-running] / shed go
shed lease NAME --ttl 3600 [--release]
shed digest [--send]     # last-24h runtime digest (auto-emails ~08:30 daily)
```

## Install / verify

```bash
bash ~/Documents/Dev/shed/scripts/install-runtime.sh   # plist + prints sudoers step
shed governor            # → both clocks render
shed runtime-tick        # → '[]' or actions list, no traceback
pmset -g | grep -i sleepdisabled   # 1 while work runs on AC, else 0
```

S2 probes (from `shed-ho-5e9a0002`): `pmset -g`/`pgrep caffeinate` (host),
`rate-limits.json` freshness + `guard-thresholds.json` (governor),
settings hooks + `pending-inject.md` one-shot (handoff loop),
`launchctl list | grep -i workflow` (ingress).
