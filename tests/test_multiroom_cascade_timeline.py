# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.multiroom import cascade_timeline as ct


def test_classify_reconcile_restart_line():
    ev = ct.classify_journal_line(
        "jasper-grouping-reconcile",
        "event=multiroom.reconcile.unit_restarted unit=jasper-snapclient.service reason=leader",
        observed_at=123.0,
    )
    assert ev == {
        "occurred_at": 123.0,
        "observed_at": 123.0,
        "unit": "jasper-grouping-reconcile",
        "event": "multiroom.reconcile.unit_restarted",
        "severity": "action",
        "detail": "unit_restarted jasper-snapclient.service reason=leader",
        "fields": {"unit": "jasper-snapclient.service", "reason": "leader"},
    }


def test_classify_restart_broker_request_keeps_units_and_reason():
    ev = ct.classify_journal_line(
        "jasper-control",
        'event=restart_broker.request verb=restart units=jasper-grouping-reconcile.service reason="starved lane"',
        observed_at=10.0,
    )
    assert ev is not None
    assert ev["event"] == "restart_broker.request"
    assert ev["detail"] == "restart jasper-grouping-reconcile.service reason=starved lane"
    assert ev["fields"]["reason"] == "starved lane"


def test_classify_ignores_unrelated_event():
    assert ct.classify_journal_line(
        "jasper-control",
        "event=wifi_guardian.ok result=noop",
        observed_at=1.0,
    ) is None


def test_sampler_scans_into_bounded_ring():
    clock = [100.0]
    lines = {
        "jasper-control": [
            "event=grouping_supervisor.starved consecutive=3 threshold=3",
            "event=grouping_supervisor.starved_detected action=kick_reconcile count=1",
            "event=restart_broker.request verb=restart units=jasper-grouping-reconcile.service reason=starved",
        ],
        "jasper-grouping-reconcile": [
            "event=multiroom.reconcile.start reason=supervisor",
            "event=multiroom.reconcile.done rc=0",
        ],
    }

    windows = []

    def reader(unit, since, now):
        windows.append((unit, since, now))
        assert since <= now
        return [(99.0 + i, line) for i, line in enumerate(lines.get(unit, []))]

    sampler = ct.CascadeTimelineSampler(
        journal_lookback_sec=30.0,
        ring_size=3,
        journal_reader=reader,
        grouped_check=lambda: True,
        time_func=lambda: clock[0],
    )
    sampler._tick()
    snap = sampler.snapshot()
    assert snap["enabled"] is True
    assert snap["last_scan_at"] == 100.0
    assert snap["journal_lookback_sec"] == 30.0
    assert [e["event"] for e in snap["events"]] == [
        "restart_broker.request",
        "multiroom.reconcile.start",
        "multiroom.reconcile.done",
    ]
    assert [e["occurred_at"] for e in snap["events"]] == [101.0, 99.0, 100.0]
    assert {call[1] for call in windows} == {70.0}


def test_sampler_advances_cursor_after_initial_lookback():
    clock = [100.0]
    windows = []

    def reader(unit, since, now):
        windows.append((unit, since, now))
        return []

    sampler = ct.CascadeTimelineSampler(
        journal_lookback_sec=30.0,
        journal_reader=reader,
        grouped_check=lambda: True,
        time_func=lambda: clock[0],
    )
    sampler._tick()
    clock[0] = 115.0
    sampler._tick()

    first = windows[:len(ct.JOURNAL_UNITS)]
    second = windows[len(ct.JOURNAL_UNITS):]
    assert {call[1] for call in first} == {70.0}
    assert {call[2] for call in first} == {100.0}
    assert {call[1] for call in second} == {100.0}
    assert {call[2] for call in second} == {115.0}


def test_read_journal_lines_uses_journal_timestamp(monkeypatch):
    class Proc:
        returncode = 0
        stdout = (
            '{"__REALTIME_TIMESTAMP":"100500000",'
            '"MESSAGE":"event=restart_broker.request verb=restart units=x"}\n'
            '{"MESSAGE":"event=grouping_supervisor.starved"}\n'
            '{not json}\n'
        )

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(ct.subprocess, "run", fake_run)
    records = ct.CascadeTimelineSampler._read_journal_lines(
        "jasper-control", 90.0, 120.0,
    )
    assert records == [
        (100.5, "event=restart_broker.request verb=restart units=x"),
        (120.0, "event=grouping_supervisor.starved"),
    ]
    assert "-o" in calls[0]
    assert "json" in calls[0]
    # Belt-and-suspenders RAM cap on the 1 GB Pi: the scan must bound the read.
    assert "-n" in calls[0]
    assert str(ct.JOURNAL_SCAN_LINE_CAP) in calls[0]


def test_state_aggregate_cascade_snapshot_fails_soft(monkeypatch):
    from jasper.control import state_aggregate

    def boom():
        raise RuntimeError("sampler wedged")

    monkeypatch.setattr(state_aggregate.cascade_timeline, "snapshot", boom)
    assert state_aggregate._multiroom_cascade_snapshot() is None


def test_module_snapshot_default_disabled(monkeypatch):
    monkeypatch.setattr(ct, "_sampler", None)
    assert ct.snapshot() == {"enabled": False, "events": []}


# ---- solo gate ----


def _recording_reader(calls):
    def reader(unit, since, now):
        calls.append((unit, since, now))
        return []
    return reader


def test_solo_skips_journalctl_subprocess_work():
    """A solo speaker must not spawn the per-unit journalctl scan."""
    calls = []
    sampler = ct.CascadeTimelineSampler(
        journal_lookback_sec=30.0,
        journal_reader=_recording_reader(calls),
        grouped_check=lambda: False,
        time_func=lambda: 100.0,
    )
    sampler._tick()
    # No journal read happened for any unit.
    assert calls == []
    snap = sampler.snapshot()
    # /state honestly reports "no scan" rather than "scanned, found nothing".
    assert snap["last_scan_at"] is None
    assert snap["events"] == []


def test_solo_advances_cursor_so_later_bond_window_is_bounded():
    """While solo the cursor tracks `now`, so the first grouped tick scans a
    fresh short window instead of replaying a stale backlog."""
    clock = [100.0]
    calls = []
    grouped = [False]
    sampler = ct.CascadeTimelineSampler(
        journal_lookback_sec=30.0,
        journal_reader=_recording_reader(calls),
        grouped_check=lambda: grouped[0],
        time_func=lambda: clock[0],
    )
    sampler._tick()            # solo at t=100 → cursor advances to 100
    clock[0] = 130.0
    grouped[0] = True
    sampler._tick()            # now grouped at t=130 → window starts at 100
    assert calls, "grouped tick must scan"
    assert {c[1] for c in calls} == {100.0}
    assert {c[2] for c in calls} == {130.0}


def test_grouped_runs_the_scan():
    calls = []
    sampler = ct.CascadeTimelineSampler(
        journal_lookback_sec=30.0,
        journal_reader=_recording_reader(calls),
        grouped_check=lambda: True,
        time_func=lambda: 100.0,
    )
    sampler._tick()
    assert {c[0] for c in calls} == set(ct.JOURNAL_UNITS)
    assert sampler.snapshot()["last_scan_at"] == 100.0


def test_default_grouped_delegates_to_is_enabled(monkeypatch):
    import jasper.multiroom.config as cfg

    monkeypatch.setattr(cfg, "is_enabled", lambda *a, **k: True)
    assert ct._default_grouped() is True
    monkeypatch.setattr(cfg, "is_enabled", lambda *a, **k: False)
    assert ct._default_grouped() is False


def test_default_grouped_fails_soft_to_true(monkeypatch):
    """A grouped-check error must not blind the timeline — fail to scanning."""
    import jasper.multiroom.config as cfg

    def boom(*a, **k):
        raise RuntimeError("env read wedged")

    monkeypatch.setattr(cfg, "is_enabled", boom)
    assert ct._default_grouped() is True


# ---- off-switch (mirrors JASPER_GROUPING_SUPERVISOR / _SHAIRPORT_ / _SYSTEM_) ----


def test_start_sampler_disabled_starts_no_thread(monkeypatch):
    """JASPER_MULTIROOM_CASCADE_TIMELINE=disabled => no sampler, no subprocess.

    Hard-fails if any journalctl subprocess is spawned, proving the loop
    never runs when disabled.
    """
    monkeypatch.setattr(ct, "_sampler", None)
    monkeypatch.setenv("JASPER_MULTIROOM_CASCADE_TIMELINE", "disabled")

    def forbidden(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("disabled sampler spawned a subprocess")

    monkeypatch.setattr(ct.subprocess, "run", forbidden)

    result = ct.start_sampler()
    assert result is None
    assert ct._sampler is None
    assert ct.snapshot() == {"enabled": False, "events": []}


def test_start_sampler_disabled_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(ct, "_sampler", None)
    monkeypatch.setenv("JASPER_MULTIROOM_CASCADE_TIMELINE", "DISABLED")
    assert ct.start_sampler() is None
    assert ct._sampler is None


def test_start_sampler_unrecognized_value_stays_enabled_and_warns(
    monkeypatch, caplog,
):
    """An unrelated value keeps the timeline enabled and logs a warning —
    only the exact literal 'disabled' turns it off."""
    monkeypatch.setattr(ct, "_sampler", None)
    monkeypatch.setenv("JASPER_MULTIROOM_CASCADE_TIMELINE", "off")
    # Don't let the real daemon thread / journalctl run in the test.
    monkeypatch.setattr(ct.CascadeTimelineSampler, "start", lambda self: None)

    with caplog.at_level("WARNING"):
        result = ct.start_sampler()

    assert result is not None
    assert ct._sampler is result
    assert any(
        "JASPER_MULTIROOM_CASCADE_TIMELINE" in r.message and "off" in r.message
        for r in caplog.records
    )


def test_start_sampler_auto_stays_enabled_without_warning(monkeypatch, caplog):
    monkeypatch.setattr(ct, "_sampler", None)
    monkeypatch.delenv("JASPER_MULTIROOM_CASCADE_TIMELINE", raising=False)
    monkeypatch.setattr(ct.CascadeTimelineSampler, "start", lambda self: None)

    with caplog.at_level("WARNING"):
        result = ct.start_sampler()

    assert result is not None
    assert not any(
        "JASPER_MULTIROOM_CASCADE_TIMELINE" in r.message for r in caplog.records
    )


def test_start_sampler_idempotent(monkeypatch):
    monkeypatch.setattr(ct, "_sampler", None)
    monkeypatch.delenv("JASPER_MULTIROOM_CASCADE_TIMELINE", raising=False)
    monkeypatch.setattr(ct.CascadeTimelineSampler, "start", lambda self: None)
    first = ct.start_sampler()
    second = ct.start_sampler()
    assert first is second
