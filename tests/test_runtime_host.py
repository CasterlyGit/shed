"""S1-E1: host — work detection, leases, the reconcile state machine."""

from __future__ import annotations

import json
import os
import time

import pytest

from shed.runtime import host, resume_queue_dir, stop_flag


@pytest.fixture()
def watcher_dir(tmp_path, monkeypatch):
    d = tmp_path / "watcher"
    d.mkdir()
    monkeypatch.setenv("SHED_WATCHER_DIR", str(d))
    return d


def _fake_power(monkeypatch, ac=True, disabled=False):
    calls = {"set": []}
    monkeypatch.setattr(host, "on_ac_power", lambda: ac)
    monkeypatch.setattr(host, "sleep_disabled", lambda: disabled)
    monkeypatch.setattr(host, "set_sleep_disabled",
                        lambda want: calls["set"].append(want) or True)
    monkeypatch.setattr(host, "_start_caffeinate", lambda: 99999999)
    monkeypatch.setattr(host, "_stop_caffeinate", lambda pid: None)
    monkeypatch.setattr(host, "_caffeinate_pid", lambda: None)
    return calls


def test_no_work_no_reasons(watcher_dir):
    assert host.detect_work() == []


def test_live_watcher_run_is_work(watcher_dir):
    (watcher_dir / "active_run.json").write_text(
        json.dumps({"pid": os.getpid(), "slug": "demo"})
    )
    assert host.detect_work() == ["run:demo"]


def test_dead_pid_is_not_work(watcher_dir):
    (watcher_dir / "active_run.json").write_text(
        json.dumps({"pid": 99999999, "slug": "demo"})
    )
    assert host.detect_work() == []


def test_resume_queue_is_work(watcher_dir):
    q = resume_queue_dir()
    q.mkdir(parents=True)
    (q / "job.json").write_text(json.dumps({"slug": "job"}))
    assert host.detect_work() == ["resume:job"]


def test_lease_grant_expire_release(watcher_dir):
    host.lease("s2-conductor", ttl_s=60)
    assert host.detect_work() == ["lease:s2-conductor"]
    assert host.release("s2-conductor") is True
    assert host.detect_work() == []
    # Expired lease is groomed away on detection.
    p = host.lease("old", ttl_s=60)
    data = json.loads(p.read_text())
    data["expires_at"] = time.time() - 5
    p.write_text(json.dumps(data))
    assert host.detect_work() == []
    assert not p.exists()


def test_reconcile_work_on_ac_disables_sleep(watcher_dir, monkeypatch):
    calls = _fake_power(monkeypatch, ac=True, disabled=False)
    host.lease("job", ttl_s=600)
    state = host.reconcile()
    assert state["want_awake"] is True
    assert calls["set"] == [True]
    assert any(a.startswith("caffeinate-start") for a in state["actions"])


def test_reconcile_battery_never_disables_sleep(watcher_dir, monkeypatch):
    calls = _fake_power(monkeypatch, ac=False, disabled=False)
    host.lease("job", ttl_s=600)
    state = host.reconcile()
    assert state["want_awake"] is True
    assert calls["set"] == []  # current(False) == want(False): no toggle


def test_reconcile_battery_clears_existing_disablesleep(watcher_dir, monkeypatch):
    calls = _fake_power(monkeypatch, ac=False, disabled=True)
    host.lease("job", ttl_s=600)
    host.reconcile()
    assert calls["set"] == [False]


def test_reconcile_stop_overrides_work(watcher_dir, monkeypatch):
    calls = _fake_power(monkeypatch, ac=True, disabled=True)
    host.lease("job", ttl_s=600)
    stop_flag().parent.mkdir(parents=True, exist_ok=True)
    stop_flag().write_text("now")
    state = host.reconcile()
    assert state["want_awake"] is False
    assert calls["set"] == [False]


def test_reconcile_idle_clears_everything(watcher_dir, monkeypatch):
    calls = _fake_power(monkeypatch, ac=True, disabled=True)
    state = host.reconcile()
    assert state["want_awake"] is False
    assert calls["set"] == [False]
