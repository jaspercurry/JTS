# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import subprocess

from jasper.control.ha_status_cache import HomeAssistantStatusCache


def _inline_thread(target):
    target()
    return object()


def test_snapshot_starts_background_probe_and_returns_checking(monkeypatch):
    calls = []
    signature = ["a"]
    cache = HomeAssistantStatusCache(
        ttl_sec=30,
        thread_factory=_inline_thread,
        signature_reader=lambda: signature[0],
    )

    def fake_run_child():
        calls.append("probe")
        return {
            "configured": True,
            "connected": True,
            "url": "http://ha.local:8123",
            "instance_name": "Home",
            "version": "2026.6.1",
            "error": None,
        }

    monkeypatch.setattr(cache, "_run_child", fake_run_child)

    first = cache.snapshot()
    second = cache.snapshot()

    assert first["checking"] is True
    assert second["configured"] is True
    assert second["connected"] is True
    assert calls == ["probe"]


def test_snapshot_refreshes_when_wizard_env_signature_changes(monkeypatch):
    calls = []
    signature = ["a"]
    cache = HomeAssistantStatusCache(
        ttl_sec=30,
        thread_factory=_inline_thread,
        signature_reader=lambda: signature[0],
    )

    def fake_run_child():
        calls.append(signature[0])
        return {
            "configured": True,
            "connected": True,
            "url": "http://ha-" + signature[0],
            "instance_name": "Home",
            "version": "2026.6.1",
            "error": None,
        }

    monkeypatch.setattr(cache, "_run_child", fake_run_child)

    cache.snapshot()
    assert cache.snapshot()["url"] == "http://ha-a"
    signature[0] = "b"

    changed = cache.snapshot()
    refreshed = cache.snapshot()

    assert changed["checking"] is True
    assert changed["stale"] is True
    assert refreshed["url"] == "http://ha-b"
    assert calls == ["a", "b"]


def test_snapshot_does_not_spawn_duplicate_refreshes(monkeypatch):
    calls = []

    def hold_thread(target):
        calls.append(target)
        return object()

    cache = HomeAssistantStatusCache(
        ttl_sec=30,
        min_refresh_interval_sec=30,
        thread_factory=hold_thread,
    )

    assert cache.snapshot()["checking"] is True
    assert cache.snapshot()["checking"] is True
    assert len(calls) == 1


def test_refresh_failure_keeps_stale_cached_status(monkeypatch):
    now = [0.0]
    outcomes = [
        {
            "configured": True,
            "connected": True,
            "url": "http://ha.local:8123",
            "instance_name": "Home",
            "version": "2026.6.1",
            "error": None,
        },
        RuntimeError("boom"),
    ]
    cache = HomeAssistantStatusCache(
        ttl_sec=1,
        thread_factory=_inline_thread,
        clock=lambda: now[0],
    )

    def fake_run_child():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cache, "_run_child", fake_run_child)

    cache.snapshot()
    assert cache.snapshot()["connected"] is True

    now[0] = 2.0
    stale = cache.snapshot()
    refreshed = cache.snapshot()

    assert stale["checking"] is True
    assert stale["stale"] is True
    assert refreshed["connected"] is True
    assert refreshed["stale"] is True
    assert refreshed["error"] == "probe failed"


def test_parent_logs_reachability_transitions_from_child_status(monkeypatch, caplog):
    now = [0.0]
    outcomes = [
        {
            "configured": True,
            "connected": True,
            "url": "http://ha.local:8123",
            "instance_name": "Home",
            "version": "2026.6.1",
            "error": None,
        },
        {
            "configured": True,
            "connected": False,
            "url": "http://ha.local:8123",
            "instance_name": None,
            "version": None,
            "error": "Couldn't reach Home Assistant.",
        },
        {
            "configured": False,
            "connected": False,
            "url": "",
            "instance_name": None,
            "version": None,
            "error": None,
        },
    ]
    cache = HomeAssistantStatusCache(
        ttl_sec=1,
        thread_factory=_inline_thread,
        clock=lambda: now[0],
    )
    caplog.set_level(logging.INFO, logger="jasper.control.ha_status_cache")
    monkeypatch.setattr(cache, "_run_child", lambda: outcomes.pop(0))

    cache.snapshot()
    assert any("event=ha.reachable" in r.message for r in caplog.records)

    caplog.clear()
    cache.snapshot()
    assert not caplog.records

    now[0] = 2.0
    cache.snapshot()
    assert any("event=ha.unreachable" in r.message for r in caplog.records)

    caplog.clear()
    now[0] = 4.0
    cache.snapshot()
    assert any("event=ha.unconfigured" in r.message for r in caplog.records)


def test_thread_start_failure_does_not_wedge_refreshing(monkeypatch):
    attempts = []
    now = [0.0]

    def failing_thread(_target):
        attempts.append("start")
        raise RuntimeError("no threads")

    cache = HomeAssistantStatusCache(
        ttl_sec=30,
        min_refresh_interval_sec=0.1,
        thread_factory=failing_thread,
        clock=lambda: now[0],
    )

    first = cache.snapshot()
    second = cache.snapshot()

    assert first["error"] == "probe failed"
    assert second["checking"] is True
    assert attempts == ["start"]


def test_run_child_invokes_probe_module(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"configured":false,"connected":false,"url":"","error":null}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    cache = HomeAssistantStatusCache(python_exe="/opt/jasper/.venv/bin/python")
    status = cache._run_child()

    assert seen["cmd"] == [
        "/opt/jasper/.venv/bin/python",
        "-m",
        "jasper.control.ha_probe_child",
    ]
    assert seen["kwargs"]["timeout"] > 0
    assert seen["kwargs"]["capture_output"] is True
    assert status["configured"] is False


def test_run_child_raises_on_child_probe_failure(monkeypatch):
    def fake_run(cmd, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout='{"configured":false,"connected":false,"error":"probe failed"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    cache = HomeAssistantStatusCache()
    try:
        cache._run_child()
    except RuntimeError as exc:
        assert "child exited 1" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected child failure")
