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

    def reader(unit, since, now):
        assert since <= now
        return lines.get(unit, [])

    sampler = ct.CascadeTimelineSampler(
        ring_size=3,
        journal_reader=reader,
        time_func=lambda: clock[0],
    )
    sampler._tick()
    snap = sampler.snapshot()
    assert snap["enabled"] is True
    assert snap["last_scan_at"] == 100.0
    assert [e["event"] for e in snap["events"]] == [
        "restart_broker.request",
        "multiroom.reconcile.start",
        "multiroom.reconcile.done",
    ]


def test_module_snapshot_default_disabled(monkeypatch):
    monkeypatch.setattr(ct, "_sampler", None)
    assert ct.snapshot() == {"enabled": False, "events": []}
