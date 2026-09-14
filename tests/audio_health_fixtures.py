# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared audio-health payload builders and a fake AirPlay sampler.

Used across test_audio_health.py, test_audio_incidents.py,
test_airplay_health.py, test_control_server_system.py and
test_audio_health_route_claim.py so each keeps one copy of the composer's
input shapes instead of re-deriving them.
"""

from __future__ import annotations

from jasper.control.audio_health import compose_audio_health
from jasper.music_sources import MUSIC_SOURCE_SPECS

# #2285 P2 (A6) retired the snd-aloop ACTIVE lane's outputd capture PAIRING
# along with the endpoint, so this shape no longer reports a capture MISMATCH —
# there is no registered capture to mismatch against. The unpaired-device arm of
# `transport_coherence_report` reports it instead. Same box, same verdict
# (parked), different sentence.
# The retired snd-aloop ACTIVE lane. A graph still naming it is a post-DSP
# route with no reader, whatever sentence the report wraps it in.
_RETIRED_ACTIVE_LANE = "outputd_active_content_playback"

# A healthy Ring A sample: 2 slots deep, nothing waiting, no stall.
_RING = {
    "occupancy": 2,
    "slots": 2,
    "stall_active": False,
    "full_waits_per_sec": 0.0,
    "drops_per_sec": 0.0,
}


def _airplay(
    *,
    selected: str | None = None,
    ladder: str | None = None,
    warmup: bool = False,
    events: list[dict] | None = None,
    ring: dict | None = None,
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
                "inputs": {
                    spec.id.value: {
                        "label": spec.fanin_label,
                        "present": True,
                        "xrun_count": 0,
                        "xruns_per_sec": 0.0,
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
                    "period_frames": 256,
                    "ring": {**_RING, **(ring or {})},
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
    content_deaf: bool = False,
) -> dict:
    return {
        "backend": backend,
        "mix": {"clipped_samples": clipped_samples},
        "content": {"xrun_count": content_xruns, "deaf": content_deaf},
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


def _route(*, transport: dict | None = None) -> dict:
    return {
        "status": "available",
        "route_id": "usb_low_latency_48k",
        "source_id": "usbsink",
        "fixed_sample_rate": 48000,
        "low_latency_claim": True,
        "transport": transport or {"coherence_errors": [], "capability_gap": None},
    }


def _compose(
    *,
    selected=None,
    ladder=None,
    service_states=None,
    source_intents=None,
    session=None,
    transport=None,
    outputd=None,
) -> dict:
    return compose_audio_health(
        airplay=_airplay(selected=selected, ladder=ladder),
        outputd=outputd if outputd is not None else _outputd(),
        route=_route(transport=transport),
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


def _airplay_link(
    *,
    ring: dict | None,
    rx_bytes_per_sec: float | None,
    rx_bytes_per_sec_baseline: float | None,
    udp_rcvbuf_errors_delta: int | None = 0,
    receiver_state: str | None = "S",
    majflt_per_sec: float | None = 0.0,
) -> dict:
    """An `_airplay()` snapshot with AirPlay selected, plus the ring +
    link blocks `_input_attribution` reads."""
    airplay = _airplay(selected="airplay")
    airplay["current"]["fanin"]["inputs"]["airplay"]["ring"] = ring
    airplay["current"]["link"] = {
        "rx_bytes_per_sec": rx_bytes_per_sec,
        "rx_bytes_per_sec_baseline": rx_bytes_per_sec_baseline,
        "udp_in_datagrams_per_sec": 128.0,
        "udp_rcvbuf_errors_delta": udp_rcvbuf_errors_delta,
        "receiver": {"state": receiver_state, "majflt_per_sec": majflt_per_sec},
    }
    return airplay


class _FakeAirPlay:
    def __init__(self, snapshots: list[dict]) -> None:
        self._snapshots = snapshots
        self._index = -1

    def sample_once(self) -> None:
        self._index = min(self._index + 1, len(self._snapshots) - 1)

    def snapshot(self) -> dict:
        return self._snapshots[max(0, self._index)]

