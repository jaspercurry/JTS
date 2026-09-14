# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.control import audio_signal_path
from jasper.control.audio_health import compose_audio_health

from .audio_health_fixtures import _airplay, _outputd, _route


# --- mux activity truth -----------------------------------------------------
#
# Mux owns the canonical per-source `playing` predicate; these pin that the
# composer defers to it rather than inferring activity from frame counters or
# MPRIS, and fails closed rather than guessing when mux itself is unreachable.

def test_selected_route_without_frame_progress_is_not_claimed_as_playback() -> None:
    airplay = _airplay(selected="spotify")
    airplay["current"]["fanin"]["inputs"]["spotify"]["frames_per_sec"] = 0.0
    airplay["mux_status"]["sources"]["spotify"]["playing"] = False
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["signal_path"]["status"] == "ok"
    assert health["overall"]["status"] == "idle"
    assert health["current_stream"] is None


def test_mux_truth_gates_selected_spotify_and_bluetooth() -> None:
    for source_id in ("spotify", "bluetooth"):
        idle = _airplay(selected=source_id)
        idle["mux_status"]["sources"][source_id]["playing"] = False
        active = _airplay(selected=source_id)

        idle_health = compose_audio_health(
            airplay=idle,
            outputd=_outputd(),
            route=_route(),
            issues=[],
            sampled_at=1000.0,
        )
        active_health = compose_audio_health(
            airplay=active,
            outputd=_outputd(),
            route=_route(),
            issues=[],
            sampled_at=1000.0,
        )

        assert idle_health["current_stream"] is None
        assert active_health["current_stream"]["source_id"] == source_id


def test_missing_mux_status_fails_closed_instead_of_guessing_playback() -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    airplay.pop("mux_status")

    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["overall"] == {
        "status": "unknown",
        "headline": "Playback activity unavailable",
        "detail": audio_signal_path.ACTIVITY_UNKNOWN_DETAIL,
        "active_source": None,
        "since": 1000.0,
    }
    assert health["current_stream"]["source_id"] == "usbsink"
    assert health["current_stream"]["signal"]["summary"] == (
        "Playback state unavailable"
    )


def test_free_running_airplay_requires_mux_canonical_playing_truth() -> None:
    idle = _airplay(selected="airplay")
    # A phantom sender can leave MPRIS playing while mux's metadata gate
    # correctly decides that no audible AirPlay session exists.
    idle["current"]["mpris"]["playing"] = True
    idle["mux_status"]["sources"]["airplay"]["playing"] = False
    active = _airplay(selected="airplay")

    idle_health = compose_audio_health(
        airplay=idle,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    active_health = compose_audio_health(
        airplay=active,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert idle["current"]["fanin"]["inputs"]["airplay"]["frames_per_sec"] == 48000.0
    assert idle_health["current_stream"] is None
    assert active_health["current_stream"]["source_id"] == "airplay"


def test_free_running_usb_requires_mux_canonical_playing_truth() -> None:
    idle = _airplay(selected="usbsink", ladder="l0_locked")
    idle["current"]["fanin"]["inputs"]["usbsink"]["rms_dbfs"] = -80.0
    idle["mux_status"]["sources"]["usbsink"]["playing"] = False
    active = _airplay(selected="usbsink", ladder="l0_locked")

    idle_health = compose_audio_health(
        airplay=idle,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    active_health = compose_audio_health(
        airplay=active,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert idle["current"]["fanin"]["inputs"]["usbsink"]["frames_per_sec"] == 48000.0
    assert idle_health["current_stream"] is None
    assert active_health["current_stream"]["source_id"] == "usbsink"


# --- signal-path classifier --------------------------------------------------

def test_stale_or_inactive_outputd_is_not_reported_clean() -> None:
    stalled = compose_audio_health(
        airplay=_airplay(selected="spotify"),
        outputd=_outputd(progress_age_ms=9000),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    inactive = compose_audio_health(
        airplay=_airplay(selected="spotify"),
        outputd=_outputd(backend="none"),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert stalled["signal_path"]["code"] == "output_stalled"
    assert stalled["overall"]["status"] == "issue"
    assert inactive["signal_path"]["code"] == "output_backend_inactive"
    assert inactive["overall"]["status"] == "issue"


def test_selected_source_without_a_fanin_lane_is_a_continuity_issue() -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    airplay["current"]["fanin"]["inputs"]["usbsink"]["present"] = False
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["code"] == "input_absent"


# Ring A is a blocking handshake pinned near full in steady state (ADR-0205),
# so `full_waits` is normal and must never reach a verdict. Only the two loss
# counters and the lane's own xrun rate can degrade the path.
@pytest.mark.parametrize(
    ("ring", "input_xruns_per_sec", "code"),
    [
        # A stalled ring is the CAUSE of the deafness outputd reports, so it
        # outranks `output_deaf` (the fixture below sets both).
        ({"stall_active": True}, 0.0, "output_ring_stalled"),
        ({"drops_per_sec": 0.4}, 0.0, "path_pressured"),
        ({}, 0.2, "path_pressured"),
        ({"full_waits_per_sec": 162.0}, 0.0, "clean"),
        ({"full_waits_per_sec": 375.0, "drops_per_sec": 0.0}, 0.0, "clean"),
        ({}, 0.0, "clean"),
    ],
)
def test_ring_loss_and_stall_are_read_by_the_signal_path(
    ring: dict, input_xruns_per_sec: float, code: str,
) -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked", ring=ring)
    airplay["current"]["fanin"]["inputs"]["usbsink"]["xruns_per_sec"] = (
        input_xruns_per_sec
    )
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(content_deaf=ring.get("stall_active", False)),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["signal_path"]["code"] == code


@pytest.mark.parametrize(
    ("ring", "expected"),
    [({"occupancy": 2}, "5.3 ms"), ({"occupancy": 0}, "0.0 ms")],
)
def test_mixing_queue_is_derived_from_ring_occupancy(
    ring: dict, expected: str,
) -> None:
    health = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l0_locked", ring=ring),
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    rows = {
        row["label"]: row["value"]
        for row in health["current_stream"]["latency"]["details"]
    }

    assert rows["Mixing queue"] == expected


def test_mixing_queue_is_omitted_when_the_ring_is_unreported() -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    airplay["current"]["fanin"]["output"].pop("ring")
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    labels = [
        row["label"] for row in health["current_stream"]["latency"]["details"]
    ]

    assert "Mixing queue" not in labels


def test_idle_tts_queue_pressure_is_visible_in_overall_health() -> None:
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(tts_pending_frames=96000),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["signal_path"]["status"] == "warn"
    assert health["overall"]["status"] == "warn"
    assert health["signal_path"]["code"] == "tts_queue_full"


# Fan-in's TTS socket has a non-optional default, so its lane is armed on every
# box; the verdict must follow the DEEPEST lane, never the first armed one.
@pytest.mark.parametrize(
    ("fanin_tts", "outputd_pending", "code"),
    [
        ({"enabled": True, "pending_frames": 96000, "budget_frames": 96000},
         0, "tts_queue_full"),
        ({"enabled": True, "pending_frames": 0, "budget_frames": 96000},
         96000, "tts_queue_full"),
        ({"enabled": True, "pending_frames": 0, "budget_frames": 96000},
         0, "clean"),
        ({"enabled": False}, 96000, "tts_queue_full"),
        (None, 96000, "tts_queue_full"),
    ],
)
def test_tts_verdict_follows_the_deepest_armed_lane(
    fanin_tts: dict | None, outputd_pending: int, code: str,
) -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    airplay["current"]["fanin"]["tts"] = fanin_tts
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(tts_pending_frames=outputd_pending),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["signal_path"]["code"] == code
