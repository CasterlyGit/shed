"""S1-E3 · Pause → handoff → resume, with nobody at the screen.

Two trigger paths into one resume queue:

- **Proactive** (fresh budget data): governor says ``wall_imminent`` → SIGINT
  the active headless run's process group. claude -p stops gracefully — its
  Stop hook fires handoff-writer, the watcher wrapper's KeyboardInterrupt
  handler snapshots + finalizes as "paused" — and we enqueue a resume entry
  timed to ``five_hour.resets_at``.
- **Reactive** (blind stretch — headless runs don't refresh rate-limits.json):
  a run that dies ON the wall is detected by watch.py's finalize (usage-limit
  text in its output), which enqueues the entry itself. Same queue, same shape.

When ``resets_at`` passes (+buffer) and no run is active and STOP is absent,
we respawn through watch.py's ``--run`` wrapper with ``resume: true`` →
``claude --continue -p`` in the same workspace: full conversation context
back, finalize/email-back/active-lock all reused, nothing reinvented.

Also here: the STOP kill-switch enforcement and the morning digest
(yesterday's runtime events, emailed ~08:30 via the shared Gmail keychain
entry). Everything fail-open — a digest failure must never block a resume.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import smtplib
import subprocess
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from shed.config import state_dir

from . import (
    atomic_write_json,
    digest_event,
    digest_path,
    governor,
    host,
    now_epoch,
    pid_alive,
    read_json,
    resume_queue_dir,
    stop_engaged,
    stop_flag,
)

GRACE_S = int(os.environ.get("SHED_PAUSE_GRACE_S", "120"))
RESUME_BUFFER_S = 120          # spawn a little after resets_at, not on the dot
MAX_ATTEMPTS = 8               # build entries: respawn cap across walls
GOAL_MAX_ATTEMPTS = 500        # goal entries: deadline-bound (expires_at), not attempt-bound
DIGEST_HOUR = 8                # morning digest sent on first tick ≥ 08:30
DIGEST_MINUTE = 30
GMAIL_USER = os.environ.get("WORKFLOW_GMAIL_USER", "tarunsp23@gmail.com")
KEYCHAIN_SVC = os.environ.get("WORKFLOW_KEYCHAIN_SVC", "seeings-watcher-gmail")

RESUME_PROMPT = (
    "The 5-hour usage window has reset. You were paused mid-run by the shed S1 "
    "runtime (autonomous pause→resume loop). Re-read _run.json, `git log`, and "
    "your own previous work in this workspace, then CONTINUE the original task "
    "autonomously to completion. Do not start over; do not ask questions."
)

RESUME_PROMPT_GOAL = (
    "You are an autonomous GOAL run managed by the shed S1 runtime; your previous "
    "iteration ended (5h-wall pause or end-of-cycle). Re-read _run.json, `git log`, "
    "and your own previous work in this workspace, then keep iterating toward the "
    "goal:\n\n{goal}\n\nWork in plan → execute → verify cycles. Print the single "
    "line GOAL-COMPLETE as the last line of your final message ONLY when the goal "
    "is fully met and verified; otherwise end with a concise progress note — the "
    "harness relaunches you with full context until the deadline. Do not start "
    "over; do not ask questions."
)


def watch_py() -> Path:
    return host.watcher_dir() / "watch.py"


# ---------------------------------------------------------------------------
# Active-run + queue plumbing
# ---------------------------------------------------------------------------


def active_run() -> dict | None:
    a = read_json(host.watcher_dir() / "active_run.json")
    if a and pid_alive(a.get("pid")):
        return a
    return None


def enqueue_resume(
    slug: str,
    workspace: str,
    title: str = "",
    model: str = "",
    resume_at: float | None = None,
    reason: str = "wall-imminent",
    attempts: int = 0,
    kind: str = "build",
    goal: str = "",
    expires_at: float = 0,
    max_attempts: int | None = None,
) -> Path:
    d = resume_queue_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.json"
    atomic_write_json(
        p,
        {
            "slug": slug,
            "title": title or slug,
            "workspace": workspace,
            "model": model,
            "resume_at": int(resume_at or (now_epoch() + 1800)),
            "reason": reason,
            "attempts": attempts,
            "kind": kind,
            "goal": goal,
            "expires_at": int(expires_at),
            "max_attempts": int(max_attempts or (GOAL_MAX_ATTEMPTS if kind == "goal" else MAX_ATTEMPTS)),
            "queued_at": int(now_epoch()),
        },
    )
    digest_event("enqueue-resume", slug=slug, reason=reason, job_kind=kind,
                 resume_at=int(resume_at or 0))
    return p


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)  # watcher runs are session leaders → pgid == pid
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, sig)


def pause_active_run(active: dict, gov: dict, sleep_fn=time.sleep) -> None:
    """Gracefully stop the run's process group; claude's Stop hooks do the rest."""
    pid = int(active["pid"])
    slug = active.get("slug", "?")
    digest_event("pause-start", slug=slug, pid=pid, reason="wall-imminent")

    _kill_group(pid, signal.SIGINT)
    deadline = now_epoch() + GRACE_S
    while pid_alive(pid) and now_epoch() < deadline:
        sleep_fn(2)
    if pid_alive(pid):
        _kill_group(pid, signal.SIGTERM)
        deadline = now_epoch() + 15
        while pid_alive(pid) and now_epoch() < deadline:
            sleep_fn(1)
    if pid_alive(pid):
        _kill_group(pid, signal.SIGKILL)

    ws = active.get("workspace", "")
    if ws and Path(ws).is_dir():
        subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=shed-runtime", "-c", "user.email=runtime@local",
             "commit", "-m", "shed-runtime: checkpoint at 5h-wall pause"],
            cwd=ws, capture_output=True,
        )

    fh = (gov.get("five_hour") or {})
    resets_at = fh.get("resets_at") or 0
    run_state = read_json(Path(ws) / "_run.json") if ws else None
    rs = run_state or {}
    # Carry kind/goal/deadline/attempts across the wall — a goal job mid-flight
    # must come back as a goal job, with its history intact (not reset to zero).
    enqueue_resume(
        slug=slug,
        workspace=ws,
        title=active.get("title", slug),
        model=rs.get("model", ""),
        resume_at=(resets_at + RESUME_BUFFER_S) if resets_at else None,
        reason="wall-imminent",
        attempts=int(rs.get("resume_attempt", 0)),
        kind=rs.get("kind", "build"),
        goal=rs.get("goal", ""),
        expires_at=float(rs.get("expires_at", 0) or 0),
    )
    digest_event("pause-done", slug=slug)


def respawn(entry: dict) -> int | None:
    """Relaunch a paused run through watch.py --run (resume mode), detached."""
    ws = Path(entry["workspace"])
    if not ws.is_dir():
        digest_event("resume-skip", slug=entry.get("slug"), reason="workspace-missing")
        return None
    statefile = ws / "_run.json"
    state = read_json(statefile) or {}
    kind = entry.get("kind") or state.get("kind") or "build"
    goal = entry.get("goal") or state.get("goal") or ""
    prompt = RESUME_PROMPT_GOAL.format(goal=goal) if kind == "goal" else RESUME_PROMPT
    state.update(
        {
            "slug": entry["slug"],
            "title": entry.get("title", entry["slug"]),
            "prompt": prompt,
            "workspace": str(ws),
            "model": entry.get("model") or state.get("model") or "",
            "resume": True,
            "status": "resuming",
            "resume_attempt": int(entry.get("attempts", 0)) + 1,
            "kind": kind,
            "goal": goal,
            "expires_at": int(entry.get("expires_at") or state.get("expires_at") or 0),
            "queued_at": datetime.now().isoformat(),
        }
    )
    atomic_write_json(statefile, state)

    boot_log = open(ws / "_boot.log", "a")
    proc = subprocess.Popen(
        ["/usr/bin/python3", str(watch_py()), "--run", str(statefile)],
        stdout=boot_log,
        stderr=boot_log,
        start_new_session=True,
        env={**os.environ},
    )
    atomic_write_json(
        host.watcher_dir() / "active_run.json",
        {
            "pid": proc.pid,
            "slug": entry["slug"],
            "title": entry.get("title", entry["slug"]),
            "workspace": str(ws),
            "started_at": datetime.now().isoformat(),
            "status": "running",
        },
    )
    digest_event("resume-spawn", slug=entry["slug"], pid=proc.pid,
                 attempt=state["resume_attempt"])
    return proc.pid


def process_resume_queue(gov: dict, now: float | None = None) -> list[str]:
    """Respawn due entries. One per tick — runs are serial by design."""
    now = now or now_epoch()
    actions: list[str] = []
    qdir = resume_queue_dir()
    if not qdir.is_dir():
        return actions
    done = qdir / "done"
    for f in sorted(qdir.glob("*.json")):
        entry = read_json(f)
        if not entry:
            f.unlink(missing_ok=True)
            continue
        cap = int(entry.get("max_attempts") or MAX_ATTEMPTS)
        if int(entry.get("attempts", 0)) >= cap:
            done.mkdir(exist_ok=True)
            f.rename(done / f"{f.stem}.exhausted.json")
            digest_event("resume-exhausted", slug=entry.get("slug"))
            actions.append(f"exhausted:{entry.get('slug')}")
            continue
        exp = float(entry.get("expires_at") or 0)
        if exp and now >= exp:
            done.mkdir(exist_ok=True)
            f.rename(done / f"{f.stem}.expired.json")
            digest_event("resume-expired", slug=entry.get("slug"), job_kind=entry.get("kind"))
            actions.append(f"expired:{entry.get('slug')}")
            continue
        if now < float(entry.get("resume_at") or 0):
            continue
        if stop_engaged() or active_run() is not None:
            break  # STOP or serial-run guard — try again next tick
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        pid = respawn(entry)
        if pid:
            done.mkdir(exist_ok=True)
            atomic_write_json(done / f"{f.stem}.{int(now)}.json", entry)
            f.unlink(missing_ok=True)
            actions.append(f"respawn:{entry['slug']}")
        break  # at most one respawn per tick
    return actions


def park(
    workspace: str,
    title: str = "",
    goal: str = "",
    hours: float = 48.0,
    model: str = "",
) -> Path:
    """Explicit interactive→autonomous handover (`shed park`).

    The ONLY path by which terminal work enters the autonomous loop — never
    automatic. Queues this workspace for immediate pickup; the respawn's
    ``claude --continue`` resumes the most recent conversation in that
    directory, so the parked terminal session continues headlessly with full
    context. The terminal itself is yours to close (or not) — the runtime
    never touches it.
    """
    ws = Path(workspace).expanduser().resolve()
    base = re.sub(r"[^a-z0-9]+", "-", (title or ws.name).lower()).strip("-")[:40] or "parked"
    slug = base
    n = 1
    while (resume_queue_dir() / f"{slug}.json").exists():
        n += 1
        slug = f"{base}-{n}"
    kind = "goal" if goal else "build"
    p = enqueue_resume(
        slug=slug,
        workspace=str(ws),
        title=title or ws.name,
        model=model,
        resume_at=now_epoch(),  # due immediately — next tick picks it up when idle
        reason="parked",
        kind=kind,
        goal=goal,
        expires_at=(now_epoch() + hours * 3600) if kind == "goal" else 0,
    )
    digest_event("parked", slug=slug, job_kind=kind)
    return p


# ---------------------------------------------------------------------------
# STOP kill-switch
# ---------------------------------------------------------------------------


def engage_stop(kill_active: bool = True) -> dict:
    stop_flag().parent.mkdir(parents=True, exist_ok=True)
    stop_flag().write_text(datetime.now().isoformat())
    killed = None
    if kill_active:
        a = active_run()
        if a:
            _kill_group(int(a["pid"]), signal.SIGINT)
            killed = a.get("slug")
    digest_event("stop-engaged", killed=killed)
    return {"stop": True, "killed": killed}


def clear_stop() -> bool:
    if stop_flag().exists():
        stop_flag().unlink()
        digest_event("stop-cleared")
        return True
    return False


# ---------------------------------------------------------------------------
# Morning digest
# ---------------------------------------------------------------------------


def _last_digest_marker() -> Path:
    return state_dir() / "last-digest-sent"


def _send_email(subject: str, body: str) -> bool:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", GMAIL_USER,
             "-s", KEYCHAIN_SVC, "-w"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return False
        msg = EmailMessage()
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_USER
        msg["Subject"] = subject
        msg["X-Workflow-Watcher"] = "digest"  # never re-ingested as a trigger
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, r.stdout.strip())
            s.send_message(msg)
        return True
    except Exception:
        return False


def compose_digest(since: float, gov: dict) -> str:
    events: list[dict] = []
    try:
        for line in digest_path().read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if float(e.get("epoch") or 0) >= since:
                events.append(e)
    except FileNotFoundError:
        pass

    lines = [f"shed runtime digest — {datetime.now().strftime('%a %Y-%m-%d %H:%M')}", ""]
    fh, sd = gov.get("five_hour") or {}, gov.get("seven_day") or {}
    lines.append(
        f"budget: 5h {fh.get('used_pct', '?')}% · week {sd.get('used_pct', '?')}% "
        f"(pace {gov.get('pace_delta', '?')}) · fanout ×{gov.get('fanout_scale', '?')}"
    )
    lines.append(f"kill-switch: {'ENGAGED' if stop_engaged() else 'off'}")
    lines.append("")
    if not events:
        lines.append("no runtime events in the window — quiet night.")
    else:
        counts: dict[str, int] = {}
        for e in events:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        lines.append(f"{len(events)} events: " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())))
        lines.append("")
        for e in events[-40:]:
            extra = {k: v for k, v in e.items() if k not in ("ts", "epoch", "kind")}
            lines.append(f"  {e.get('ts', '')}  {e['kind']}  {json.dumps(extra) if extra else ''}".rstrip())
    return "\n".join(lines) + "\n"


def maybe_send_digest(gov: dict, now: float | None = None) -> bool:
    """First tick at/after 08:30 local each day → email yesterday's events."""
    if os.environ.get("SHED_DIGEST_DISABLE"):
        return False
    now_dt = datetime.fromtimestamp(now or now_epoch())
    if (now_dt.hour, now_dt.minute) < (DIGEST_HOUR, DIGEST_MINUTE):
        return False
    marker = _last_digest_marker()
    today = now_dt.strftime("%Y-%m-%d")
    try:
        if marker.read_text().strip() == today:
            return False
    except FileNotFoundError:
        pass
    body = compose_digest(since=(now or now_epoch()) - 86400, gov=gov)
    sent = _send_email("⛅ shed runtime — morning digest", body)
    # Mark even on send failure: one attempt per day, never a retry storm.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(today)
    digest_event("digest-sent" if sent else "digest-failed")
    return sent


# ---------------------------------------------------------------------------
# The tick — launchd entrypoint (StartInterval 60)
# ---------------------------------------------------------------------------


def tick(now: float | None = None) -> dict:
    """One runtime heartbeat: governor → host → pause/resume → digest."""
    lock = open(state_dir() / "runtime-tick.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"skipped": "tick-overlap"}

    try:
        now = now or now_epoch()
        gov = governor.tick(now)
        host_state = host.reconcile(now)

        actions: list[str] = list(host_state.get("actions", []))
        a = active_run()

        if stop_engaged():
            if a:
                _kill_group(int(a["pid"]), signal.SIGINT)
                digest_event("stop-enforced", slug=a.get("slug"))
                actions.append(f"stop-killed:{a.get('slug')}")
        elif gov.get("wall_imminent") and a:
            pause_active_run(a, gov)
            actions.append(f"paused:{a.get('slug')}")
        else:
            actions += process_resume_queue(gov, now)

        maybe_send_digest(gov, now)
        return {"gov": gov, "host": host_state, "actions": actions}
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()
