# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from jasper.control import audio_health
from jasper.control.airplay_health import AirPlayHealthSampler
from jasper.control.audio_health import (
    AudioHealthSampler,
    compose_audio_health,
)
from jasper.control.audio_incidents import (
    INCIDENT_HISTORY_MAX_BYTES,
    ISSUE_RING_SIZE,
    IncidentStore,
    IssueTracker,
    SessionRollup,
)
from jasper.music_sources import MUSIC_SOURCE_SPECS
from jasper.control.server import _make_handler


def _airplay(
    *,
    selected: str | None = None,
    ladder: str | None = None,
    warmup: bool = False,
    events: list[dict] | None = None,
) -> dict:
    return {
        "last_sample_at": 1000.0,
        "warmup_active": warmup,
        "suppressed_reason": "warmup" if warmup else None,
        "status": "ok",
        "reason": "clean",
        "mux_status": {
            "sources": {
                spec.id.value: {"playing": spec.id.value == selected}
                for spec in MUSIC_SOURCE_SPECS
            },
        },
        "current": {
            "fanin": {
                "available": True,
                "selected_input": selected,
                "input_buffer_frames": 4096,
                "output_buffer_frames": 1024,
                "inputs": {
                    spec.id.value: {
                        "label": spec.fanin_label,
                        "present": True,
                        "xrun_count": 0,
                        "frames_per_sec": (
                            48000.0 if spec.id.value == selected else 0.0
                        ),
                        "source": (
                            "direct" if spec.id.value == "usbsink" else "lane"
                        ),
                        "rms_dbfs": (
                            -20.0 if spec.id.value == selected else -100.0
                        ),
                        "health": "capturing" if spec.id.value == selected else "idle",
                    }
                    for spec in MUSIC_SOURCE_SPECS
                },
                "host_clock": (
                    {"enabled": True, "ladder": ladder}
                    if ladder is not None else None
                ),
                "watchdog": {
                    "last_progress_age_ms": 10,
                    "pings_skipped": 0,
                },
                "output": {
                    "sample_rate": 48000,
                    "snd_pcm_delay_frames": 864,
                    "snd_pcm_delay_ms": 18.0,
                },
            },
            "mpris": {"playing": selected == "airplay"},
            "camilla": {
                "capture_rate": 48000,
                "buffer_level": 32,
                "rate_adjust": 1.0,
                "chunksize": 128,
            },
        },
        "summary_5m": {
            "shairport_packet_drops": 0,
            "shairport_sync_errors": 0,
            "shairport_underruns": 0,
        },
        "summary_30m": {},
        "storm": {"active": False},
        "events": events or [],
    }


def _outputd(
    *,
    content_xruns: int = 0,
    dac_xruns: int = 0,
    progress_age_ms: int = 10,
    backend: str = "alsa",
    tts_pending_frames: int = 0,
    tts_budget_frames: int = 96000,
    delay_age_ms: int = 10,
    clipped_samples: int = 0,
) -> dict:
    return {
        "backend": backend,
        "mix": {"clipped_samples": clipped_samples},
        "content": {"xrun_count": content_xruns},
        "dac": {
            "xrun_count": dac_xruns,
            "sample_rate": 48000,
            "snd_pcm_delay_ms": 5.0,
            "snd_pcm_delay_sample_age_ms": delay_age_ms,
        },
        "tts": {
            "enabled": True,
            "pending_frames": tts_pending_frames,
            "budget_frames": tts_budget_frames,
        },
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }


def _route(
    *,
    artifact_status: str = "pass",
    artifact_issues: list[str] | None = None,
) -> dict:
    return {
        "status": "available",
        "route_id": "usb_low_latency_48k",
        "source_id": "usbsink",
        "fixed_sample_rate": 48000,
        "low_latency_claim": True,
        "p95_budget_ms": 40.0,
        "p99_budget_ms": 42.0,
        "artifact": {
            "status": artifact_status,
            "validated_at": "2026-07-14T12:00:00Z",
            "p95_ms": 37.9,
            "p99_ms": 38.3,
            "issues": artifact_issues or [],
        },
    }


def _compose(
    *,
    selected=None,
    ladder=None,
    artifact_status="pass",
    service_states=None,
    source_intents=None,
    session=None,
) -> dict:
    return compose_audio_health(
        airplay=_airplay(selected=selected, ladder=ladder),
        outputd=_outputd(),
        route=_route(artifact_status=artifact_status),
        issues=[],
        sampled_at=1000.0,
        service_states=service_states,
        source_intents=source_intents,
        session=session or {
            "summary": "No interruptions observed",
            "detail": "Since JTS observed this source become active.",
            "details": [],
            "started_at": 1000.0,
            "duration_seconds": 0.0,
            "interruptions": 0,
            "latency_events": 0,
            "sync_events": 0,
            "degraded_seconds": 0.0,
            "last_incident_at": None,
        },
    )


def test_usb_l0_and_matching_artifact_are_two_distinct_verified_facts() -> None:
    health = _compose(selected="usbsink", ladder="l0_locked")

    assert health["signal_path"]["status"] == "ok"
    assert health["latency"]["status"] == "ok"
    assert health["latency"]["verification"]["status"] == "verified"
    assert health["latency"]["runtime"] == {
        "mode": "lowest_latency",
        "raw_mode": "l0_locked",
    }
    assert health["overall"]["status"] == "ok"


def test_usb_l2_degrades_latency_without_claiming_continuity_failed() -> None:
    health = _compose(selected="usbsink", ladder="l2_fallback")

    assert health["signal_path"]["status"] == "ok"
    assert health["latency"]["status"] == "warn"
    assert health["latency"]["runtime"]["mode"] == "fallback"
    assert "Playback is protected" in health["latency"]["detail"]
    assert health["overall"]["status"] == "warn"
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["headline"] == "Playing"
    assert usb["detail"] == "Using the shared audio path."
    assert usb["timing"]["headline"] == "Stable fallback · latency increased"


def test_stale_artifact_is_technical_evidence_not_a_household_warning() -> None:
    health = _compose(
        selected="usbsink",
        ladder="l0_locked",
        artifact_status="fail",
    )

    assert health["latency"]["runtime"]["mode"] == "lowest_latency"
    assert health["latency"]["verification"]["status"] == "unverified"
    assert health["latency"]["status"] == "ok"
    assert health["latency"]["headline"] == "Low latency · stable"
    assert health["overall"]["status"] == "ok"
    assert health["technical"]["route_verification"]["status"] == "unverified"


def test_airplay_sync_stays_source_specific_not_a_latency_claim() -> None:
    health = _compose(selected="airplay")

    assert health["latency"]["applicable"] is False
    assert health["latency"]["kind"] == "none"
    airplay = next(source for source in health["sources"] if source["id"] == "airplay")
    assert airplay["timing"]["kind"] == "sync"
    assert airplay["timing"]["verification"]["status"] == "not_applicable"


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
    assert spotify["headline"] == "Spotify unavailable"
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
    assert sources["airplay"]["headline"] == "Ready"
    assert sources["spotify"]["state"] == "not_running"
    assert sources["spotify"]["headline"] == "Not running"


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
    assert spotify["headline"] == "Off"
    assert spotify["status"] == "idle"


def test_household_off_but_active_is_reported_as_drift() -> None:
    service_states = {
        "librespot.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"spotify": False},
    )
    spotify = next(source for source in health["sources"] if source["id"] == "spotify")
    assert spotify["state"] == "unavailable"
    assert "running while Off" in spotify["headline"]

    issues = audio_health._state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"spotify": False},
    )
    assert any(issue["key"].endswith("off_drift") for issue in issues)


def test_usb_off_ignores_always_on_management_gadget() -> None:
    service_states = {
        "jasper-usbgadget.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
        "jasper-usbsink.service": {
            "load_state": "loaded",
            "active_state": "inactive",
            "result": "success",
        },
        "jasper-usbsink-volume.service": {
            "load_state": "loaded",
            "active_state": "inactive",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"usbsink": False},
    )
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "off"
    assert usb["status"] == "idle"
    assert usb["headline"] == "Off"

    issues = audio_health._state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"usbsink": False},
    )
    assert not any(issue["key"].startswith("usbsink.service.") for issue in issues)


def test_usb_off_with_active_audio_service_is_reported_as_drift() -> None:
    service_states = {
        "jasper-usbgadget.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
        "jasper-usbsink.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"usbsink": False},
    )
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "unavailable"
    assert usb["status"] == "issue"
    assert usb["headline"] == "USB Audio is running while Off"
    assert "jasper-usbsink.service" in usb["detail"]
    assert "jasper-usbgadget.service" not in usb["detail"]

    issues = audio_health._state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"usbsink": False},
    )
    drift_keys = {issue["key"] for issue in issues if issue["key"].endswith("off_drift")}
    assert drift_keys == {
        "usbsink.service.jasper-usbsink.service.off_drift",
    }


def test_usb_on_still_requires_its_management_gadget() -> None:
    service_states = {
        "jasper-usbgadget.service": {
            "load_state": "loaded",
            "active_state": "failed",
            "result": "exit-code",
        },
        "jasper-usbsink.service": {
            "load_state": "loaded",
            "active_state": "active",
            "result": "success",
        },
    }
    health = _compose(
        service_states=service_states,
        source_intents={"usbsink": True},
    )
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["state"] == "unavailable"
    assert usb["status"] == "issue"
    assert usb["headline"] == "USB Audio unavailable"
    assert "jasper-usbgadget.service reports failed" in usb["detail"]

    issues = audio_health._state_issues(
        _airplay(),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        {"usbsink": True},
    )
    assert any(
        issue["key"] == "usbsink.service.jasper-usbgadget.service"
        for issue in issues
    )


def test_required_pairing_agent_failure_degrades_bluetooth() -> None:
    health = _compose(service_states={
        "bluealsa-aplay.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
        "bluealsa.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
        "bt-agent.service": {
            "active_state": "failed",
            "load_state": "loaded",
            "result": "exit-code",
        },
    })

    bluetooth = next(
        source for source in health["sources"] if source["id"] == "bluetooth"
    )
    assert bluetooth["state"] == "unavailable"
    assert bluetooth["status"] == "issue"
    assert "bt-agent.service reports failed" in bluetooth["detail"]
    assert health["overall"]["status"] == "idle"


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
        "detail": "JTS could not read the mux's canonical source state.",
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

    assert (
        stalled["signal_path"]["headline"]
        == "Final audio output has stopped progressing"
    )
    assert stalled["overall"]["status"] == "issue"
    assert inactive["signal_path"]["headline"] == "Final audio output is not active"
    assert inactive["overall"]["status"] == "issue"


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
    assert health["overall"]["headline"] == "Voice audio is delayed"


def test_usb_route_and_runtime_uncertainty_are_not_green() -> None:
    unavailable = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l0_locked"),
        outputd=_outputd(),
        route={"status": "unavailable", "low_latency_claim": False},
        issues=[],
        sampled_at=1000.0,
    )
    missing_clock = compose_audio_health(
        airplay=_airplay(selected="usbsink"),
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert unavailable["latency"]["status"] == "unknown"
    assert unavailable["overall"]["status"] == "warn"
    assert missing_clock["latency"]["status"] == "warn"
    assert "clock mode unavailable" in missing_clock["latency"]["headline"]


def test_old_target_miss_is_technical_evidence_not_a_household_warning() -> None:
    route = _route(
        artifact_status="warn",
        artifact_issues=["p99_exceeds_42ms"],
    )
    route["artifact"]["p99_ms"] = 50.0
    health = compose_audio_health(
        airplay=_airplay(selected="usbsink", ladder="l0_locked"),
        outputd=_outputd(),
        route=route,
        issues=[],
        sampled_at=1000.0,
    )

    assert health["latency"]["verification"]["status"] == "target_missed"
    assert health["latency"]["status"] == "ok"
    assert health["overall"]["status"] == "ok"
    assert health["technical"]["route_verification"]["status"] == "target_missed"


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
    assert health["signal_path"]["headline"] == "Active audio input is unavailable"


def test_usb_current_stream_is_presentation_ready_without_bitrate_inference() -> None:
    health = _compose(selected="usbsink", ladder="l0_locked")
    stream = health["current_stream"]

    assert stream["source_id"] == "usbsink"
    assert stream["media"]["summary"] == "48 kHz · Stereo PCM"
    assert "bitrate" not in json.dumps(stream).lower()
    assert stream["latency"]["summary"].startswith("At least ")
    assert "observed JTS queues" in stream["latency"]["summary"]
    assert "end-to-end" not in stream["latency"]["summary"].lower()
    assert stream["latency"]["details"][-1] == {
        "label": "Scope",
        "value": "Observed JTS queues only",
    }
    assert stream["output"]["summary"] == "48 kHz final output"
    assert stream["session"]["summary"] == "No interruptions observed"
    assert "reliability" not in stream  # session owns the roll-up once


def test_usb_latency_omits_stale_or_unaged_dac_delay() -> None:
    for outputd in (_outputd(delay_age_ms=4000), _outputd()):
        if outputd["dac"]["snd_pcm_delay_sample_age_ms"] == 10:
            del outputd["dac"]["snd_pcm_delay_sample_age_ms"]
        health = compose_audio_health(
            airplay=_airplay(selected="usbsink", ladder="l0_locked"),
            outputd=outputd,
            route=_route(),
            issues=[],
            sampled_at=1000.0,
        )
        latency_details = health["current_stream"]["latency"]["details"]
        output_details = health["current_stream"]["output"]["details"]

        assert all(row["label"] != "DAC presentation queue" for row in latency_details)
        assert all(row["label"] != "DAC queue" for row in output_details)


def test_usb_latency_omits_negative_queue_telemetry() -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    fanin = airplay["current"]["fanin"]
    fanin["inputs"]["usbsink"]["resampler"] = {"fill_frames": -480}
    fanin["output"]["snd_pcm_delay_ms"] = -4.0
    airplay["current"]["camilla"]["buffer_level"] = -32
    outputd = _outputd()
    outputd["dac"]["snd_pcm_delay_ms"] = -5.0

    health = compose_audio_health(
        airplay=airplay,
        outputd=outputd,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    latency = health["current_stream"]["latency"]

    assert latency["estimate"] is None
    assert [row["label"] for row in latency["details"]] == ["Scope"]
    assert health["current_stream"]["output"]["details"] == []


def test_current_stream_omits_processing_without_live_processing_telemetry() -> None:
    airplay = _airplay(selected="spotify")
    airplay["current"]["camilla"] = None
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert "processing" not in health["current_stream"]


def test_airplay_uses_sync_evidence_without_numeric_latency_claim() -> None:
    health = _compose(selected="airplay")
    latency = health["current_stream"]["latency"]

    assert latency["summary"] == "AirPlay sync timing clean"
    assert latency["details"] == []
    assert "estimate" not in latency
    assert "ms" not in latency["summary"]


def test_unsupported_source_omits_latency_and_missing_output_is_not_active() -> None:
    health = compose_audio_health(
        airplay=_airplay(selected="spotify"),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    stream = health["current_stream"]

    assert "latency" not in stream
    assert "output" not in stream


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
    )
    recurrence = health["recent_incidents"][0]["recurrence"]

    assert recurrence["count"] == 5
    assert recurrence["window_seconds"] == 1800.0
    assert recurrence["count_is_lower_bound"] is True
    assert recurrence["summary"] == "At least 5 occurrences observed in 30 min"


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
    )

    assert "recurrence" not in health["recent_incidents"][0]


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
    )
    assert "duration_seconds" not in health["current_incident"]


def test_issue_tracker_flushes_once_for_multiple_transitions_in_one_tick() -> None:
    class Store:
        def __init__(self) -> None:
            self.saves: list[list[dict]] = []

        def load(self) -> list[dict]:
            return []

        def save(self, incidents: list[dict]) -> None:
            self.saves.append(incidents)

    store = Store()
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
    class Store:
        def __init__(self) -> None:
            self.saves: list[list[dict]] = []

        def load(self) -> list[dict]:
            return []

        def save(self, incidents: list[dict]) -> None:
            self.saves.append(incidents)

    store = Store()
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


def test_airplay_collector_exposes_fixed_declared_inputs_and_host_clock() -> None:
    now = [1000.0]
    status = {
        "input_buffer_frames": 4096,
        "selected_input": "usbsink",
        "inputs": [
            {
                "label": "usbsink",
                "source": "direct",
                "frames_read": 100,
                "xrun_count": 2,
                "rms_dbfs": -20.0,
                "direct": {"health": "capturing"},
                "resampler": {
                    "health": "steady",
                    "locked": True,
                    "fill_frames": 512,
                    "target_fill_frames": 512,
                },
            }
        ],
        "output": {
            "frames_written": 100,
            "xrun_count": 0,
            "buffer_frames": 1024,
            "snd_pcm_delay_frames": 864,
            "snd_pcm_delay_ms": 18.0,
        },
        "watchdog": {"last_progress_age_ms": 0, "pings_skipped": 0},
        "host_clock": {"enabled": True, "ladder": "l0_locked"},
    }
    sampler = AirPlayHealthSampler(
        fanin_probe=lambda: status,
        journal_reader=lambda *_args: [],
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        maintenance_suppress_path=None,
        warmup_sec=0,
        time_fn=lambda: now[0],
    )

    sampler.sample_once()
    fanin = sampler.snapshot()["current"]["fanin"]
    assert set(fanin["inputs"]) == {
        spec.id.value for spec in MUSIC_SOURCE_SPECS
    }
    assert fanin["inputs"]["usbsink"]["health"] == "capturing"
    assert fanin["inputs"]["spotify"]["present"] is False
    assert fanin["host_clock"]["ladder"] == "l0_locked"
    assert fanin["output"]["snd_pcm_delay_frames"] == 864
    assert fanin["output"]["snd_pcm_delay_ms"] == 18.0

    status["inputs"][0]["frames_read"] += 48000
    now[0] += 1.0
    sampler.sample_once()
    fanin = sampler.snapshot()["current"]["fanin"]
    assert fanin["inputs"]["usbsink"]["frames_per_sec"] == 48000.0


class _FakeAirPlay:
    def __init__(self, snapshots: list[dict]) -> None:
        self._snapshots = snapshots
        self._index = -1

    def sample_once(self) -> None:
        self._index = min(self._index + 1, len(self._snapshots) - 1)

    def snapshot(self) -> dict:
        return self._snapshots[max(0, self._index)]


def test_sampler_uses_mux_status_as_current_source_truth() -> None:
    now = [1000.0]
    mux = [
        {"sources": {"usbsink": {"playing": False}}},
        {"sources": {"usbsink": {"playing": True}}},
    ]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([
            _airplay(selected="usbsink", ladder="l0_locked"),
            _airplay(selected="usbsink", ladder="l0_locked"),
        ]),
        outputd_probe=_outputd,
        mux_probe=lambda: mux.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    assert sampler.snapshot()["current_stream"] is None

    now[0] += 5.0
    sampler._tick()
    assert sampler.snapshot()["current_stream"]["source_id"] == "usbsink"


def test_mux_outage_is_unknown_and_preserves_the_observed_session() -> None:
    now = [1000.0]
    snapshots = [
        _airplay(selected="usbsink", ladder="l0_locked"),
        _airplay(selected="usbsink", ladder="l0_locked"),
        _airplay(selected="usbsink", ladder="l0_locked"),
    ]
    snapshots[1].pop("mux_status")
    mux = [
        {"sources": {"usbsink": {"playing": True}}},
        None,
        {"sources": {"usbsink": {"playing": False}}},
    ]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay(snapshots),
        outputd_probe=_outputd,
        mux_probe=lambda: mux.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    started_at = sampler.snapshot()["current_stream"]["session"]["started_at"]

    now[0] += 5.0
    sampler._tick()
    unknown = sampler.snapshot()
    assert unknown["overall"]["status"] == "unknown"
    assert unknown["current_stream"]["session"]["started_at"] == started_at
    assert unknown["current_incident"]["key"] == "monitor.mux_status_unavailable"

    now[0] += 5.0
    sampler._tick()
    idle = sampler.snapshot()
    assert idle["overall"]["status"] == "idle"
    assert idle["current_stream"] is None


def test_mux_outage_preserves_ongoing_source_incident_identity() -> None:
    now = [1000.0]
    snapshots = [
        _airplay(selected="usbsink", ladder="l2_fallback")
        for _ in range(3)
    ]
    snapshots[1].pop("mux_status")
    mux = [
        {"sources": {"usbsink": {"playing": True}}},
        None,
        {"sources": {"usbsink": {"playing": True}}},
    ]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay(snapshots),
        outputd_probe=_outputd,
        mux_probe=lambda: mux.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    original = next(
        issue for issue in sampler.snapshot()["issues"]
        if issue["key"] == "usbsink.latency_fallback"
    )

    now[0] += 5.0
    sampler._tick()
    during_gap = next(
        issue for issue in sampler.snapshot()["issues"]
        if issue["key"] == "usbsink.latency_fallback"
    )
    assert during_gap["status"] == "ongoing"
    assert during_gap["started_at"] == original["started_at"]
    assert during_gap["last_seen_at"] == original["last_seen_at"]
    assert during_gap["observed_seconds"] == 0.0

    now[0] += 5.0
    sampler._tick()
    resumed = next(
        issue for issue in sampler.snapshot()["issues"]
        if issue["key"] == "usbsink.latency_fallback"
    )
    assert resumed["status"] == "ongoing"
    assert resumed["started_at"] == original["started_at"]
    assert resumed["observed_seconds"] == 0.0
    assert sampler.snapshot()["current_stream"]["session"]["latency_events"] == 1
    assert sampler.snapshot()["current_stream"]["session"]["degraded_seconds"] == 0.0
    assert sum(
        issue["key"] == "usbsink.latency_fallback"
        for issue in sampler.snapshot()["issues"]
    ) == 1


def test_confirmed_output_failure_outranks_mux_observability_gap() -> None:
    snapshot = _airplay(selected="usbsink", ladder="l0_locked")
    snapshot.pop("mux_status")
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([snapshot]),
        outputd_probe=lambda: None,
        mux_probe=lambda: None,
        route_probe=_route,
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    health = sampler.snapshot()

    assert health["signal_path"]["headline"] == "Final output unavailable"
    assert health["current_incident"]["key"] == "path.outputd_unavailable"
    assert any(
        issue["key"] == "monitor.mux_status_unavailable"
        and issue["status"] == "ongoing"
        for issue in health["issues"]
    )


def test_inactive_airplay_xrun_is_not_household_history() -> None:
    event = {
        "ts": 1000.0,
        "type": "fanin_airplay_xrun",
        "severity": "issue",
        "title": "AirPlay fan-in xrun",
        "detail": "input recovered 1 xrun(s)",
        "count": 1,
    }
    idle = _airplay(selected="spotify", events=[event])
    active = _airplay(selected="airplay", events=[event])

    idle_sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([idle]),
        outputd_probe=_outputd,
        mux_probe=lambda: idle["mux_status"],
        route_probe=_route,
        time_fn=lambda: 1000.0,
    )
    idle_sampler._tick()
    assert all(
        issue["key"] != "airplay.fanin_airplay_xrun"
        for issue in idle_sampler.snapshot()["issues"]
    )

    active_sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([active]),
        outputd_probe=_outputd,
        mux_probe=lambda: active["mux_status"],
        route_probe=_route,
        time_fn=lambda: 1000.0,
    )
    active_sampler._tick()
    assert any(
        issue["key"] == "airplay.fanin_airplay_xrun"
        for issue in active_sampler.snapshot()["issues"]
    )


def test_sampler_persists_multiple_incidents_once_per_tick() -> None:
    class Store:
        def __init__(self) -> None:
            self.saves: list[list[dict]] = []

        def load(self) -> list[dict]:
            return []

        def save(self, incidents: list[dict]) -> None:
            self.saves.append(incidents)

    airplay = _airplay(
        events=[
            {
                "ts": 1000.0,
                "type": "fanin_output_xrun",
                "detail": "Fan-in recovered.",
            },
            {
                "ts": 1000.0,
                "type": "shairport_packet_drop",
                "detail": "AirPlay recovered.",
            },
        ],
    )
    store = Store()
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([airplay]),
        outputd_probe=_outputd,
        route_probe=_route,
        incident_store=store,  # type: ignore[arg-type]
        time_fn=lambda: 1000.0,
    )

    sampler._tick()

    assert len(store.saves) == 1
    assert {item["key"] for item in store.saves[0]} == {
        "path.fanin_output_xrun",
        "airplay.shairport_packet_drop",
    }


def test_delayed_raw_event_is_not_attributed_to_new_playback_session() -> None:
    now = [1000.0]
    delayed = _airplay(
        selected="usbsink",
        ladder="l0_locked",
        events=[{
            "ts": 990.0,
            "type": "fanin_output_xrun",
            "detail": "Recovered before this session.",
        }],
    )
    current = _airplay(
        selected="usbsink",
        ladder="l0_locked",
        events=[
            *delayed["events"],
            {
                "ts": 1001.0,
                "type": "fanin_output_xrun",
                "detail": "Recovered during this session.",
            },
        ],
    )
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([delayed, current]),
        outputd_probe=_outputd,
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    first = sampler.snapshot()
    assert first["current_stream"]["session"]["interruptions"] == 0
    delayed_issue = next(
        row for row in first["issues"]
        if row["key"] == "path.fanin_output_xrun"
    )
    assert "context" not in delayed_issue

    now[0] = 1005.0
    sampler._tick()
    second = sampler.snapshot()
    assert second["current_stream"]["session"]["interruptions"] == 1


def test_idle_source_xrun_delta_is_not_troubleshooting_history() -> None:
    now = [1000.0]
    first = _airplay(selected="spotify")
    second = _airplay(selected="spotify")
    second["current"]["fanin"]["inputs"]["usbsink"]["xrun_count"] = 4
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([first, second]),
        outputd_probe=_outputd,
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    health = sampler.snapshot()

    assert all(row["key"] != "usbsink.input_xrun" for row in health["issues"])
    assert all(
        row["key"] != "usbsink.input_xrun"
        for row in health["recent_incidents"]
    )


def test_sampler_tracks_l2_to_l0_as_ongoing_then_recovered() -> None:
    now = [1000.0]
    route_calls = 0

    def route_probe() -> dict:
        nonlocal route_calls
        route_calls += 1
        return _route()

    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([
            _airplay(selected="usbsink", ladder="l2_fallback"),
            _airplay(selected="usbsink", ladder="l0_locked"),
        ]),
        outputd_probe=_outputd,
        route_probe=route_probe,
        route_interval_sec=60.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    first = sampler.snapshot()
    assert first is not None
    fallback = next(
        issue
        for issue in first["issues"]
        if issue["key"] == "usbsink.latency_fallback"
    )
    assert fallback["status"] == "ongoing"

    now[0] += 5.0
    sampler._tick()
    second = sampler.snapshot()
    assert second is not None
    fallback = next(
        issue
        for issue in second["issues"]
        if issue["key"] == "usbsink.latency_fallback"
    )
    assert fallback["status"] == "recovered"
    assert second["signal_path"]["status"] == "ok"
    assert route_calls == 1  # route/artifact reads stay on the slow cadence


def test_sampler_records_outputd_xrun_delta_as_a_recovered_blip() -> None:
    now = [1000.0]
    outputd = [_outputd(), _outputd(dac_xruns=2)]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay()]),
        outputd_probe=lambda: outputd.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    health = sampler.snapshot()
    assert health is not None
    issue = next(
        item
        for item in health["issues"]
        if item["key"] == "path.outputd_dac_xrun"
    )
    assert issue["status"] == "recovered"
    assert issue["count"] == 2
    assert health["signal_path"]["status"] == "ok"


def test_sampler_records_output_clipping_delta_and_ignores_counter_reset() -> None:
    now = [1000.0]
    outputd = [
        _outputd(clipped_samples=2),
        _outputd(clipped_samples=7),
        _outputd(clipped_samples=10),
        _outputd(clipped_samples=10),
        _outputd(clipped_samples=1),
    ]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()] * len(outputd)),
        outputd_probe=lambda: outputd.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    ongoing = sampler.snapshot()["current_incident"]
    assert ongoing["key"] == "path.outputd_clipping"
    assert ongoing["status"] == "ongoing"
    assert "5 clipped sample(s)" in ongoing["detail"]

    now[0] += 5.0
    sampler._tick()
    continuing = sampler.snapshot()["current_incident"]
    assert continuing["id"] == ongoing["id"]
    assert continuing["status"] == "ongoing"
    assert "3 clipped sample(s)" in continuing["detail"]

    now[0] += 5.0
    sampler._tick()
    health = sampler.snapshot()
    clipping = next(
        item for item in health["issues"]
        if item["key"] == "path.outputd_clipping"
    )

    assert clipping["count"] == 1
    assert clipping["impact"] == "quality"
    assert clipping["status"] == "recovered"
    assert clipping["observed_seconds"] == 5.0
    assert "recover" not in clipping["detail"].lower()

    now[0] += 5.0
    sampler._tick()
    health = sampler.snapshot()
    assert sum(
        issue["key"] == "path.outputd_clipping"
        for issue in health["issues"]
    ) == 1
    assert health["technical"]["outputd"]["mix"]["clipped_samples"] == 1


def test_clipping_episode_survives_output_gap_rebaseline_and_counter_reset() -> None:
    now = [1000.0]
    outputd = [
        _outputd(clipped_samples=0),
        _outputd(clipped_samples=5),
        None,
        _outputd(clipped_samples=5),
        _outputd(clipped_samples=1),
        _outputd(clipped_samples=1),
    ]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()] * len(outputd)),
        outputd_probe=lambda: outputd.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    original = next(
        issue for issue in sampler.snapshot()["issues"]
        if issue["key"] == "path.outputd_clipping"
    )

    for _ in range(3):
        now[0] += 5.0
        sampler._tick()
        preserved = next(
            issue for issue in sampler.snapshot()["issues"]
            if issue["key"] == "path.outputd_clipping"
        )
        assert preserved["status"] == "ongoing"
        assert preserved["started_at"] == original["started_at"]
        assert preserved["last_seen_at"] == original["last_seen_at"]
        assert preserved["observed_seconds"] == 0.0

    now[0] += 5.0
    sampler._tick()
    recovered = next(
        issue for issue in sampler.snapshot()["issues"]
        if issue["key"] == "path.outputd_clipping"
    )
    assert recovered["status"] == "recovered"
    assert recovered["started_at"] == original["started_at"]


def test_clipping_episode_survives_sampler_restart_until_clean_interval(
    tmp_path,
) -> None:
    now = [1000.0]
    store = IncidentStore(str(tmp_path / "incidents.json"))
    first_outputd = [
        _outputd(clipped_samples=0),
        _outputd(clipped_samples=5),
    ]
    first = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay()]),
        outputd_probe=lambda: first_outputd.pop(0),
        route_probe=_route,
        incident_store=store,
        time_fn=lambda: now[0],
    )
    first._tick()
    now[0] += 5.0
    first._tick()
    original = next(
        issue for issue in first.snapshot()["issues"]
        if issue["key"] == "path.outputd_clipping"
    )

    second_outputd = [
        _outputd(clipped_samples=5),
        _outputd(clipped_samples=5),
    ]
    restored = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay()]),
        outputd_probe=lambda: second_outputd.pop(0),
        route_probe=_route,
        incident_store=store,
        time_fn=lambda: now[0],
    )
    restored._tick()
    preserved = next(
        issue for issue in restored.snapshot()["issues"]
        if issue["key"] == "path.outputd_clipping"
    )
    assert preserved["status"] == "ongoing"
    assert preserved["started_at"] == original["started_at"]

    now[0] += 5.0
    restored._tick()
    recovered = next(
        issue for issue in restored.snapshot()["issues"]
        if issue["key"] == "path.outputd_clipping"
    )
    assert recovered["status"] == "recovered"
    assert recovered["started_at"] == original["started_at"]


def test_cumulative_watchdog_skip_is_a_recovered_blip_not_current_failure() -> None:
    now = [1000.0]
    first = _airplay()
    second = _airplay()
    second["current"]["fanin"]["watchdog"]["pings_skipped"] = 1
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([first, second]),
        outputd_probe=_outputd,
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    health = sampler.snapshot()
    assert health is not None
    issue = next(
        item
        for item in health["issues"]
        if item["key"] == "path.fanin_watchdog_recovered"
    )
    assert issue["status"] == "recovered"
    assert health["signal_path"]["status"] == "ok"


def test_sampler_records_live_output_and_tts_conditions() -> None:
    now = [1000.0]
    outputd = [
        _outputd(progress_age_ms=9000),
        _outputd(tts_pending_frames=96000),
    ]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay()]),
        outputd_probe=lambda: outputd.pop(0),
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    first = sampler.snapshot()
    assert first is not None
    assert any(
        item["key"] == "path.outputd_watchdog_stale"
        and item["status"] == "ongoing"
        for item in first["issues"]
    )

    now[0] += 5.0
    sampler._tick()
    second = sampler.snapshot()
    assert second is not None
    assert any(
        item["key"] == "path.tts_queue_full"
        and item["status"] == "ongoing"
        for item in second["issues"]
    )
    assert any(
        item["key"] == "path.outputd_watchdog_stale"
        and item["status"] == "recovered"
        for item in second["issues"]
    )


def test_sampler_keeps_inactive_source_failure_out_of_incident_history() -> None:
    now = [1000.0]
    states = [{
        "librespot.service": {
            "active_state": "failed",
            "load_state": "loaded",
            "result": "exit-code",
        },
    }, {
        "librespot.service": {
            "active_state": "active",
            "load_state": "loaded",
            "result": "success",
        },
    }]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay()]),
        outputd_probe=_outputd,
        route_probe=_route,
        service_probe=lambda: states.pop(0),
        time_fn=lambda: now[0],
    )

    sampler._tick()
    first = sampler.snapshot()
    assert first is not None
    key = "spotify.service.librespot.service"
    spotify = next(source for source in first["sources"] if source["id"] == "spotify")
    assert spotify["status"] == "issue"
    assert all(issue["key"] != key for issue in first["issues"])
    assert first["current_incident"] is None
    assert first["recent_incidents"] == []

    now[0] += 5.0
    sampler._tick()
    second = sampler.snapshot()
    assert second is not None
    assert all(issue["key"] != key for issue in second["issues"])
    assert second["recent_incidents"] == []


def test_sampler_turns_an_old_snapshot_unknown() -> None:
    now = [1000.0]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=_outputd,
        route_probe=_route,
        sample_interval_sec=5.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 16.0
    stale = sampler.snapshot()

    assert stale is not None
    assert stale["overall"]["status"] == "unknown"
    assert stale["signal_path"]["status"] == "unknown"
    assert stale["issues"][0]["key"] == "monitor.sample_stale"
    assert stale["current_stream"] == {
        "source_id": None,
        "label": "Audio",
        "started_at": 1015.0,
        "signal": {
            "summary": "Current stream details unavailable",
            "detail": "The audio monitor has not completed a fresh sample.",
            "details": [],
        },
    }
    assert stale["current_incident"]["key"] == "monitor.sample_stale"
    assert all(
        row["key"] != "monitor.sample_stale"
        for row in stale["recent_incidents"]
    )


class _FakeHaStatus:
    def snapshot(self) -> dict:
        return {
            "configured": False,
            "connected": False,
            "url": "",
            "instance_name": None,
            "version": None,
            "error": None,
        }


def test_system_snapshot_shares_normalized_and_legacy_health() -> None:
    normalized = _compose(selected="usbsink", ladder="l0_locked")
    legacy = {"status": "ok", "reason": "clean"}
    outputd = _outputd()

    class FakeAudioHealth:
        def snapshot(self) -> dict:
            return normalized

        def airplay_snapshot(self) -> dict:
            return legacy

        def outputd_snapshot(self) -> dict:
            return outputd

    handler = _make_handler(
        "127.0.0.1",
        1234,
        "/nonexistent.sock",
        sampler=None,
        audio_health_sampler=FakeAudioHealth(),
        ha_status_cache=_FakeHaStatus(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/system/snapshot",
            timeout=2,
        ) as response:
            payload = json.loads(response.read())
        assert payload["audio_health"] == normalized
        assert payload["airplay_health"] == legacy
        assert payload["outputd"] == outputd
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_state_keeps_working_when_audio_health_snapshot_raises(monkeypatch) -> None:
    import jasper.control.server as control_server

    class RaisingAudioHealth:
        def snapshot(self) -> dict:
            raise RuntimeError("audio monitor failed")

    async def fake_state(**_kwargs) -> dict:
        return {"ts": 1000.0, "audio": {}}

    monkeypatch.setattr(control_server, "_get_state", fake_state)
    handler = _make_handler(
        "127.0.0.1",
        1234,
        "/nonexistent.sock",
        sampler=None,
        audio_health_sampler=RaisingAudioHealth(),
        ha_status_cache=_FakeHaStatus(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/state",
            timeout=2,
        ) as response:
            payload = json.loads(response.read())
        assert payload["ts"] == 1000.0
        assert payload["audio_health"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
