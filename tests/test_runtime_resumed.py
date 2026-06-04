"""S1-E3: resumed — pause decisions, resume queue, STOP, digest."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from shed.runtime import digest_path, read_json, resume_queue_dir, resumed, stop_flag


@pytest.fixture()
def watcher_dir(tmp_path, monkeypatch):
    d = tmp_path / "watcher"
    d.mkdir()
    monkeypatch.setenv("SHED_WATCHER_DIR", str(d))
    return d


def _gov(fh_used=50.0, resets_in=3600, wall=False, now=None):
    now = now or time.time()
    return {
        "fresh": True,
        "wall_imminent": wall,
        "fanout_scale": 1.0,
        "pace_delta": 0.0,
        "five_hour": {"used_pct": fh_used, "resets_at": int(now + resets_in),
                      "minutes_to_reset": resets_in // 60},
        "seven_day": {"used_pct": 10.0, "resets_at": int(now + 86400)},
    }


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_enqueue_writes_entry(watcher_dir):
    p = resumed.enqueue_resume("job", "/tmp/ws", resume_at=12345, reason="walled")
    e = read_json(p)
    assert e["slug"] == "job" and e["resume_at"] == 12345 and e["attempts"] == 0


def test_not_due_entries_wait(watcher_dir):
    now = time.time()
    resumed.enqueue_resume("job", "/tmp/ws", resume_at=now + 999)
    assert resumed.process_resume_queue(_gov(), now=now) == []
    assert (resume_queue_dir() / "job.json").exists()


def test_due_entry_respawns_and_archives(watcher_dir, monkeypatch):
    now = time.time()
    spawned = []
    monkeypatch.setattr(resumed, "respawn", lambda e: spawned.append(e["slug"]) or 4242)
    resumed.enqueue_resume("job", "/tmp/ws", resume_at=now - 10)
    actions = resumed.process_resume_queue(_gov(), now=now)
    assert actions == ["respawn:job"]
    assert spawned == ["job"]
    assert not (resume_queue_dir() / "job.json").exists()
    assert list((resume_queue_dir() / "done").glob("job.*.json"))


def test_stop_blocks_respawn(watcher_dir, monkeypatch):
    now = time.time()
    monkeypatch.setattr(resumed, "respawn", lambda e: 4242)
    stop_flag().parent.mkdir(parents=True, exist_ok=True)
    stop_flag().write_text("x")
    resumed.enqueue_resume("job", "/tmp/ws", resume_at=now - 10)
    assert resumed.process_resume_queue(_gov(), now=now) == []
    assert (resume_queue_dir() / "job.json").exists()  # still queued


def test_active_run_blocks_respawn(watcher_dir, monkeypatch):
    now = time.time()
    monkeypatch.setattr(resumed, "respawn", lambda e: 4242)
    monkeypatch.setattr(resumed, "active_run", lambda: {"pid": 1, "slug": "other"})
    resumed.enqueue_resume("job", "/tmp/ws", resume_at=now - 10)
    assert resumed.process_resume_queue(_gov(), now=now) == []


def test_exhausted_entry_is_retired(watcher_dir):
    now = time.time()
    resumed.enqueue_resume("job", "/tmp/ws", resume_at=now - 10,
                           attempts=resumed.MAX_ATTEMPTS)
    actions = resumed.process_resume_queue(_gov(), now=now)
    assert actions == ["exhausted:job"]
    assert (resume_queue_dir() / "done" / "job.exhausted.json").exists()


def test_goal_entry_outlives_build_cap(watcher_dir, monkeypatch):
    """Goal runs are deadline-bound: attempts far past MAX_ATTEMPTS still respawn."""
    now = time.time()
    spawned = []
    monkeypatch.setattr(resumed, "respawn", lambda e: spawned.append(e["slug"]) or 1)
    resumed.enqueue_resume("marathon", "/tmp/ws", resume_at=now - 10, kind="goal",
                           goal="polish until done", attempts=resumed.MAX_ATTEMPTS + 30,
                           expires_at=now + 3600)
    assert resumed.process_resume_queue(_gov(), now=now) == ["respawn:marathon"]
    assert spawned == ["marathon"]


def test_expired_goal_entry_is_retired(watcher_dir, monkeypatch):
    now = time.time()
    monkeypatch.setattr(resumed, "respawn", lambda e: 1)
    resumed.enqueue_resume("late", "/tmp/ws", resume_at=now - 10, kind="goal",
                           goal="g", expires_at=now - 5)
    actions = resumed.process_resume_queue(_gov(), now=now)
    assert actions == ["expired:late"]
    assert (resume_queue_dir() / "done" / "late.expired.json").exists()


def test_park_enqueues_due_now(watcher_dir, tmp_path):
    ws = tmp_path / "myproj"
    ws.mkdir()
    p = resumed.park(str(ws), goal="ship v1", hours=24)
    e = read_json(p)
    assert e["reason"] == "parked" and e["kind"] == "goal"
    assert e["resume_at"] <= time.time() + 1          # due immediately
    assert 0 < e["expires_at"] - time.time() <= 24 * 3600 + 5
    assert e["max_attempts"] == resumed.GOAL_MAX_ATTEMPTS


def test_one_respawn_per_tick(watcher_dir, monkeypatch):
    now = time.time()
    spawned = []
    monkeypatch.setattr(resumed, "respawn", lambda e: spawned.append(e["slug"]) or 1)
    active = {"v": None}
    monkeypatch.setattr(resumed, "active_run", lambda: active["v"])
    resumed.enqueue_resume("a-job", "/tmp/a", resume_at=now - 10)
    resumed.enqueue_resume("b-job", "/tmp/b", resume_at=now - 10)
    resumed.process_resume_queue(_gov(), now=now)
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------


def test_pause_carries_goal_identity_across_wall(watcher_dir, tmp_path, monkeypatch):
    """A goal job paused at the wall must come back as a goal job: kind, goal
    text, deadline, and iteration count all survive into the queue entry."""
    ws = tmp_path / "ws"
    ws.mkdir()
    exp = int(time.time() + 7200)
    (ws / "_run.json").write_text(json.dumps(
        {"model": "opus", "kind": "goal", "goal": "make realm hit 60fps",
         "expires_at": exp, "resume_attempt": 4}))
    monkeypatch.setattr(resumed, "_kill_group", lambda pid, s: None)
    monkeypatch.setattr(resumed, "pid_alive", lambda p: False)
    monkeypatch.setattr(resumed.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    resumed.pause_active_run(
        {"pid": 4242, "slug": "realm", "title": "Realm", "workspace": str(ws)},
        _gov(wall=True), sleep_fn=lambda s: None,
    )
    e = read_json(resume_queue_dir() / "realm.json")
    assert e["kind"] == "goal" and e["goal"] == "make realm hit 60fps"
    assert e["expires_at"] == exp and e["attempts"] == 4
    assert e["max_attempts"] == resumed.GOAL_MAX_ATTEMPTS


def test_respawn_uses_goal_prompt(watcher_dir, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(resumed.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {"pid": 999})())
    pid = resumed.respawn({"slug": "j", "workspace": str(ws), "kind": "goal",
                           "goal": "ship the thing", "expires_at": 99, "attempts": 2})
    assert pid == 999
    st = read_json(ws / "_run.json")
    assert "ship the thing" in st["prompt"] and "GOAL-COMPLETE" in st["prompt"]
    assert st["kind"] == "goal" and st["resume_attempt"] == 3


def test_pause_kills_group_and_enqueues(watcher_dir, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "_run.json").write_text(json.dumps({"model": "opus"}))

    sigs = []
    alive = {"v": True}
    monkeypatch.setattr(resumed, "_kill_group",
                        lambda pid, s: (sigs.append(s), alive.update(v=False)))
    monkeypatch.setattr(resumed, "pid_alive", lambda p: alive["v"])
    monkeypatch.setattr(resumed.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())

    now = time.time()
    gov = _gov(fh_used=96.0, resets_in=1200, wall=True, now=now)
    resumed.pause_active_run(
        {"pid": 4242, "slug": "job", "title": "Job", "workspace": str(ws)},
        gov, sleep_fn=lambda s: None,
    )
    assert sigs[0] == resumed.signal.SIGINT  # graceful first
    entry = read_json(resume_queue_dir() / "job.json")
    assert entry["reason"] == "wall-imminent"
    assert entry["model"] == "opus"
    # resume_at honors resets_at + buffer
    assert abs(entry["resume_at"] - (gov["five_hour"]["resets_at"]
                                     + resumed.RESUME_BUFFER_S)) <= 1


def test_respawn_missing_workspace_skips(watcher_dir):
    assert resumed.respawn({"slug": "gone", "workspace": "/nope/nothing"}) is None


# ---------------------------------------------------------------------------
# STOP
# ---------------------------------------------------------------------------


def test_engage_and_clear_stop(watcher_dir, monkeypatch):
    killed = []
    monkeypatch.setattr(resumed, "active_run",
                        lambda: {"pid": 7, "slug": "job"})
    monkeypatch.setattr(resumed, "_kill_group", lambda pid, s: killed.append(pid))
    r = resumed.engage_stop()
    assert r["killed"] == "job" and killed == [7]
    assert stop_flag().exists()
    assert resumed.clear_stop() is True
    assert not stop_flag().exists()
    assert resumed.clear_stop() is False


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def test_compose_digest_includes_events_and_budget(watcher_dir):
    resumed.digest_event("pause-start", slug="job")
    resumed.digest_event("resume-spawn", slug="job")
    body = resumed.compose_digest(since=time.time() - 60, gov=_gov(fh_used=33.0))
    assert "pause-start" in body and "resume-spawn" in body
    assert "33.0%" in body


def test_digest_sent_once_per_day(watcher_dir, monkeypatch):
    sent = []
    monkeypatch.setattr(resumed, "_send_email",
                        lambda s, b: sent.append(s) or True)
    morning = datetime.now().replace(hour=9, minute=0).timestamp()
    assert resumed.maybe_send_digest(_gov(), now=morning) is True
    assert resumed.maybe_send_digest(_gov(), now=morning) is False
    assert len(sent) == 1


def test_digest_not_before_0830(watcher_dir, monkeypatch):
    monkeypatch.setattr(resumed, "_send_email", lambda s, b: True)
    early = datetime.now().replace(hour=7, minute=0).timestamp()
    assert resumed.maybe_send_digest(_gov(), now=early) is False


def test_digest_event_appends_jsonl(watcher_dir):
    resumed.digest_event("test-kind", foo="bar")
    lines = digest_path().read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["kind"] == "test-kind" and rec["foo"] == "bar"
