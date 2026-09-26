# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""IssueTracker, IncidentStore and SessionRollup (jasper.control.audio_incidents).

Coalescing, persistence, corruption handling, restart continuity, and the
attribution round trip. compose_audio_health's own incident-shape tests
(current_incident, recent_incidents, recurrence) stay in
test_audio_health.py; this file pins the tracker/store/rollup classes and
the composer's read of a corrupted or restart-carried record.
"""

from __future__ import annotations

import json
import logging

from jasper.control import audio_health_sampler
from jasper.control.audio_health import compose_audio_health
from jasper.control.audio_incidents import (
    INCIDENT_HISTORY_MAX_BYTES,
    ISSUE_RING_SIZE,
    IncidentStore,
    IssueTracker,
    SessionRollup,
)
from tests.audio_health_fixtures import _airplay, _airplay_link, _mux, _outputd, _route


def test_session_rollup_is_observed_presentation_not_an_exact_boundary() -> None:
    issue = {
        "key": "usbsink.input_xrun",
        "scope": "source",
        "source_id": "usbsink",
        "impact": "continuity",
        "severity": "issue",
        "title": "USB input recovered",
        "detail": "The input recovered.",
    }
    rollup = SessionRollup()
    rollup.reset("usbsink", 1000.0)
    rollup.record_point(issue, 1010.0, count=2)
    health = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l0_locked"),
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1060.0,
        session=rollup.snapshot(1060.0),
        mux_status=_mux("usbsink"),
    )
    session = health["current_stream"]["session"]

    assert session["summary"] == "2 observed interruptions"
    assert session["detail"] == "Since JTS observed this source become active."
    assert session["duration_seconds"] == 60.0
    assert session["details"] == [{
        "label": "Observed interruptions",
        "value": "2",
    }]


def test_session_rollup_is_monotonic_when_incident_history_evicts() -> None:
    tracker = IssueTracker(ring_size=2)
    rollup = SessionRollup()
    rollup.reset("usbsink", 100.0)
    for index in range(25):
        issue = {
            "key": f"path.blip_{index}",
            "scope": "path",
            "source_id": None,
            "impact": "continuity",
            "severity": "issue",
            "title": "Audio recovered",
            "detail": "The shared path recovered.",
        }
        tracker.record_point(issue, 101.0 + index)
        rollup.record_point(issue, 101.0 + index)

    assert len(tracker.snapshot()) == 2
    assert rollup.snapshot(130.0)["interruptions"] == 25

    rollup.reset("spotify", 140.0)
    assert rollup.snapshot(140.0)["interruptions"] == 0


def test_session_rollup_counts_only_observed_ongoing_degradation() -> None:
    issue = {
        "key": "usbsink.latency_fallback",
        "scope": "latency",
        "source_id": "usbsink",
        "impact": "latency",
        "severity": "warn",
        "title": "USB timing adjusted",
        "detail": "Playback continues with more buffering.",
    }
    rollup = SessionRollup(max_observation_gap_sec=15.0)
    rollup.reset("usbsink", 100.0)
    rollup.observe_state([issue], 100.0)
    rollup.observe_state([issue], 105.0)
    rollup.observe_state([], 110.0)

    session = rollup.snapshot(200.0)
    assert session["latency_events"] == 1
    assert session["degraded_seconds"] == 5.0


def test_issue_tracker_marks_an_ongoing_condition_recovered() -> None:
    tracker = IssueTracker()
    issue = {
        "key": "usbsink.latency_fallback",
        "scope": "latency",
        "source_id": "usbsink",
        "impact": "latency",
        "severity": "warn",
        "title": "USB latency fallback",
        "detail": "Playback continues.",
    }

    tracker.update([issue], 100.0)
    assert tracker.snapshot()[0]["status"] == "ongoing"
    tracker.update([], 105.0)
    recovered = tracker.snapshot()[0]
    assert recovered["status"] == "recovered"
    assert recovered["recovered_at"] == 105.0


def test_issue_tracker_point_burst_cannot_evict_an_ongoing_issue() -> None:
    tracker = IssueTracker(ring_size=2)
    ongoing = {
        "key": "path.outputd_unavailable",
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "title": "Final output unavailable",
        "detail": "Outputd is not reporting.",
    }
    tracker.update([ongoing], 100.0)
    for index in range(4):
        tracker.record_point(
            {
                **ongoing,
                "key": f"path.point_{index}",
                "title": f"Recovered point {index}",
            },
            101.0 + index,
        )

    snapshot = tracker.snapshot()
    assert snapshot[0]["key"] == "path.outputd_unavailable"
    assert snapshot[0]["status"] == "ongoing"
    assert len(snapshot) == 2


def test_issue_coalescing_is_independent_of_caller_session_context() -> None:
    tracker = IssueTracker(coalesce_sec=60.0)
    issue = {
        "key": "usbsink.input_xrun",
        "scope": "source",
        "source_id": "usbsink",
        "impact": "continuity",
        "severity": "issue",
        "title": "USB input recovered",
        "detail": "Recovered.",
    }

    tracker.record_point(issue, 100.0, context={"session_id": "usb:1"})
    tracker.record_point(issue, 110.0, context={"session_id": "usb:2"})

    records = tracker.snapshot()
    assert len(records) == 1
    assert records[0]["count"] == 2


def test_ongoing_issue_is_not_split_when_observation_context_changes() -> None:
    tracker = IssueTracker()
    issue = {
        "key": "usbsink.latency_fallback",
        "scope": "latency",
        "source_id": "usbsink",
        "impact": "latency",
        "severity": "warn",
        "title": "USB fallback",
        "detail": "Playback continues.",
    }
    tracker.update([issue], 100.0, context={"session_id": "usb:1"})
    tracker.update([issue], 110.0, context={"session_id": "usb:2"})

    records = tracker.snapshot()
    assert [(item["status"], item["started_at"]) for item in records] == [
        ("ongoing", 100.0),
    ]
    assert records[0]["observed_seconds"] == 10.0


def test_incident_store_round_trips_bounded_allowlisted_freeze_frames(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = IncidentStore(str(path), max_records=2)
    record = {
        "key": "path.outputd_dac_xrun",
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "title": "Final output recovered",
        "detail": "Outputd recovered.",
        "status": "recovered",
        "started_at": 100.0,
        "last_seen_at": 101.0,
        "recovered_at": 101.0,
        "count": 1,
        "context": {
            "started": {
                "session_id": "usb:1",
                "source_id": "usbsink",
                "clock_mode": "l0_locked",
                "output": {"snd_pcm_delay_ms": 5.0, "secret": "drop-me"},
                "secret": "drop-me",
            },
        },
        "secret": "drop-me",
    }

    store.save([record, {**record, "key": "second"}, {**record, "key": "third"}])
    loaded = store.load()

    assert [item["key"] for item in loaded] == [
        "path.outputd_dac_xrun", "second",
    ]
    assert "secret" not in loaded[0]
    assert loaded[0]["context"]["started"] == {
        "clock_mode": "l0_locked",
        "output": {"snd_pcm_delay_ms": 5.0},
    }
    assert path.stat().st_mode & 0o777 == 0o660


def test_incident_store_drops_oldest_records_to_stay_readable(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = IncidentStore(str(path))
    escape_heavy = '\\"' * 500
    records = [
        {
            "key": f"path.escape_heavy_{index}",
            "scope": escape_heavy,
            "source_id": escape_heavy,
            "impact": escape_heavy,
            "severity": escape_heavy,
            "title": escape_heavy,
            "detail": escape_heavy,
            "status": "recovered",
            "started_at": float(index),
            "last_seen_at": float(index),
            "recovered_at": float(index),
            "count": 1,
        }
        for index in range(ISSUE_RING_SIZE)
    ]
    untrimmed = {
        "schema_version": 1,
        "incidents": records,
    }
    assert len(json.dumps(untrimmed, separators=(",", ":")).encode()) > (
        INCIDENT_HISTORY_MAX_BYTES
    )

    assert store.save(records) is True

    loaded = store.load()
    assert path.stat().st_size <= INCIDENT_HISTORY_MAX_BYTES
    assert 0 < len(loaded) < ISSUE_RING_SIZE
    assert [record["key"] for record in loaded] == [
        record["key"] for record in records[:len(loaded)]
    ]


def test_attribution_survives_incident_store_round_trip_and_drops_bad_token(
    tmp_path,
) -> None:
    path = tmp_path / "incidents.json"
    store = IncidentStore(str(path))
    airplay = _airplay_link(
        ring={"attached": True}, rx_bytes_per_sec=100.0,
        rx_bytes_per_sec_baseline=1000.0,
    )
    context = audio_health_sampler._incident_context(airplay, None, "airplay")
    assert context["attribution"]["verdict"] == "network"

    record = {
        "key": "airplay.input_unavailable",
        "scope": "source",
        "source_id": "airplay",
        "impact": "continuity",
        "severity": "issue",
        "title": "AirPlay stalled",
        "detail": "AirPlay reports playing but fan-in is not receiving frames",
        "status": "recovered",
        "started_at": 1.0,
        "last_seen_at": 1.0,
        "recovered_at": 1.0,
        "count": 1,
        "context": {"started": context},
    }
    corrupted = {
        **record,
        "key": "second",
        "context": {
            "started": {**context, "attribution": {"verdict": "bogus", "details": []}},
        },
    }

    store.save([record, corrupted])
    loaded = store.load()

    assert loaded[0]["context"]["started"]["attribution"]["verdict"] == "network"
    assert "attribution" not in loaded[1].get("context", {}).get("started", {})


def test_incident_store_rejects_bad_version_symlink_and_oversize(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    path.write_text('{"schema_version":99,"incidents":[]}', encoding="utf-8")
    assert IncidentStore(str(path)).load() == []

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text('{"schema_version":1,"incidents":[]}', encoding="utf-8")
    path.symlink_to(target)
    assert IncidentStore(str(path)).load() == []

    path.unlink()
    path.write_bytes(b" " * (INCIDENT_HISTORY_MAX_BYTES + 1))
    assert IncidentStore(str(path)).load() == []


def test_corrupt_typed_fields_are_omitted_and_cannot_crash_presentation(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "incidents": [{
            "key": "path.corrupt",
            "scope": "path",
            "source_id": None,
            "impact": "continuity",
            "severity": "issue",
            "title": "Corrupt persisted incident",
            "detail": "The typed fields are malformed.",
            "status": "ongoing",
            "started_at": "100.0",
            "last_seen_at": "101.0",
            "recovered_at": "102.0",
            "count": "9",
            "observed_seconds": "3.0",
            "context": {
                "started": {
                    "clock_mode": 123,
                    "input": {"rms_dbfs": "-18.0"},
                    "output": {"snd_pcm_delay_ms": "5.0"},
                },
            },
        }],
    }), encoding="utf-8")
    path.chmod(0o660)

    tracker = IssueTracker(store=IncidentStore(str(path)))
    issue = tracker.snapshot()[0]
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=[issue],
        sampled_at=1000.0,
        mux_status=_mux(),
    )

    assert "started_at" not in issue
    assert "count" not in issue
    assert issue["observed_seconds"] == 0.0
    assert "context" not in issue
    assert "duration_seconds" not in health["current_incident"]


def test_incident_store_failures_log_stable_events_only_once(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{}", encoding="utf-8")
    loader = IncidentStore(str(corrupt))
    loader.load()
    loader.load()

    write_attempt = 0

    def fail_then_recover_then_fail(*_args, **_kwargs) -> None:
        nonlocal write_attempt
        write_attempt += 1
        if write_attempt in {1, 3}:
            raise OSError("read-only filesystem")

    writer = IncidentStore(
        str(tmp_path / "write.json"),
        writer=fail_then_recover_then_fail,
    )
    record = {
        "key": "path.output_xrun",
        "status": "recovered",
        "started_at": 100.0,
        "last_seen_at": 100.0,
        "recovered_at": 100.0,
        "count": 1,
    }
    writer.save([record])
    writer.save([record])
    writer.save([record])

    messages = [entry.getMessage() for entry in caplog.records]
    assert sum("event=audio_incident_store.load_failed" in msg for msg in messages) == 1
    assert sum("event=audio_incident_store.write_failed" in msg for msg in messages) == 2


def test_failed_transition_write_is_retained_and_retried_with_backoff(tmp_path) -> None:
    attempts = 0

    def flaky_write(*_args, **_kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporarily read-only")

    tracker = IssueTracker(
        store=IncidentStore(str(tmp_path / "incidents.json"), writer=flaky_write),
    )
    issue = {
        "key": "path.output_unavailable",
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "title": "Output unavailable",
        "detail": "The output is unavailable.",
    }
    tracker.update([issue], 100.0)
    assert attempts == 1
    with tracker.batch(399.0):
        pass
    assert attempts == 1
    with tracker.batch(400.0):
        pass
    assert attempts == 2


def test_incident_freeze_frame_survives_restart_and_records_recovery(tmp_path) -> None:
    store = IncidentStore(str(tmp_path / "incidents.json"))
    issue = {
        "key": "path.outputd_unavailable",
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "title": "Final output unavailable",
        "detail": "Outputd is not reporting.",
    }
    first = IssueTracker(store=store)
    first.update(
        [issue],
        100.0,
        context={
            "clock_mode": "l0_locked",
            "input": {"rms_dbfs": -18.0},
            "output": {"snd_pcm_delay_ms": 5.0},
        },
    )

    restored = IssueTracker(store=store)
    assert restored.snapshot()[0]["status"] == "ongoing"
    restored.update(
        [],
        105.0,
        context={
            "clock_mode": "l0_locked",
            "input": {"rms_dbfs": -30.0},
            "output": {"snd_pcm_delay_ms": 4.0},
        },
    )
    record = IncidentStore(str(tmp_path / "incidents.json")).load()[0]

    assert record["status"] == "recovered"
    assert record["context"]["started"] == {
        "clock_mode": "l0_locked",
        "input": {"rms_dbfs": -18.0},
        "output": {"snd_pcm_delay_ms": 5.0},
    }
    assert "recovered" not in record["context"]


def test_restart_does_not_split_incident_or_count_monitor_downtime(tmp_path) -> None:
    store = IncidentStore(str(tmp_path / "incidents.json"))
    issue = {
        "key": "usbsink.latency_fallback",
        "scope": "latency",
        "source_id": "usbsink",
        "impact": "latency",
        "severity": "warn",
        "title": "USB timing adjusted",
        "detail": "Playback continues with more buffering.",
    }
    first = IssueTracker(store=store)
    first.update([issue], 100.0)
    first.update([issue], 110.0)
    first.update([issue], 120.0)
    first.update([issue], 130.0)
    assert store.load()[0]["observed_seconds"] == 0.0

    restored = IssueTracker(store=store)
    restored.update([issue], 10_000.0, context={"process": "new"})
    record = restored.snapshot()[0]

    assert len(restored.snapshot()) == 1
    assert record["status"] == "ongoing"
    assert record["started_at"] == 100.0
    assert record["observed_seconds"] == 0.0
    health = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l2_fallback"),
        outputd=_outputd(),
        route=_route(),
        issues=[record],
        sampled_at=10_000.0,
        mux_status=_mux("usbsink"),
    )
    assert "duration_seconds" not in health["current_incident"]


class _RecordingStore:
    """An ``IncidentStore`` double that records ``save()`` calls verbatim,
    for tests pinning how often and with what payload the tracker writes."""

    def __init__(self) -> None:
        self.saves: list[list[dict]] = []

    def load(self) -> list[dict]:
        return []

    def save(self, incidents: list[dict]) -> None:
        self.saves.append(incidents)


def test_issue_tracker_flushes_once_for_multiple_transitions_in_one_tick() -> None:
    store = _RecordingStore()
    tracker = IssueTracker(store=store)  # type: ignore[arg-type]
    issue = {
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "title": "Recovered",
        "detail": "Recovered.",
    }
    with tracker.batch():
        tracker.record_point({**issue, "key": "one"}, 100.0)
        tracker.record_point({**issue, "key": "two"}, 100.0)

    assert len(store.saves) == 1
    assert {item["key"] for item in store.saves[0]} == {"one", "two"}


def test_incident_count_persistence_is_debounced_but_transitions_are_immediate() -> None:
    store = _RecordingStore()
    tracker = IssueTracker(store=store)  # type: ignore[arg-type]
    point = {
        "key": "path.output_xrun",
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "title": "Output recovered",
        "detail": "The output recovered.",
    }
    tracker.record_point(point, 100.0)
    assert len(store.saves) == 1  # new record

    for now in (105.0, 110.0, 120.0):
        tracker.record_point(point, now)
    assert len(store.saves) == 1
    with tracker.batch(399.0):
        pass
    assert len(store.saves) == 1
    with tracker.batch(400.0):
        pass
    assert len(store.saves) == 2
    assert store.saves[-1][0]["count"] == 4

    ongoing = {**point, "key": "path.output_unavailable"}
    tracker.update([ongoing], 410.0)
    assert len(store.saves) == 3  # start transition
    tracker.update([], 411.0)
    assert len(store.saves) == 4  # recovery transition
