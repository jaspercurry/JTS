# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AirPlay drop attribution (`jasper.control.audio_attribution`) and the
presented-incident shape (`jasper.control.audio_incident_view`): which
ongoing issue `compose_audio_health` surfaces as `current_incident`, the
30-minute recurrence rollup, and the evidence/likely-area rows an incident
carries.

`compose_audio_health`'s own signal-path and source-card verdicts stay
pinned in test_audio_health.py; this file pins only the incident-shape and
attribution-verdict layer built on top of them.
"""

from __future__ import annotations

import pytest

from jasper.control import audio_attribution, audio_health, audio_incident_view
from jasper.control.audio_health import compose_audio_health

from .audio_health_fixtures import _airplay, _airplay_link, _mux, _outputd, _route


def test_current_incident_is_separate_from_five_recent_history_rows() -> None:
    base = {
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "issue",
        "detail": "Recovered.",
        "count": 1,
    }
    issues = [{
        **base,
        "key": "path.ongoing",
        "title": "Current problem",
        "status": "ongoing",
        "started_at": 990.0,
        "last_seen_at": 1000.0,
        "recovered_at": None,
    }]
    issues.extend({
        **base,
        "key": f"path.recovered_{index}",
        "title": f"Recovered {index}",
        "status": "recovered",
        "started_at": 980.0 - index,
        "last_seen_at": 981.0 - index,
        "recovered_at": 981.0 - index,
    } for index in range(6))
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=issues,
        sampled_at=1000.0,
        mux_status=_mux(),
    )

    assert health["current_incident"]["key"] == "path.ongoing"
    assert len(health["recent_incidents"]) == 5
    assert all(row["status"] == "recovered" for row in health["recent_incidents"])
    assert all(row["key"] != "path.ongoing" for row in health["recent_incidents"])
    assert health["recent_incidents"][0]["evidence"] == []


def test_current_incident_prefers_failure_over_newer_warning() -> None:
    failure = {
        "key": "path.outputd_unavailable",
        "status": "ongoing",
        "severity": "issue",
        "title": "Final output unavailable",
        "detail": "The final output is not reporting.",
        "started_at": 900.0,
        "last_seen_at": 990.0,
        "count": 1,
    }
    warning = {
        "key": "usbsink.latency_fallback",
        "status": "ongoing",
        "severity": "warn",
        "title": "USB timing adjusted",
        "detail": "Playback continues with more buffering.",
        "started_at": 995.0,
        "last_seen_at": 1000.0,
        "count": 1,
    }

    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=[warning, failure],
        sampled_at=1000.0,
        mux_status=_mux(),
    )

    assert health["current_incident"]["key"] == "path.outputd_unavailable"


def test_current_incident_prefers_active_source_and_keeps_secondary_ongoing() -> None:
    active_warning = {
        "key": "usbsink.latency_fallback",
        "scope": "latency",
        "source_id": "usbsink",
        "status": "ongoing",
        "severity": "warn",
        "title": "USB timing adjusted",
        "detail": "Playback continues with more buffering.",
        "started_at": 990.0,
        "last_seen_at": 1000.0,
        "count": 1,
    }
    inactive_failure = {
        "key": "spotify.service.librespot",
        "scope": "source",
        "source_id": "spotify",
        "status": "ongoing",
        "severity": "issue",
        "title": "Spotify unavailable",
        "detail": "The inactive source is unavailable.",
        "started_at": 995.0,
        "last_seen_at": 1000.0,
        "count": 1,
    }

    health = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l0_locked"),
        outputd=_outputd(),
        route=_route(),
        issues=[inactive_failure, active_warning],
        sampled_at=1000.0,
        mux_status=_mux("usbsink"),
    )

    assert health["current_incident"]["key"] == "usbsink.latency_fallback"
    assert health["recent_incidents"] == []


def test_recurrence_aggregates_stable_key_over_explicit_30_min_window() -> None:
    def recovered(at: float, count: int) -> dict:
        return {
            "key": "airplay.shairport_packet_drop",
            "scope": "source",
            "source_id": "airplay",
            "impact": "sync",
            "severity": "warn",
            "title": "AirPlay correction",
            "detail": "Packet timing recovered.",
            "status": "recovered",
            "started_at": at,
            "last_seen_at": at,
            "recovered_at": at,
            "count": count,
            "first_occurrence_at": at,
            "last_occurrence_at": at,
        }

    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=[recovered(990.0, 2), recovered(900.0, 3), recovered(-1000.0, 9)],
        sampled_at=1000.0,
        mux_status=_mux(),
    )
    recurrence = health["recent_incidents"][0]["recurrence"]

    assert recurrence["count"] == 5
    assert recurrence["window_seconds"] == 1800.0
    assert recurrence["count_is_lower_bound"] is True


def test_recurrence_does_not_count_pre_window_events_from_coalesced_record() -> None:
    issue = {
        "key": "airplay.shairport_packet_drop",
        "scope": "source",
        "source_id": "airplay",
        "impact": "sync",
        "severity": "warn",
        "title": "AirPlay correction",
        "detail": "Packet timing recovered.",
        "status": "recovered",
        "started_at": -1000.0,
        "last_seen_at": 990.0,
        "recovered_at": 990.0,
        "count": 40,
        "first_occurrence_at": -1000.0,
        "last_occurrence_at": 990.0,
    }
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=[issue],
        sampled_at=1000.0,
        mux_status=_mux(),
    )

    assert "recurrence" not in health["recent_incidents"][0]


def test_old_recovered_row_does_not_claim_recent_recurrence() -> None:
    issue = {
        "key": "path.old_blip",
        "scope": "path",
        "source_id": None,
        "impact": "continuity",
        "severity": "warn",
        "title": "Old recovered blip",
        "detail": "Recovered.",
        "status": "recovered",
        "started_at": -1000.0,
        "last_seen_at": -999.0,
        "recovered_at": -999.0,
        "count": 3,
    }
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=[issue],
        sampled_at=1000.0,
        mux_status=_mux(),
    )

    assert "recurrence" not in health["recent_incidents"][0]


@pytest.mark.parametrize(
    (
        "ring", "rx_bytes_per_sec", "baseline", "rcvbuf_delta",
        "receiver_state", "majflt_per_sec", "expected_verdict",
    ),
    [
        # Ring drained + packets collapsed + a healthy receiver -> network.
        ({"attached": True}, 100.0, 1000.0, 0, "S", 0.0, "network"),
        # Rate at baseline -> internal:receiver.
        ({"attached": True}, 1000.0, 1000.0, 0, "S", 0.0, "internal:receiver"),
        # Receiver stopped outranks a collapsed rate.
        ({"attached": True}, 100.0, 1000.0, 0, "T", 0.0, "internal:receiver"),
        # A page fault this tick outranks a collapsed rate.
        ({"attached": True}, 100.0, 1000.0, 0, "S", 1.0, "internal:receiver"),
        # No baseline yet -> unknown.
        ({"attached": True}, 100.0, None, 0, "S", 0.0, "unknown"),
        # A zero baseline is treated like no baseline -> unknown, never a
        # divide-by-zero ratio.
        ({"attached": True}, 100.0, 0.0, 0, "S", 0.0, "unknown"),
        # RcvbufErrors is system-wide and cannot implicate shairport-sync by
        # itself — it stays evidence only, so a collapsed rate still reads
        # network even with errors climbing.
        ({"attached": True}, 100.0, 1000.0, 5, "S", 0.0, "network"),
        # Lane not ring-armed -> unknown, regardless of the other signals.
        (None, 100.0, 1000.0, 0, "S", 0.0, "unknown"),
    ],
)
def test_input_attribution_rules(
    ring, rx_bytes_per_sec, baseline, rcvbuf_delta,
    receiver_state, majflt_per_sec, expected_verdict,
) -> None:
    airplay = _airplay_link(
        ring=ring,
        rx_bytes_per_sec=rx_bytes_per_sec,
        rx_bytes_per_sec_baseline=baseline,
        udp_rcvbuf_errors_delta=rcvbuf_delta,
        receiver_state=receiver_state,
        majflt_per_sec=majflt_per_sec,
    )

    attribution = audio_attribution._input_attribution(airplay, "airplay")

    assert attribution["verdict"] == expected_verdict
    assert expected_verdict in audio_attribution.ATTRIBUTION_VERDICTS


def test_input_attribution_is_none_off_the_airplay_source() -> None:
    airplay = _airplay_link(
        ring={"attached": True}, rx_bytes_per_sec=100.0,
        rx_bytes_per_sec_baseline=1000.0,
    )

    assert audio_attribution._input_attribution(airplay, "usbsink") is None
    assert audio_attribution._input_attribution(airplay, None) is None


def test_incident_evidence_keeps_attribution_and_legacy_rows_uncapped() -> None:
    """A full 5-row attribution plus the 3 legacy rows must all survive —
    the evidence list must not silently drop rows past a fixed cap."""
    airplay = _airplay_link(
        ring={"attached": True}, rx_bytes_per_sec=100.0,
        rx_bytes_per_sec_baseline=1000.0, udp_rcvbuf_errors_delta=5,
    )
    airplay["current"]["fanin"]["host_clock"] = {
        "enabled": True, "ladder": "l0_locked",
    }
    context = audio_health._incident_context(airplay, _outputd(), "airplay")
    issue = {"key": "airplay.input_unavailable", "context": {"started": context}}

    evidence = audio_incident_view._incident_evidence(issue)

    assert [row["label"] for row in evidence] == [
        "Verdict", "Link rate", "Receiver state", "Packets in",
        "UDP recv buffer errors", "Clock mode", "Input level", "DAC queue",
    ]
