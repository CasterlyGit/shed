"""S1 autonomous runtime — the conductor's foundation.

Four epics, three modules, one launchd tick (``shed runtime-tick``):

- ``governor``  — S1-E2: both budget clocks (5h rolling + weekly reset),
  pacing math, and the ``governor.json`` report S2 scales fan-out from.
- ``host``      — S1-E1: keep the laptop awake (lid closed included) while —
  and only while — tracked work is running and we're on AC power.
- ``resumed``   — S1-E3: pause→handoff→resume across 5h walls with nobody at
  the screen, the STOP kill-switch, and the morning digest.

S1-E4 (remote ingress) lives in workflow-watcher's ``watch.py``; it talks to
this package purely through the state-file contract below.

State-file contract (everything under ``~/.shed/state/``):

- ``governor.json``            budget report, refreshed every tick
- ``STOP``                     kill-switch; presence halts pause/respawn/keepawake
- ``resume-queue/<slug>.json`` runs waiting for a window reset
- ``keepawake-leases/*.json``  work leases ({"expires_at": epoch}) — anything
                               (S2 conductor, scripts) may hold one
- ``digest.jsonl``             append-only runtime event log
- ``caffeinate.pid``           pid of the assertion child we own

External (not ours, read/written cooperatively):

- ``~/.claude/state/rate-limits.json``                    budget data plane
- workflow-watcher ``active_run.json`` + ``runs/*/_run.json``  headless runs
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from shed.config import state_dir


def now_epoch() -> float:
    return time.time()


def stop_flag() -> Path:
    return state_dir() / "STOP"


def stop_engaged() -> bool:
    return stop_flag().exists()


def resume_queue_dir() -> Path:
    return state_dir() / "resume-queue"


def leases_dir() -> Path:
    return state_dir() / "keepawake-leases"


def digest_path() -> Path:
    return state_dir() / "digest.jsonl"


def atomic_write_json(path: Path, data: dict) -> None:
    """Write-then-rename so readers never see a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def digest_event(kind: str, **fields) -> None:
    """Append one runtime event. Fail-open: the digest must never break a tick."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "epoch": int(now_epoch()), "kind": kind}
        rec.update(fields)
        p = digest_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours
    except Exception:
        return False
