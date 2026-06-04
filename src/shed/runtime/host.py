"""S1-E1 · Always-on host.

Keep the laptop awake — lid closed included — while, and ONLY while, tracked
work is running. Two mechanisms, layered:

1. ``caffeinate -ims`` child (user-space, no root): blocks idle/disk/AC-system
   sleep while the lid is OPEN. We own the child and kill it when work stops.
2. ``sudo -n pmset -a disablesleep`` (root, via a single NOPASSWD sudoers line
   that ``scripts/install-runtime.sh`` prints): the only thing that survives a
   CLOSED lid on Apple Silicon. Asserted only when work is active AND we're on
   AC power — never cook a battery in a backpack. Cleared the moment work
   stops, STOP is engaged, or AC drops.

"Tracked work" = a live workflow-watcher run, a pending resume-queue entry,
or an unexpired keepawake lease (the generic hook S2's conductor uses).

Fail-open: if the sudoers line isn't installed, ``sudo -n`` fails quietly and
we degrade to caffeinate-only (lid must stay open) — logged, never fatal.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from shed.config import state_dir

from . import (
    atomic_write_json,
    digest_event,
    leases_dir,
    now_epoch,
    pid_alive,
    read_json,
    resume_queue_dir,
    stop_engaged,
)

PMSET = "/usr/bin/pmset"
CAFFEINATE = "/usr/bin/caffeinate"


def watcher_dir() -> Path:
    return Path(
        os.environ.get(
            "SHED_WATCHER_DIR",
            str(Path.home() / "Library/Application Support/workflow-watcher"),
        )
    ).expanduser()


def caffeinate_pid_path() -> Path:
    return state_dir() / "caffeinate.pid"


# ---------------------------------------------------------------------------
# Work detection
# ---------------------------------------------------------------------------


def detect_work(now: float | None = None) -> list[str]:
    """Reasons the host must stay awake. Empty list = let it sleep."""
    now = now or now_epoch()
    reasons: list[str] = []

    active = read_json(watcher_dir() / "active_run.json")
    if active and pid_alive(active.get("pid")):
        reasons.append(f"run:{active.get('slug', '?')}")

    qdir = resume_queue_dir()
    if qdir.is_dir():
        for f in sorted(qdir.glob("*.json")):
            entry = read_json(f)
            if entry:
                reasons.append(f"resume:{f.stem}")

    ldir = leases_dir()
    if ldir.is_dir():
        for f in sorted(ldir.glob("*.json")):
            lease = read_json(f)
            if not lease:
                continue
            if float(lease.get("expires_at") or 0) > now:
                reasons.append(f"lease:{f.stem}")
            else:
                f.unlink(missing_ok=True)  # expired — groom as we go

    return reasons


def lease(name: str, ttl_s: int, holder: str = "") -> Path:
    """Grant/refresh a keepawake lease. S2's conductor calls this per job."""
    d = leases_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    atomic_write_json(
        p, {"name": name, "holder": holder, "expires_at": now_epoch() + ttl_s}
    )
    return p


def release(name: str) -> bool:
    p = leases_dir() / f"{name}.json"
    if p.exists():
        p.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Power state
# ---------------------------------------------------------------------------


def on_ac_power() -> bool:
    """Parse ``pmset -g batt`` — first line says 'AC Power' or 'Battery Power'."""
    try:
        out = subprocess.run(
            [PMSET, "-g", "batt"], capture_output=True, text=True, timeout=10
        ).stdout
        return "AC Power" in out.splitlines()[0]
    except Exception:
        return False  # unknown → treat as battery: never disablesleep blind


def sleep_disabled() -> bool | None:
    """Current ``SleepDisabled`` state, or None if undeterminable."""
    try:
        out = subprocess.run(
            [PMSET, "-g"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            if "SleepDisabled" in line:
                return line.split()[-1] == "1"
        return False  # key absent = not disabled
    except Exception:
        return None


def set_sleep_disabled(want: bool) -> bool:
    """Toggle via the NOPASSWD sudoers line. False = couldn't (not installed)."""
    r = subprocess.run(
        ["/usr/bin/sudo", "-n", PMSET, "-a", "disablesleep", "1" if want else "0"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Caffeinate child
# ---------------------------------------------------------------------------


def _caffeinate_pid() -> int | None:
    d = read_json(caffeinate_pid_path())
    pid = d.get("pid") if d else None
    return pid if pid_alive(pid) else None


def _start_caffeinate() -> int:
    proc = subprocess.Popen(
        [CAFFEINATE, "-ims"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    atomic_write_json(caffeinate_pid_path(), {"pid": proc.pid})
    return proc.pid


def _stop_caffeinate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    caffeinate_pid_path().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Reconcile — the per-tick state machine
# ---------------------------------------------------------------------------


def reconcile(now: float | None = None) -> dict:
    """Drive actual host state toward desired state. Returns what happened."""
    now = now or now_epoch()
    reasons = detect_work(now)
    stopped = stop_engaged()
    want_awake = bool(reasons) and not stopped
    ac = on_ac_power()

    actions: list[str] = []

    # Layer 1: caffeinate child (lid-open protection).
    cpid = _caffeinate_pid()
    if want_awake and cpid is None:
        cpid = _start_caffeinate()
        actions.append(f"caffeinate-start:{cpid}")
    elif not want_awake and cpid is not None:
        _stop_caffeinate(cpid)
        actions.append("caffeinate-stop")

    # Layer 2: disablesleep (lid-closed protection). AC-gated, STOP-gated.
    want_disable = want_awake and ac
    current = sleep_disabled()
    if current is not None and current != want_disable:
        if set_sleep_disabled(want_disable):
            actions.append(f"disablesleep:{'1' if want_disable else '0'}")
        elif want_disable:
            actions.append("disablesleep-unavailable")  # sudoers line missing

    state = {
        "at": int(now),
        "want_awake": want_awake,
        "reasons": reasons,
        "ac_power": ac,
        "stop": stopped,
        "caffeinate_pid": _caffeinate_pid(),
        "sleep_disabled": sleep_disabled(),
        "actions": actions,
    }
    if actions:
        digest_event("host", actions=actions, reasons=reasons, ac=ac)
    return state
