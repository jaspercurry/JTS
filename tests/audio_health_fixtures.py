# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared audio-health payload builders and a fake AirPlay sampler.

Used across test_audio_health.py, test_audio_incidents.py,
test_airplay_health.py, test_control_server_system.py,
test_audio_health_route_claim.py, test_audio_health_overrides.py,
test_audio_health_sampler.py and test_audio_health_events.py so each keeps
one copy of the composer's input shapes instead of re-deriving them.
"""

from __future__ import annotations

from jasper.control.audio_health import compose_audio_health
from jasper.music_sources import MUSIC_SOURCE_SPECS
from jasper.output_hardware import OutputHardwareState
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    OutputTopologySnapshot,
)

# #2285 P2 (A6) retired the snd-aloop ACTIVE lane's outputd capture PAIRING
# along with the endpoint, so this shape no longer reports a capture MISMATCH —
# there is no registered capture to mismatch against. The unpaired-device arm of
# `transport_coherence_report` reports it instead. Same box, same verdict
# (parked), different sentence.
# The retired snd-aloop ACTIVE lane. A graph still naming it is a post-DSP
# route with no reader, whatever sentence the report wraps it in.
_RETIRED_ACTIVE_LANE = "outputd_active_content_playback"

# One representative coherence error, for tests that only need the health
# model to SEE an error rather than to produce a particular one.
_ROUTE_DISCONNECTED = (
    "post-DSP route has no registered outputd capture for "
    f"Camilla playback={_RETIRED_ACTIVE_LANE!r}"
)

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


def _output_hardware(
    *,
    status: str = "ready",
    profile_id: str = "dual_apple_usb_c_dac_4ch",
    profile_label: str = "Dual Apple USB-C DAC 4-channel pair",
    physical_output_count: int = 4,
    apple_dac_count: int = 2,
    issues: tuple[dict, ...] = (),
) -> OutputHardwareState:
    return OutputHardwareState(
        profile_id=profile_id,
        profile_label=profile_label,
        status=status,
        physical_output_count=physical_output_count,
        apple_dac_count=apple_dac_count,
        issues=issues,
    )


def _declared_topology(
    *,
    device_id: str = "unknown",
    device_label: str = "Unknown output device",
    physical_output_count: int = 0,
) -> OutputTopologySnapshot:
    """A REAL, SAVED topology snapshot declaring the given hardware --
    #2812 B1's "outer conjunct" input. The default (``device_id="unknown"``)
    is a saved topology.json that names an unrecognized profile -- a
    genuine mismatch -- NOT a simulation of "nothing was ever saved". Those
    are different facts (#2812 B2): a real missing file resolves through
    ``new_topology_draft``, which auto-seeds ``hardware`` FROM the observed
    record whenever it has outputs, so it does NOT read as
    ``device_id="unknown"`` once the record is ready. Tests that need the
    genuinely-missing case must drive the real loader against an absent
    path, not synthesize a revision here. This helper's ``revision`` is
    always a real (non-``"missing"``) value for exactly that reason.
    """
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "hardware": {
            "device_id": device_id,
            "device_label": device_label,
            "physical_output_count": physical_output_count,
        },
        "speaker_groups": [],
        "routing": {},
    })
    return OutputTopologySnapshot(topology, "sha256:test-declared-topology")


_CAMILLA_CLEAN_STOP = {
    "load_state": "loaded",
    "active_state": "inactive",
    "sub_state": "dead",
    "result": "success",
}


def _compose_camilla(
    camilla_state: dict | None,
    *,
    selected: str | None = None,
    warmup: bool = False,
    outputd: dict | None = None,
) -> dict:
    """Compose health with only CamillaDSP's systemd state varying.

    Fan-in and outputd are held HEALTHY on purpose, because that is what the
    box actually reports when CamillaDSP dies — not a convenient fixture.
    Both daemons are built to keep looping when the stage between them goes
    away (fan-in's loopback coupling is timer-paced, outputd zero-fills an
    absent content lane), and both `last_progress_age_ms` counters time the
    work loop rather than audio moving.
    """
    return compose_audio_health(
        airplay=_airplay(selected=selected, warmup=warmup),
        outputd=outputd if outputd is not None else _outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        service_states=(
            None if camilla_state is None
            else {"jasper-camilla.service": camilla_state}
        ),
    )


def _live_parks() -> tuple[dict, ...]:
    """One `transport_park` verdict per park class (#3120).

    Driven from that module's own `_PARK_CASES` table rather than a second
    copy of it, so a fifth class added there is swept here without an edit —
    and each class's operator detail and remedy get their own chance to leak
    onto the household card.
    """
    from jasper.control import transport_eligibility as transport_park_reader
    from tests.test_transport_eligibility import _PARK_CASES

    return tuple(
        transport_park_reader.snapshot(case.values[0], case.values[1])
        for case in _PARK_CASES
    )

