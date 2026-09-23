# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.control.audio_health import compose_audio_health

from .audio_health_fixtures import _airplay, _compose, _mux, _outputd, _route


def test_usb_l0_reports_the_live_lowest_latency_runtime() -> None:
    health = _compose(selected="usbsink", ladder="l0_locked")

    assert health["signal_path"]["status"] == "ok"
    assert health["latency"]["status"] == "ok"
    assert health["latency"]["runtime"] == {
        "mode": "lowest_latency",
        "raw_mode": "l0_locked",
        "phase": "stable",
    }
    assert health["overall"]["status"] == "ok"


def test_usb_l2_degrades_latency_without_claiming_continuity_failed() -> None:
    health = _compose(selected="usbsink", ladder="l2_fallback")

    assert health["signal_path"]["status"] == "ok"
    assert health["latency"]["status"] == "warn"
    assert health["latency"]["runtime"]["mode"] == "fallback"
    assert health["overall"]["status"] == "warn"
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "active"
    assert usb["timing"]["status"] == "warn"


@pytest.mark.parametrize("mode,held,floor,reason,headline,summary", [
    ("medium", 2560, 1024, "", "Recovery buffer active · 53.3 ms input buffer", "latency adjusting"),
    ("low", 1088, 576, "backoff", "Extra buffer in use · 22.7 ms input buffer", "extra buffer in use"),
])
def test_usb_runtime_preset_outranks_stale_route_label(mode, held, floor, reason, headline, summary) -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    usb = airplay["current"]["fanin"]["inputs"]["usbsink"]
    usb["resampler"] = {
        "locked": True,
        "held_target_frames": held,
        "decay": {"enabled": True, "floor_frames": floor, "frozen_reason": reason},
    }

    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route={**_route(), "low_latency_claim": False},
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux("usbsink"),
    )

    assert health["latency"]["runtime"]["preset"] == mode
    assert health["latency"]["headline"] == headline
    assert health["latency"]["status"] == "warn"
    assert health["current_stream"]["latency"]["summary"].endswith(
        f"ms · {summary}"
    )


def test_usb_terminal_fallback_outranks_raised_recovery_buffer() -> None:
    """A raw ``l2_fallback`` ladder outranks a held recovery-buffer target.

    Pinned on the runtime axes the ladder actually drives (``raw_mode``,
    ``phase``) rather than the rendered headline/detail sentences: those are
    household copy owned by :mod:`jasper.control.audio_health`, not a second
    encoding of this behavior.
    """
    airplay = _airplay(selected="usbsink", ladder="l2_fallback")
    airplay["current"]["fanin"]["host_clock"]["fallback_reason"] = (
        "probe_noncompliant"
    )
    usb = airplay["current"]["fanin"]["inputs"]["usbsink"]
    usb["resampler"] = {
        "locked": True,
        "held_target_frames": 2560,
        "decay": {"enabled": True, "floor_frames": 576},
    }

    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux("usbsink"),
    )

    assert health["latency"]["runtime"]["raw_mode"] == "l2_fallback"
    assert health["latency"]["runtime"]["phase"] == "fallback"
    assert health["latency"]["status"] == "warn"


def test_airplay_sync_stays_source_specific_not_a_latency_claim() -> None:
    health = _compose(selected="airplay")

    assert health["latency"]["applicable"] is False
    assert health["latency"]["kind"] == "none"
    airplay = next(source for source in health["sources"] if source["id"] == "airplay")
    assert airplay["timing"]["kind"] == "sync"


def test_failed_inactive_renderer_is_not_disguised_as_idle() -> None:
    health = _compose(service_states={
        "librespot.service": {
            "load_state": "loaded",
            "active_state": "failed",
            "result": "exit-code",
        },
    })

    spotify = next(
        source for source in health["sources"] if source["id"] == "spotify"
    )
    assert spotify["state"] == "unavailable"
    assert spotify["status"] == "issue"
    assert health["overall"]["status"] == "idle"


def test_cached_service_state_distinguishes_ready_from_not_running() -> None:
    health = _compose(service_states={
        "shairport-sync.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
        "librespot.service": {
            "load_state": "loaded",
            "active_state": "inactive",
            "result": "success",
        },
    })

    sources = {source["id"]: source for source in health["sources"]}
    assert sources["airplay"]["state"] == "ready"
    assert sources["spotify"]["state"] == "not_running"


def test_household_off_is_labeled_without_inactive_failure_noise() -> None:
    health = _compose(
        service_states={
            "librespot.service": {
                "load_state": "loaded",
                "active_state": "failed",
                "result": "exit-code",
            },
        },
        source_intents={"spotify": False},
    )

    spotify = next(source for source in health["sources"] if source["id"] == "spotify")
    assert spotify["state"] == "off"
    assert spotify["status"] == "idle"


def test_optional_usb_volume_observer_failure_does_not_disable_audio() -> None:
    health = _compose(service_states={
        "jasper-usbgadget.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
        "jasper-usbsink.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
        "jasper-usbsink-volume.service": {
            "active_state": "failed",
            "load_state": "loaded",
            "result": "exit-code",
        },
    })

    usb = next(
        source for source in health["sources"] if source["id"] == "usbsink"
    )
    assert usb["state"] == "ready"
    assert usb["status"] == "ok"
    assert health["overall"]["status"] == "idle"


def test_usb_route_and_runtime_uncertainty_are_not_green() -> None:
    unavailable = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l0_locked"),
        outputd=_outputd(),
        route={"status": "unavailable", "low_latency_claim": False},
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux("usbsink"),
    )
    missing_clock = compose_audio_health(
        airplay=_airplay(selected="usbsink"),
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        mux_status=_mux("usbsink"),
    )

    assert unavailable["latency"]["status"] == "unknown"
    assert unavailable["overall"]["status"] == "warn"
    assert missing_clock["latency"]["status"] == "warn"
    assert "clock mode unavailable" in missing_clock["latency"]["headline"]
