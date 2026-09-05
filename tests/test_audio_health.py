# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

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
from jasper.output_hardware import OutputHardwareState
from jasper.output_hardware import write_state as write_output_hardware_state
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    OutputTopologySnapshot,
)

from .active_speaker_fixtures import (
    PASSIVE_ONLY_DAC_ID,
    PASSIVE_ONLY_DAC_LABEL,
    register_passive_only_dac,
)


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
    assert "Playback is protected" in health["latency"]["detail"]
    assert health["overall"]["status"] == "warn"
    usb = next(source for source in health["sources"] if source["id"] == "usbsink")
    assert usb["headline"] == "Playing"
    assert usb["detail"] == "Playing through the speaker."
    assert usb["timing"]["headline"] == "Stable fallback · latency increased"


def test_usb_runtime_preset_outranks_stale_route_label() -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    usb = airplay["current"]["fanin"]["inputs"]["usbsink"]
    usb["resampler"] = {
        "locked": True,
        "held_target_frames": 2560,
        "decay": {"enabled": True, "floor_frames": 1024},
    }

    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route={**_route(), "low_latency_claim": False},
        issues=[],
        sampled_at=1000.0,
    )

    assert health["latency"]["runtime"]["preset"] == "medium"
    assert health["latency"]["headline"] == (
        "Recovery buffer active · 53.3 ms input buffer"
    )
    assert health["latency"]["status"] == "warn"
    assert health["current_stream"]["latency"]["summary"].endswith(
        "ms · latency adjusting"
    )


def test_usb_terminal_fallback_outranks_raised_recovery_buffer() -> None:
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
    )

    assert health["latency"]["runtime"]["phase"] == "fallback"
    assert health["latency"]["headline"] == (
        "Stable fallback · 53.3 ms input buffer"
    )
    assert "for this USB session" in health["latency"]["detail"]
    assert health["current_stream"]["latency"]["summary"].endswith(
        "ms · stable fallback"
    )


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
    assert spotify["headline"] == "Spotify unavailable"
    assert health["overall"]["status"] == "idle"


# --- parked transport (structurally-mute box) ------------------------------
#
# The jts5 shape: CamillaDSP plays an active graph into an snd-aloop lane that
# nothing drains while outputd captures the passive lane, so the speaker emits
# digital silence with every daemon "healthy". ``transport_coherence_report``
# already detected it for doctor; these pin that /state stops calling it ready.

# #2285 P2 (A6) retired the snd-aloop ACTIVE lane's outputd capture PAIRING
# along with the endpoint, so this shape no longer reports a capture MISMATCH —
# there is no registered capture to mismatch against. The unpaired-device arm of
# `transport_coherence_report` reports it instead. Same box, same verdict
# (parked), different sentence.
_ROUTE_DISCONNECTED = (
    "post-DSP route has no registered outputd capture for "
    "Camilla playback='outputd_active_content_playback'"
)


def _plan_for(coupling: str, outputd_env: dict[str, str] | None = None):
    """A plan stub carrying BOTH coupling faces the real ``AudioRuntimePlan`` has.

    ``transport_topology`` is built by the production resolver rather than
    written as a literal, so the SHAPE name a test exercises is whatever the
    shipped code actually derives for that coupling + outputd marker pair —
    which is the whole point of #2376, where the shape name and the coupling
    token stopped being interchangeable.
    """
    from types import SimpleNamespace

    from jasper.transport_coherence import transport_topology_for_coupling
    from jasper.fanin_coupling import COUPLING_ENV_VAR

    def setting(key: str):
        if key != COUPLING_ENV_VAR:
            raise KeyError(key)
        return SimpleNamespace(key=key, value=coupling)

    return SimpleNamespace(
        transport_topology=transport_topology_for_coupling(
            coupling, outputd_env=dict(outputd_env or {})
        ),
        # The merged outputd env the real plan carries, so the sampler reads the
        # plan's copy instead of re-merging the two env files itself.
        outputd_env=dict(outputd_env or {}),
        setting=setting,
    )


def _armed_active_outputd_env(**overrides: str) -> dict[str, str]:
    """``outputd.env`` for a box armed on the ACTIVE ring — the jts3 shape.

    Keys come from the constants (they are the drift axis); the wire format is
    resolved rather than spelled, so the premise stays coherent by construction
    instead of shearing the moment the shipped ring wire changes.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        OUTPUTD_RING_PATH_ENV_VAR,
        resolve_ring_wire,
    )

    env = {
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1",
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR: "shm_ring",
        OUTPUTD_RING_PATH_ENV_VAR: DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        "JASPER_OUTPUTD_CONTENT_FORMAT": resolve_ring_wire().sample_format,
    }
    env.update(overrides)
    return env


def _armed_active_camilla_devices() -> dict[str, str]:
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
    )

    return {
        "capture_device": RING_CAPTURE_DEVICE,
        "playback_device": RING_ACTIVE_PLAYBACK_DEVICE,
    }


def _armed_active_transport_read(monkeypatch, tmp_path, **env_overrides):
    """Run ``_read_transport_state`` against the armed-ACTIVE-ring premise."""
    from jasper import audio_runtime_plan

    outputd_env = _armed_active_outputd_env(**env_overrides)
    env_file = tmp_path / "outputd.env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in outputd_env.items()),
        encoding="utf-8",
    )
    # The FIRST layer of the merge every surface now reads (`outputd.env`, then
    # `grouping-outputd.env`); the grouping layer is absent on this box.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(env_file)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices=_armed_active_camilla_devices()
        ),
    )
    # outputd's live STATUS is unreachable in-test; a ring-coupled outputd opens
    # no ALSA content PCM anyway, so there is no live value to prefer here.
    monkeypatch.setattr(audio_health, "_read_local_status", lambda *a, **k: None)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "absent.json"))
    return audio_health._read_transport_state(_plan_for("shm_ring", outputd_env))


def test_armed_active_ring_is_not_reported_as_parked(monkeypatch, tmp_path) -> None:
    """#2376: an armed roleful box must not be reported as parked.

    Observed on jts3 while audio was demonstrably playing: ``/state.audio_health``
    said "parked" with "transport plan is loopback but
    JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring" while the SAME ``/state`` reported
    ``coupling.persisted=shm_ring``, ``live_transport=shm_ring``, both rings
    armed. The health model passed ``plan.transport_topology.name`` where
    ``transport_coherence_report`` wants a coupling TOKEN; on this box that name
    is ``shm_ring_active``, which is not a coupling, so ``resolve_coupling``
    fail-SAFED it to ``loopback`` and the detector then compared a ring-armed
    outputd against a loopback plan it had invented.
    """
    from jasper.fanin_coupling import TRANSPORT_SHM_RING_ACTIVE
    from jasper.fanin_coupling import VALID_COUPLINGS

    # The premise that made the substitution lossy: the shape name this box
    # resolves to is NOT a coupling token, so it cannot stand in for one.
    assert TRANSPORT_SHM_RING_ACTIVE not in VALID_COUPLINGS
    plan = _plan_for("shm_ring", _armed_active_outputd_env())
    assert plan.transport_topology.name == TRANSPORT_SHM_RING_ACTIVE

    state = _armed_active_transport_read(monkeypatch, tmp_path)

    assert state["coherence_errors"] == []
    health = _compose(transport=state)
    assert health["signal_path"]["code"] != "transport_parked"


def test_armed_active_ring_reports_a_lagging_ring_path_as_the_arm_waypoint(
    monkeypatch, tmp_path
) -> None:
    """The armed-active fix routes to the right arm; it does not mute the detector.

    Positive control for the test above. The ring-path comparison can ONLY run
    inside the ``shm_ring_active`` arm, so seeing its verdict proves the coherence
    report reached that arm — where the pre-fix code silently took the loopback
    arm instead.

    The verdict is a NOTE, and the headline is deliberately not PARKED. A ring
    path lagging its marker is the first-arm waypoint: the path is the marker's
    projection and the next pass of its single writer converges it, so telling
    a household its speaker cannot make sound would name a permanent fault
    where there is a transient one. The window is not unobserved — the note rides this
    same surface verbatim, and outputd refusing to attach is separately loud.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        DEFAULT_OUTPUTD_RING_PATH,
        OUTPUTD_RING_PATH_ENV_VAR,
    )

    state = _armed_active_transport_read(
        monkeypatch,
        tmp_path,
        **{OUTPUTD_RING_PATH_ENV_VAR: DEFAULT_OUTPUTD_RING_PATH},
    )

    assert state["coherence_errors"] == []
    assert len(state["coherence_notes"]) == 1
    note = state["coherence_notes"][0]
    assert "FIRST-ARM waypoint" in note
    assert DEFAULT_OUTPUTD_RING_PATH in note
    assert DEFAULT_OUTPUTD_ACTIVE_RING_PATH in note

    # The note itself moves NOTHING on the card. Pinned as the exact shape
    # rather than as "not parked": an inequality passes for every wrong shape
    # too, including a regression that swapped one alarm for another.
    assert _compose(transport=state)["signal_path"]["code"] == "clean"

    # ...and THE REAL WAYPOINT STATE, which the read above does not model.
    # outputd refuses the crossed pair at startup and parks
    # (RestartPreventExitStatus=78), so on a box actually sitting here it is not
    # reporting health at all. That is what the household sees, and it is loud —
    # the card is not quietly green while the speaker is silent. The note is the
    # explanation printed beside it, not the alarm.
    waypoint = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(transport=state),
        issues=[],
        sampled_at=1000.0,
    )
    assert waypoint["signal_path"]["code"] == "output_absent"
    assert waypoint["overall"]["headline"] == waypoint["signal_path"]["headline"]


def test_audio_health_reads_the_coupling_doctor_reads(monkeypatch, tmp_path) -> None:
    """One fact, one resolution: the two surfaces cannot disagree by construction.

    ``_read_transport_state``'s docstring promises it "reads the evidence doctor
    reads, so the dashboard and ``jasper-doctor`` cannot disagree about whether
    the post-DSP route is connected". Doctor's ``_validate_outputd_coupling``
    feeds the report ``read_persisted_coupling()``; this pins that the health
    model feeds it a value that resolves identically, rather than a transport
    SHAPE name that only happens to alias a coupling for two of three shapes.
    """
    from jasper.fanin.ring_health import read_persisted_coupling
    from jasper.fanin_coupling import COUPLING_ENV_VAR, VALID_COUPLINGS

    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{COUPLING_ENV_VAR}=shm_ring\n", encoding="utf-8")
    doctor_coupling = read_persisted_coupling(fanin_env)

    seen: dict[str, object] = {}
    real_transport_state = audio_health._transport_state

    def spy(**kwargs):
        seen.update(kwargs)
        return real_transport_state(**kwargs)

    monkeypatch.setattr(audio_health, "_transport_state", spy)
    _armed_active_transport_read(monkeypatch, tmp_path)

    # Not "some coupling-ish string": the exact token doctor resolves, and one
    # the coupling resolver recognizes — a transport SHAPE name passes neither.
    assert seen["coupling"] == doctor_coupling
    assert seen["coupling"] in VALID_COUPLINGS


def _no_lane_active_two_way():
    """Roleful active 2-way saved against a DAC with no active outputd lane.

    Uses the synthetic passive-only profile: every DAC in the shipped registry
    now declares an active lane, so the capability-gap surfaces are pinned
    against the stand-in for the next lane-less board rather than against
    whichever real profile happens not to have been flipped yet. Callers must
    register it with ``register_passive_only_dac(monkeypatch)``.
    """

    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "default",
        "name": "Mono active 2-way",
        "status": "verified",
        "hardware": {
            "device_id": PASSIVE_ONLY_DAC_ID,
            "device_label": PASSIVE_ONLY_DAC_LABEL,
            "physical_output_count": 2,
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main active speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })


def test_transport_coherence_error_is_not_disguised_as_audio_is_ready() -> None:
    """A structurally-mute box must not report the idle "Audio is ready" line.

    Doctor already FAILs on this state; before this guard ``/state`` still said
    ``overall: idle, "Audio is ready"`` because the health model read only the
    route profile out of the runtime plan and never its coherence errors.
    """
    health = _compose(transport={
        "coherence_errors": [_ROUTE_DISCONNECTED],
        "capability_gap": None,
    })

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["code"] == "transport_parked"
    assert health["overall"]["status"] == "issue"
    assert health["overall"]["headline"] == health["signal_path"]["headline"]
    # The contradiction itself is operator evidence and stays off the card
    # (#2472); doctor and the transport evidence keep it.
    assert _ROUTE_DISCONNECTED not in health["signal_path"]["detail"]


def _sample_coherence_park(
    monkeypatch,
    *,
    outputd: dict | None = None,
    transport_park_state: dict | None = None,
) -> dict:
    from jasper.control import transport_park

    monkeypatch.setattr(
        transport_park,
        "snapshot",
        lambda: transport_park_state or {"status": "clear", "parks": []},
    )
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: outputd if outputd is not None else _outputd(),
        mux_probe=lambda: {"sources": {}},
        route_probe=lambda: _route(transport={
            "coherence_errors": [_ROUTE_DISCONNECTED],
            "capability_gap": None,
        }),
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    return sampler.snapshot()


def test_transport_coherence_error_is_a_current_incident(monkeypatch) -> None:
    health = _sample_coherence_park(monkeypatch)

    assert health["current_incident"]["key"] == "path.transport_parked"
    assert health["current_incident"]["title"] == health["signal_path"]["headline"]
    assert health["current_incident"]["likely_area"] == "Shared processing path"


@pytest.mark.parametrize(
    ("outputd", "park_state", "current_key", "generic_present"),
    [
        (
            _outputd(progress_age_ms=30_000),
            None,
            "path.outputd_watchdog_stale",
            True,
        ),
        (
            None,
            {
                "status": "parked",
                "parks": [{"park_class": "mono_full_range"}],
            },
            "path.transport_park.mono_full_range",
            False,
        ),
        (
            _outputd(progress_age_ms=30_000),
            {
                "status": "parked",
                "parks": [{"park_class": "mono_full_range"}],
            },
            "path.outputd_watchdog_stale",
            False,
        ),
    ],
)
def test_specific_incident_outranks_or_replaces_the_coherence_park(
    monkeypatch,
    outputd,
    park_state,
    current_key,
    generic_present,
) -> None:
    health = _sample_coherence_park(
        monkeypatch,
        outputd=outputd,
        transport_park_state=park_state,
    )

    assert health["current_incident"]["key"] == current_key
    keys = {issue["key"] for issue in health["issues"]}
    assert ("path.transport_parked" in keys) is generic_present


def test_parked_status_is_the_value_the_dashboard_alerts_on() -> None:
    """The household's parked surface is keyed to this status string.

    #2381: the System view's Audio card (``outputAlert`` in
    ``deploy/assets/system-status/js/audio-sections.js``) is the household's
    only front-page signal that the speaker cannot play, and it shows itself
    when ``overall.status`` equals one exact literal.

    Renaming that status is not silent on the Python side — measured, two
    neighbours here fail on it as well
    (``test_transport_coherence_error_is_not_disguised_as_audio_is_ready`` and
    ``test_parked_graph_keeps_the_speaker_reported_as_parked``). What none of
    them does is point at the browser constant that has to change with them, so
    the rename can be made green by fixing only the Python literals while the
    card quietly stops appearing. This pin is that missing pointer: it holds
    both halves of the coupling in one assertion and names the file to edit.
    """
    from pathlib import Path

    health = _compose(transport={
        "coherence_errors": [_ROUTE_DISCONNECTED],
        "capability_gap": None,
    })
    assert health["overall"]["status"] == "issue"

    module = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "assets" / "system-status" / "js" / "audio-sections.js"
    ).read_text()
    assert 'const OUTPUT_ALERT_STATUS = "issue";' in module, (
        "the dashboard's audio alert no longer keys off the status "
        "compose_audio_health emits for a parked speaker"
    )


def test_parked_detail_names_the_dac_that_cannot_drive_an_active_layout() -> None:
    """The capability cause is named in household language with the fix path."""
    health = _compose(transport={
        "coherence_errors": [_ROUTE_DISCONNECTED],
        "capability_gap": {
            "device_id": "innomaker_hifi_amp_pro",
            "device_label": "InnoMaker HiFi AMP Pro",
        },
    })

    detail = health["signal_path"]["detail"]
    assert health["signal_path"]["code"] == "transport_parked"
    assert "InnoMaker HiFi AMP Pro" in detail
    assert "/sound/setup/" in detail
    # Passive is not a free remedy: it sends full-range into every assigned
    # output, which on an actively-wired cabinet reaches a bare tweeter. The
    # consequence has to travel with the advice.
    assert "full-range to every output" in detail
    assert "requires a built-in passive crossover" in detail
    assert "attach an active-capable DAC" in detail


def test_live_output_failure_keeps_priority_over_the_parked_reason() -> None:
    """A concrete live failure outranks the standing structural reason.

    Parked is persistent and its remedy is "change the layout"; a stalled
    outputd is happening now and has a different remedy, so it must not be
    overwritten.
    """
    health = _compose(
        outputd=_outputd(progress_age_ms=30_000),
        transport={
            "coherence_errors": [_ROUTE_DISCONNECTED],
            "capability_gap": None,
        },
    )

    assert health["signal_path"]["code"] == "output_stalled"


def test_a_deaf_content_source_is_an_issue_a_healthy_one_is_not() -> None:
    """#3458: outputd emitting silence it did not intend read `clean` here.

    Every other input to the signal path — the backend, both watchdogs, the
    xrun counts — stays healthy through it, so the verdict outputd publishes
    is the only thing that can move this off green.
    """
    deaf = _compose(outputd=_outputd(content_deaf=True))
    assert deaf["signal_path"]["code"] == "output_deaf"
    assert deaf["signal_path"]["status"] == "issue"
    assert deaf["overall"]["status"] == "issue"
    assert _compose(outputd=_outputd())["signal_path"]["code"] == "clean"
    # A red headline with no incident row leaves `current_incident` None, so
    # the fault never enters history and never records a recovery. Every other
    # issue-status path code raises one; this is where it comes from.
    rows = audio_health._state_issues(
        _airplay(),
        _outputd(content_deaf=True),
        deaf["signal_path"],
        {"status": "ok", "runtime": {"raw_mode": "disabled"}},
        None,
        None,
        None,
        activity_unknown=False,
        coherence_park=None,
        undeclared_hardware=None,
        transport_park=None,
    )
    assert "path.outputd_content_deaf" in {row["key"] for row in rows}


def test_a_deaf_output_never_displaces_the_cause_that_produced_it() -> None:
    """Every upstream stall empties the ring, so `output_deaf` co-occurs.

    It latches at 2 s while FANIN_STALE_MS trips at 5, so placing it ahead of
    the fan-in watchdog would make `path_stalled` — and its
    `path.fanin_watchdog_stale` row — unreachable for the whole outage.
    Warmup is the same rule in time: outputd reads an empty ring before
    CamillaDSP is producing, so a deploy's restart is not a deaf speaker.
    """
    stalled_airplay = _airplay()
    stalled_airplay["current"]["fanin"]["watchdog"]["last_progress_age_ms"] = 60_000
    stalled = compose_audio_health(
        airplay=stalled_airplay,
        outputd=_outputd(content_deaf=True),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    assert stalled["signal_path"]["code"] == "path_stalled"

    warming = compose_audio_health(
        airplay=_airplay(warmup=True),
        outputd=_outputd(content_deaf=True),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    assert warming["signal_path"]["code"] != "output_deaf"


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
    path (see ``test_setup_hint_fires_when_no_topology_was_ever_saved``
    below), not synthesize a revision here. This helper's ``revision`` is
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


def test_undeclared_ready_hardware_surfaces_a_setup_hint() -> None:
    """#2812: outputd's control socket never answering — an undeclared
    composite parks the daemon before it ever starts — must not read as a
    generic, unexplained failure once the reconciler has positively
    identified ready hardware waiting on the household, AND the saved
    topology names a different (here, unrecognized) profile — #2812 B1's
    outer conjunct, satisfied here by a genuine declared-vs-detected
    mismatch (see `test_setup_hint_fires_when_no_topology_was_ever_saved`
    below for the separate "nothing was ever saved" case — #2812 B2).
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=_output_hardware(),
        output_topology_snapshot=_declared_topology(),
    )

    assert health["overall"]["status"] == "issue"
    assert health["overall"]["headline"] == audio_health.UNDECLARED_HARDWARE_HEADLINE
    detail = health["overall"]["detail"]
    assert "Dual Apple USB-C DAC 4-channel pair" in detail
    assert "/sound/setup/" in detail


def test_setup_hint_fires_when_no_topology_was_ever_saved(monkeypatch, tmp_path) -> None:
    """#2812 B2: the feature's primary case -- a box that has NEVER saved a
    topology at all -- must still get the hint.

    This is the false negative the round-2 gate proved live: adoption being
    allowed (a ready record) guarantees ``new_topology_draft``'s missing-file
    auto-seed matches the observed record exactly, so
    ``declared_hardware_mismatch`` alone can never see "genuinely
    undeclared" once hardware is ready -- the two conjuncts were mutually
    exclusive with no topology file. Only
    ``load_output_topology_snapshot``'s ``revision == "missing"`` can.

    Proven through the REAL default output-hardware AND output-topology
    probes (neither injected) against a genuinely absent
    ``JASPER_OUTPUT_TOPOLOGY_PATH`` and a REAL, populated
    ``JASPER_OUTPUT_HARDWARE_STATE_PATH``. Both must be real: an injected
    ``output_hardware_probe`` would desync from ``new_topology_draft``'s OWN
    internal ``load_output_hardware_state()`` read (which sees only the real
    file, not this sampler's injected value) and mask the exact defect this
    test exists to catch -- confirmed by mutation: replacing the revision
    check with an unconditional branch still passed against an
    injected-probe version of this test, because the auto-seed contamination
    never actually happened without a matching real file underneath it.
    """
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "never-saved.json")
    )
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH", str(tmp_path / "output_hardware.json")
    )
    write_output_hardware_state(
        OutputHardwareState(
            profile_id="dual_apple_usb_c_dac_4ch",
            profile_label="Dual Apple USB-C DAC 4-channel pair",
            status="ready",
            physical_output_count=4,
            apple_dac_count=2,
        ),
        path=tmp_path / "output_hardware.json",
    )
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: None,
        mux_probe=lambda: {"sources": {}},
        route_probe=_route,
        # output_hardware_probe AND output_topology_probe are deliberately
        # left at their real defaults -- see the docstring above.
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    health = sampler.snapshot()

    assert health is not None
    assert health["overall"]["headline"] == audio_health.UNDECLARED_HARDWARE_HEADLINE
    assert "Dual Apple USB-C DAC 4-channel pair" in health["overall"]["detail"]


def test_undeclared_hardware_hint_covers_the_non_alsa_backend_park() -> None:
    """The dual-Apple ``action=park_until_active_graph`` path keeps outputd's
    socket alive on a ``fake`` backend rather than never starting it — the
    other half of "outputd absent/parked" the hint must also cover, not just
    the ``outputd is None`` case.
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(backend="fake"),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=_output_hardware(),
        output_topology_snapshot=_declared_topology(),
    )

    assert health["overall"]["headline"] == audio_health.UNDECLARED_HARDWARE_HEADLINE


def test_setup_hint_does_not_fire_when_declared_already_matches_observed() -> None:
    """#2812 B1: the false-positive the gate proved live.

    A speaker that already declared and armed exactly the hardware now
    attached, hitting an unrelated outputd fault (a deploy's audio-graph
    bounce, a crash), must keep the pre-existing generic wording — not be
    told to "finish setup" for a setup that already happened. Adoption being
    allowed alone used to be enough to fire the hint; this is the regression
    guard for the fix.
    """
    matching = OutputHardwareState(
        profile_id="apple_usb_c_dongle",
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        apple_dac_count=1,
    )
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=matching,
        output_topology_snapshot=_declared_topology(
            device_id="apple_usb_c_dongle",
            device_label="Apple USB-C audio adapter",
            physical_output_count=2,
        ),
    )

    assert health["signal_path"]["code"] == "output_absent"
    assert health["overall"]["headline"] != audio_health.UNDECLARED_HARDWARE_HEADLINE


def test_missing_output_hardware_record_leaves_the_generic_message() -> None:
    """No record (reconciler never ran, or the artifact is unreadable) falls
    back to the pre-existing generic wording rather than guessing at
    hardware JTS cannot confirm.
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=None,
        output_topology_snapshot=_declared_topology(),
    )

    assert health["signal_path"]["code"] == "output_absent"


def test_missing_output_topology_leaves_the_generic_message() -> None:
    """No topology read yet (before the sampler's first slow-cadence tick)
    must not guess a mismatch either -- fail toward the generic message, the
    same fail-closed direction as a missing hardware record."""
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=_output_hardware(),
        output_topology_snapshot=None,
    )

    assert health["signal_path"]["code"] == "output_absent"


def test_unready_output_hardware_record_has_no_setup_hint() -> None:
    """A degraded/ambiguous detection blocked from adoption must not tell the
    household hardware is ready when it is not — this reuses the exact gate
    ``/sound/setup/``'s "Use detected hardware" button already applies.
    """
    blocked = _output_hardware(
        status="partial",
        issues=(
            {
                "severity": "blocker",
                "code": "dual_apple_usb_topology_mismatch",
                "message": (
                    "two Apple DACs are present but not on the same USB "
                    "controller/bus"
                ),
            },
        ),
    )
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=blocked,
        output_topology_snapshot=_declared_topology(),
    )

    assert health["signal_path"]["code"] == "output_absent"


def test_healthy_outputd_suppresses_the_setup_hint_even_with_a_ready_record() -> None:
    """A ready, genuinely-mismatched record alone is not the trigger — only
    ``_signal_path``'s own generic absent/non-ALSA wording is refined.
    Healthy outputd must play normally without a stale hint, even though the
    hardware/topology inputs alone would otherwise qualify.
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=_output_hardware(),
        output_topology_snapshot=_declared_topology(),
    )

    assert health["overall"]["headline"] != audio_health.UNDECLARED_HARDWARE_HEADLINE
    assert health["overall"]["status"] != "issue"


def test_setup_hint_yields_to_a_more_specific_live_output_issue() -> None:
    """A concrete, differently-worded live failure keeps priority, the same
    precedence ``_parked_signal`` and ``_stopped_dsp_signal`` hold: the hint
    only refines ``_signal_path``'s own generic absent/non-ALSA wording,
    never a distinct diagnosis such as a stalled watchdog — even though the
    hardware/topology inputs alone would otherwise qualify.
    """
    health = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(progress_age_ms=30_000),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
        output_hardware=_output_hardware(),
        output_topology_snapshot=_declared_topology(),
    )

    assert health["signal_path"]["code"] == "output_stalled"


def test_transport_state_pairs_the_route_error_with_the_dac_capability_reason(
    monkeypatch,
) -> None:
    """One derivation feeds the health model: the same detector doctor uses,
    plus the DAC-capability reason resolved from the saved topology."""
    register_passive_only_dac(monkeypatch)
    state = audio_health._transport_state(
        coupling="loopback",
        outputd_env={"JASPER_OUTPUTD_CONTENT_PCM": "outputd_content_capture"},
        camilla_devices={"playback_device": "outputd_active_content_playback"},
        topology=_no_lane_active_two_way(),
    )

    assert any(
        _ROUTE_DISCONNECTED in error for error in state["coherence_errors"]
    )
    assert state["capability_gap"] == {
        "device_id": PASSIVE_ONLY_DAC_ID,
        "device_label": PASSIVE_ONLY_DAC_LABEL,
    }


def test_parked_graph_keeps_the_speaker_reported_as_parked(
    monkeypatch,
    tmp_path,
) -> None:
    """#2135's parked graph must not read as ready through #2130's surface.

    The parked graph writes to a File sink on purpose, so it names no outputd
    endpoint and the transport detector sees no contradiction to report. Without
    this, seeding it would silence the parked headline on a box that is
    deliberately, permanently silent — trading one false "Audio is ready" for
    another.
    """
    from jasper import audio_runtime_plan
    from jasper.active_speaker.runtime_contract import build_parked_muted_graph

    register_passive_only_dac(monkeypatch)
    topology = _no_lane_active_two_way()
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "active_speaker_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")

    # Real premise, not a devices=None stub: point BOTH statefile constants at
    # real parked statefiles and let output_endpoint_evidence_from_statefiles
    # actually read them. Its verdict on a parked graph is
    # devices=<populated>, endpoint_recognized=False — a different shape from
    # the degraded devices=None read, and the one this branch must handle.
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA_STATEFILE_PATH", str(statefile)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(statefile)
    )
    evidence = audio_runtime_plan.output_endpoint_evidence_from_statefiles(
        str(statefile), str(statefile)
    )
    assert evidence.devices is not None  # populated...
    assert evidence.endpoint_recognized is False  # ...but names no outputd lane

    topology_path = tmp_path / "output_topology.json"
    from jasper.output_topology import save_output_topology

    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = audio_health._read_transport_state(_plan_for("loopback"))

    assert state["coherence_errors"]
    assert "parked graph" in state["coherence_errors"][0]
    assert "cannot drive an active speaker layout" in state["coherence_errors"][0]
    assert "reset output setup" in state["coherence_errors"][0]
    assert "choose an explicit passive layout" in state["coherence_errors"][0]
    # The DAC-capability clause still rides along after this reason.
    assert state["capability_gap"] == {
        "device_id": PASSIVE_ONLY_DAC_ID,
        "device_label": PASSIVE_ONLY_DAC_LABEL,
    }

    # The third transport-state constructor keeps the same shape as the other
    # two, so no reader needs a `.get(... ) or []` fallback — and a parked graph
    # is an ERROR, never a note: it has no ladder rung that clears it.
    assert state["coherence_notes"] == []
    assert set(state) == set(audio_health._empty_transport())

    health = _compose(transport=state)
    assert health["signal_path"]["code"] == "transport_parked"
    assert health["overall"]["headline"] != "Audio is ready"


def test_unconfigured_parked_graph_names_the_layout_action(monkeypatch, tmp_path) -> None:
    """A fresh/reset speaker is intentionally silent, never a hidden outage."""
    from jasper.active_speaker.runtime_contract import (
        UNCONFIGURED_PARKED_EXIT,
        build_parked_muted_graph,
    )
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _topology

    topology = _topology([])
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "speaker_setup_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA_STATEFILE_PATH", str(statefile)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(statefile)
    )
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = audio_health._read_transport_state(_plan_for("loopback"))

    assert state["coherence_errors"] == [
        "CamillaDSP is holding the parked graph, so every output is muted "
        f"({UNCONFIGURED_PARKED_EXIT})"
    ]
    health = _compose(transport=state)
    assert health["signal_path"]["code"] == "transport_parked"
    assert health["overall"]["headline"] != "Audio is ready"


def test_corrupt_layout_is_not_relabelled_as_unconfigured_silence(
    monkeypatch, tmp_path
) -> None:
    """A safe parked graph does not conceal corrupt persisted intent."""
    from jasper.active_speaker.runtime_contract import build_parked_muted_graph
    from tests.test_active_speaker_runtime_contract import _topology

    text, graph = build_parked_muted_graph(_topology([]))
    assert graph.allowed
    config = tmp_path / "speaker_setup_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA_STATEFILE_PATH", str(statefile)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(statefile)
    )
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = audio_health._read_transport_state(_plan_for("loopback"))

    assert state["coherence_errors"] == [
        "Saved speaker layout is unavailable or invalid; run jasper-doctor"
    ]
    assert "mono or stereo speaker layout" not in state["coherence_errors"][0]


def test_a_degraded_transport_read_cannot_poison_later_reads(monkeypatch) -> None:
    """The no-contradictions transport state must be fresh per read.

    A shallow copy of a module-level constant shares one ``coherence_errors``
    list, so a single append by any consumer would make every later degraded
    read report the box as parked for the lifetime of jasper-control.
    """
    from jasper import audio_runtime_plan

    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(devices=None),
    )
    plan = _plan_for("loopback")

    first = audio_health._read_transport_state(plan)
    first["coherence_errors"].append("poisoned")
    second = audio_health._read_transport_state(plan)

    assert second["coherence_errors"] == []


def test_transport_state_is_clean_when_the_ring_pair_is_undeclared(monkeypatch) -> None:
    """The converged box declares NOTHING, and `/state` must call it clean.

    An undeclared bridge is the ring — that is what outputd runs — so this is
    the ordinary healthy shape, not a half-configured one. Reading absence the
    other way put a playing speaker's pair on the parked card.
    """
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    register_passive_only_dac(monkeypatch)
    state = audio_health._transport_state(
        coupling="shm_ring",
        outputd_env={},
        camilla_devices={
            "capture_device": RING_CAPTURE_DEVICE,
            "playback_device": RING_PLAYBACK_DEVICE,
        },
        topology=_no_lane_active_two_way(),
    )

    assert state["coherence_errors"] == []
    assert state["coherence_notes"] == []
    # The capability gap is reported independently of the route error so a
    # surface can explain a fault it is also detecting through the transport.
    assert state["capability_gap"] is not None


def test_the_arm_waypoint_is_published_as_a_note_and_never_as_parked(
    monkeypatch,
) -> None:
    """`/state` carries the ACTIVE-ring waypoint, but not through the parked card.

    The waypoint is coherent-but-transient: a rung of an operator-only ladder,
    not a fault the household can act on. So it rides `coherence_notes` for
    whoever reads `/state`, `coherence_errors` stays EMPTY, and `_parked_signal`
    — which reads errors only — must stay silent. Reporting "parked" here would
    put a maintenance banner on a box whose owner did nothing wrong;
    `jasper-doctor` is the loud surface for it.
    """
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE

    register_passive_only_dac(monkeypatch)
    state = audio_health._transport_state(
        coupling="loopback",
        outputd_env={},
        camilla_devices={"playback_device": RING_ACTIVE_PLAYBACK_DEVICE},
        topology=_no_lane_active_two_way(),
    )

    assert state["coherence_errors"] == []
    assert len(state["coherence_notes"]) == 1
    assert "arm waypoint" in state["coherence_notes"][0]
    assert audio_health._parked_signal({"transport": state}) is None


def test_every_transport_state_constructor_carries_the_notes_key(monkeypatch) -> None:
    """`_empty_transport` and `_transport_state` agree on the shape.

    They are independent dict literals, so a reader that has to guard
    `.get("coherence_notes") or []` is one where the shape drifted; pin the
    shape instead. The third constructor, `_parked_graph_transport`, is pinned
    the same way inside
    :func:`test_parked_graph_keeps_the_speaker_reported_as_parked`, which
    already stages a real parked graph on disk.
    """
    register_passive_only_dac(monkeypatch)
    empty = audio_health._empty_transport()
    live = audio_health._transport_state(
        coupling="loopback",
        outputd_env={"JASPER_OUTPUTD_CONTENT_PCM": "outputd_content_capture"},
        camilla_devices={"playback_device": "outputd_content_playback"},
        topology=_no_lane_active_two_way(),
    )

    assert set(empty) == set(live)
    assert "coherence_notes" in empty
    # Built per call, never shared: an append through one reader must not be
    # visible to the next, exactly as `_empty_transport`'s docstring requires of
    # `coherence_errors`.
    empty["coherence_notes"].append("leak")
    assert audio_health._empty_transport()["coherence_notes"] == []


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


# --------------------------------------------------------------------------- #
# A stopped CamillaDSP is visible (#2163).
#
# `jasper-camilla.service` has no Condition gate and, per its own unit file,
# "must NEVER stay stopped" — but a CLEAN stop passed every surface: doctor's
# `check_service_runtime_state` flags only `failed`, `system_metrics` hides
# cleanly-inactive units, and camilla is in no source's `health_units`.
# --------------------------------------------------------------------------- #


def _camilla_issues(camilla_state: dict | None, *, warmup: bool = False) -> list[dict]:
    service_states = (
        None if camilla_state is None else {"jasper-camilla.service": camilla_state}
    )
    issues = audio_health._state_issues(
        _airplay(warmup=warmup),
        _outputd(),
        {"status": "idle", "headline": "No source is playing", "detail": ""},
        {"status": "idle"},
        None,
        service_states,
        None,
    )
    return [issue for issue in issues if issue["key"] == "path.camilla_stopped"]


def test_cleanly_stopped_camilla_is_surfaced_as_a_path_issue() -> None:
    """The exact state that was invisible: inactive, result=success."""
    issues = _camilla_issues({
        "load_state": "loaded",
        "active_state": "inactive",
        "sub_state": "dead",
        "result": "success",
    })

    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "issue"
    assert issue["scope"] == "path"
    assert issue["key"] == "path.camilla_stopped"
    # `AudioHealthSampler._tick` drops availability-impact issues whose
    # source_id is not the active source. A path-scoped continuity issue is
    # kept, which is what makes this reach `/state` and the /system audio card.
    assert issue["impact"] == "continuity"
    assert issue["source_id"] is None


def test_failed_camilla_is_surfaced_by_the_same_issue() -> None:
    issues = _camilla_issues({
        "load_state": "loaded",
        "active_state": "failed",
        "sub_state": "failed",
        "result": "exit-code",
    })

    assert len(issues) == 1
    assert issues[0]["key"] == "path.camilla_stopped"


@pytest.mark.parametrize("active_state", ["active", "activating", "reloading"])
def test_running_camilla_raises_no_issue(active_state: str) -> None:
    assert _camilla_issues({
        "load_state": "loaded",
        "active_state": active_state,
        "result": "success",
    }) == []


@pytest.mark.parametrize("states", [None, {}])
def test_unknown_camilla_state_raises_no_issue(states: dict | None) -> None:
    """Unknown is not stopped: no systemctl, or before the first probe."""
    assert _camilla_issues(states) == []


def test_stopped_camilla_is_suppressed_during_boot_warmup() -> None:
    """A deploy restarts jasper-control, so the boot grace covers its bounce.

    Same gate the sibling `path.fanin_unavailable` / `path.outputd_unavailable`
    issues use, so the audio card does not flicker on every deploy.
    """
    assert _camilla_issues(
        {"load_state": "loaded", "active_state": "inactive", "result": "success"},
        warmup=True,
    ) == []


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


def test_stopped_camilla_is_not_reported_as_a_clean_signal_path() -> None:
    """The whole payload has to agree (#2163).

    Before this, `_signal_path` read only fan-in and outputd — neither of
    which notices CamillaDSP leaving — so one response reported a clean path
    and an idle "ready" overall while carrying its own stopped-processing
    incident. An affirmative wrong answer is worse than the silence the issue
    was filed against.
    """
    health = _compose_camilla(_CAMILLA_CLEAN_STOP)

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["code"] == "camilla_stopped"

    assert health["overall"]["status"] == "issue"
    assert health["overall"]["headline"] == health["signal_path"]["headline"]
    # The state this response must never claim while the DSP is dead.
    assert health["overall"]["headline"] != "Audio is ready"


def test_stopped_camilla_outranks_the_deafness_it_causes() -> None:
    """The stopped DSP is the ring writer, so `output_deaf` is its symptom.

    Naming the cause is strictly more useful than naming the silence, and
    `output_deaf` is the one issue-status shape a stopped DSP displaces.
    """
    health = _compose_camilla(
        _CAMILLA_CLEAN_STOP, outputd=_outputd(content_deaf=True)
    )

    assert health["signal_path"]["code"] == "camilla_stopped"


def test_a_park_outranks_the_deafness_it_causes() -> None:
    """A parked lane IS a lane with no producer, so it reads deaf by design.

    `grouped_dac_content_lane` parks a box whose armed round-trip lane "has no
    producer" — outputd zero-fills it forever. Letting `output_deaf` stand
    would replace a structural verdict carrying its own rebuild issue with
    "Try Restart audio", which cannot clear it.
    """
    for park in _live_parks():
        health = compose_audio_health(
            airplay=_airplay(),
            outputd=_outputd(content_deaf=True),
            route=_route(),
            issues=[],
            sampled_at=1000.0,
            transport_park=park,
        )
        assert health["signal_path"]["code"] == "transport_unservable"

    coherence = compose_audio_health(
        airplay=_airplay(),
        outputd=_outputd(content_deaf=True),
        route=_route(
            transport={"coherence_errors": [_ROUTE_DISCONNECTED], "capability_gap": None}
        ),
        issues=[],
        sampled_at=1000.0,
    )
    assert coherence["signal_path"]["code"] == "transport_parked"


def test_stopped_camilla_outranks_a_source_that_looks_like_it_is_playing() -> None:
    """mux still calls AirPlay "playing"; nothing reaches the drivers."""
    health = _compose_camilla(_CAMILLA_CLEAN_STOP, selected="airplay")

    assert health["overall"]["status"] == "issue"
    assert health["overall"]["headline"] != "Audio is playing"
    # The active source card carries the same reason, not a green "ok".
    airplay_card = next(c for c in health["sources"] if c["id"] == "airplay")
    assert airplay_card["status"] == "issue"
    assert airplay_card["headline"] == audio_health.STOPPED_DSP_HEADLINE


def test_running_camilla_leaves_the_signal_path_clean() -> None:
    health = _compose_camilla({
        "load_state": "loaded",
        "active_state": "active",
        "sub_state": "running",
        "result": "success",
    })

    assert health["signal_path"]["status"] == "ok"
    assert health["overall"]["headline"] == "Audio is ready"


@pytest.mark.parametrize("states", [None, {}])
def test_unknown_camilla_state_leaves_the_signal_path_clean(states: dict | None) -> None:
    """Unknown is not stopped — no systemctl, or before the first probe."""
    health = _compose_camilla(states)

    assert health["signal_path"]["status"] == "ok"


def test_stopped_camilla_does_not_claim_the_signal_path_during_warmup() -> None:
    """Same boot gate as the issue, so a deploy's bounce cannot flicker it."""
    health = _compose_camilla(_CAMILLA_CLEAN_STOP, warmup=True)

    assert health["signal_path"]["status"] != "issue"
    assert health["signal_path"]["headline"] != audio_health.STOPPED_DSP_HEADLINE


def test_a_live_path_failure_still_outranks_a_stopped_camilla() -> None:
    """Deference, not precedence: the override only claims a clean path.

    Same `!= "issue"` guard `_parked_signal` uses — a concrete failure that is
    happening in fan-in or outputd keeps its own, more specific remedy.
    """
    health = _compose_camilla(
        _CAMILLA_CLEAN_STOP,
        outputd=_outputd(progress_age_ms=audio_health.OUTPUTD_STALE_MS + 1),
    )

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["code"] == "output_stalled"


def test_never_installed_camilla_keeps_its_own_remedy() -> None:
    """One box must not get two different stories about the same unit.

    A never-installed unit is a distinct state from a stopped one and doctor
    answers it with its own reinstall remedy, so the card must not offer the
    restart that clears a merely-stopped unit — no restart installs a unit
    that is not there.
    """
    missing = _compose_camilla({
        "load_state": "not-found",
        "active_state": "inactive",
        "sub_state": "dead",
    })["signal_path"]
    stopped = _compose_camilla(_CAMILLA_CLEAN_STOP)["signal_path"]

    assert missing["code"] == "camilla_not_installed"
    assert stopped["code"] == "camilla_stopped"
    assert missing["detail"] != stopped["detail"]


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
    assert any(
        issue["key"] == "bluetooth.service.bt-agent.service"
        for issue in audio_health._state_issues(
            _airplay(),
            _outputd(),
            {"status": "idle", "headline": "No source is playing", "detail": ""},
            {"status": "idle"},
            None,
            {
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
            },
        )
    )
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
        "detail": audio_health.ACTIVITY_UNKNOWN_DETAIL,
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

    assert stalled["signal_path"]["code"] == "output_stalled"
    assert stalled["overall"]["status"] == "issue"
    assert inactive["signal_path"]["code"] == "output_backend_inactive"
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
    assert health["signal_path"]["code"] == "tts_queue_full"


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


def test_usb_current_stream_is_presentation_ready_without_bitrate_inference() -> None:
    health = _compose(selected="usbsink", ladder="l0_locked")
    stream = health["current_stream"]

    assert stream["source_id"] == "usbsink"
    assert stream["media"]["summary"] == "48 kHz · Stereo PCM"
    assert "bitrate" not in json.dumps(stream).lower()
    assert stream["latency"]["summary"].endswith("ms · low latency stable")
    assert stream["latency"]["detail"] == ""
    assert [row["label"] for row in stream["latency"]["details"]] == [
        "Mixing queue",
        "DSP queue",
        "DAC presentation queue",
    ]
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
    assert latency["details"] == []
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

    attribution = audio_health._input_attribution(airplay, "airplay")

    assert attribution["verdict"] == expected_verdict


def test_input_attribution_is_none_off_the_airplay_source() -> None:
    airplay = _airplay_link(
        ring={"attached": True}, rx_bytes_per_sec=100.0,
        rx_bytes_per_sec_baseline=1000.0,
    )

    assert audio_health._input_attribution(airplay, "usbsink") is None
    assert audio_health._input_attribution(airplay, None) is None


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

    evidence = audio_health._incident_evidence(issue)

    assert [row["label"] for row in evidence] == [
        "Verdict", "Link rate", "Receiver state", "Packets in",
        "UDP recv buffer errors", "Clock mode", "Input level", "DAC queue",
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
    context = audio_health._incident_context(airplay, None, "airplay")
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
                "direct": {
                    "health": "capturing",
                    "stream_starts": 2,
                    "stream_stops": 1,
                    "buffer_frames": 768,
                    "drain_avail": {"max": 516},
                },
                "resampler": {
                    "health": "steady",
                    "locked": True,
                    "clamp_count": 7,
                    "anti_windup_count": 2,
                    "lock_count": 18,
                    "unlock_count": 17,
                    "fill_frames": 512,
                    "target_fill_frames": 512,
                    "held_target_frames": 1024,
                    "decay": {"enabled": True, "floor_frames": 1024, "demand_ppm": 125.33},
                },
            }
        ],
        "output": {
            "frames_written": 100,
            "xrun_count": 0,
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
    assert fanin["inputs"]["usbsink"]["direct"]["drain_avail"]["max"] == 516
    assert fanin["inputs"]["usbsink"]["resampler"]["unlock_count"] == 17
    # The #3464 rail counters ride the curated view alongside the ratio.
    assert fanin["inputs"]["usbsink"]["resampler"]["clamp_count"] == 7
    assert fanin["inputs"]["usbsink"]["resampler"]["anti_windup_count"] == 2
    assert fanin["inputs"]["usbsink"]["resampler"]["decay"]["enabled"] is True
    # The decontamination gauge rides the wholesale decay deepcopy (#3466).
    assert fanin["inputs"]["usbsink"]["resampler"]["decay"]["demand_ppm"] == 125.33
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


@pytest.mark.parametrize("stream_stops,expected", [(0, 1), (1, 0)])
def test_usb_underfill_is_recorded_but_normal_stream_stop_is_suppressed(
    stream_stops: int,
    expected: int,
) -> None:
    def snapshot(unlocks: int, stops: int) -> dict:
        state = _airplay(selected="usbsink", ladder="l0_locked")
        usb = state["current"]["fanin"]["inputs"]["usbsink"]
        usb["resampler"] = {
            "unlock_count": unlocks,
            "held_target_frames": 576,
            "decay": {"enabled": True, "floor_frames": 576},
        }
        usb["direct"] = {"stream_stops": stops}
        return state

    now = [1000.0]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([
            snapshot(1, 0),
            snapshot(2, stream_stops),
        ]),
        outputd_probe=_outputd,
        mux_probe=lambda: None,
        route_probe=_route,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    issues = [
        issue for issue in sampler.snapshot()["issues"]
        if issue["key"] == "usbsink.latency_buffer_underfill"
    ]
    assert len(issues) == expected
    if expected:
        assert issues[0]["title"] == "USB input buffer ran dry"
        assert sampler.snapshot()["current_stream"]["session"]["interruptions"] == 1


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

    assert health["signal_path"]["code"] == "output_absent"
    assert health["current_incident"]["key"] == "path.outputd_unavailable"
    assert any(
        issue["key"] == "monitor.mux_status_unavailable"
        and issue["status"] == "ongoing"
        for issue in health["issues"]
    )


def test_sampler_wires_the_output_hardware_probe_into_the_setup_hint() -> None:
    """The sampler must actually call its output-hardware AND output-topology
    probes each tick / slow-cadence pass and thread both into
    ``compose_audio_health``, not just accept the constructor arguments.
    Both probes are injected explicitly (never left to the real default
    reader) so this cannot pass by accident depending on ambient host state.
    """
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: None,
        mux_probe=lambda: {"sources": {}},
        route_probe=_route,
        output_hardware_probe=lambda: OutputHardwareState(
            profile_id="dual_apple_usb_c_dac_4ch",
            profile_label="Dual Apple USB-C DAC 4-channel pair",
            status="ready",
            physical_output_count=4,
            apple_dac_count=2,
        ),
        output_topology_probe=_declared_topology,
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    health = sampler.snapshot()

    assert health is not None
    assert health["overall"]["headline"] == audio_health.UNDECLARED_HARDWARE_HEADLINE


def test_output_hardware_probe_failure_is_fail_soft() -> None:
    """An unreadable/raising output-hardware probe must not break a tick —
    it degrades to "no record" like every other probe this sampler reads.
    """
    def _raise() -> None:
        raise OSError("state file vanished mid-read")

    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: None,
        mux_probe=lambda: {"sources": {}},
        route_probe=_route,
        output_hardware_probe=_raise,
        output_topology_probe=_declared_topology,
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    health = sampler.snapshot()

    assert health is not None
    assert health["signal_path"]["code"] == "output_absent"


def test_output_topology_probe_failure_is_fail_soft() -> None:
    """A raising output-topology probe must not break a tick either, and
    must not blank a previously-good cached topology (a transient read
    failure is not "the box just uninstalled its speaker layout")."""
    def _raise() -> None:
        raise OSError("topology file vanished mid-read")

    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay()]),
        outputd_probe=lambda: None,
        mux_probe=lambda: {"sources": {}},
        route_probe=_route,
        output_hardware_probe=lambda: OutputHardwareState(
            profile_id="dual_apple_usb_c_dac_4ch",
            profile_label="Dual Apple USB-C DAC 4-channel pair",
            status="ready",
            physical_output_count=4,
            apple_dac_count=2,
        ),
        output_topology_probe=_raise,
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    health = sampler.snapshot()

    assert health is not None
    # No cached topology yet (first tick, probe raised) -- fails toward the
    # pre-existing generic message rather than guessing a mismatch.
    assert health["signal_path"]["code"] == "output_absent"


def test_output_topology_probe_runs_on_the_slow_route_cadence_not_every_tick() -> None:
    """Declared topology changes only when a household saves a new layout, so
    it is read on the same slow cadence as the route/transport check, not
    every fast tick -- the resource-cost promise this module's docstring and
    #2812's design both rely on.
    """
    calls = {"count": 0}

    def _counting_probe() -> OutputTopologySnapshot:
        calls["count"] += 1
        return _declared_topology()

    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay(), _airplay()]),
        outputd_probe=lambda: None,
        mux_probe=lambda: {"sources": {}},
        route_probe=_route,
        route_interval_sec=60.0,
        output_hardware_probe=lambda: None,
        output_topology_probe=_counting_probe,
        time_fn=lambda: 1000.0,
    )

    sampler._tick()
    sampler._tick()
    sampler._tick()

    assert calls["count"] == 1


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

        def airplay_playing(self) -> bool | None:
            return None

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


# --------------------------------------------------------------------------- #
# Household register (#2472).
#
# Every sentence this module writes reaches the /system/ Audio card verbatim —
# that is the one-writer discipline #2466 chose — so all of it is household
# copy: what is wrong with the household's sound, and what they can do about
# it. The operator half of each state keeps its own home: `jasper-doctor` for
# unit names, systemd states and `journalctl` lines, and
# `/state.audio_health.technical` for the raw counters this card is built from.
# --------------------------------------------------------------------------- #

# Vocabulary that means nothing to a household: JTS's own component and unit
# names, the seams between them, and operator commands. `DSP`, `DAC` and `USB`
# are deliberately NOT here — those are printed on the hardware a speaker owner
# buys, unlike `fan-in` or `outputd`, which exist only inside this repo.
_OPERATOR_VOCABULARY = re.compile(
    r"(?i)(?:^|[^A-Za-z])("
    r"fan-?in|outputd|camilladsp|dsp engine|mux|journalctl|systemctl|systemd|"
    r"install\.sh|reconciler|renderer|watchdog|work-loop|backend|alsa|"
    r"route plan|host-clock|signal path|shared audio path|"
    r"jasper-[a-z-]+|[a-z][a-z0-9-]*\.service"
    r")(?:[^A-Za-z]|$)"
)

# The payload keys whose values a household reads. `label` is in here because
# the evidence breakdown rows ({"label", "value"}) render on this same card, so
# a daemon name in a row label is as household-facing as one in a sentence.
# `technical` is skipped wholesale: it is the raw evidence block, and naming
# daemons is its whole job.
_MESSAGE_FIELDS = frozenset({
    "headline", "detail", "title", "summary", "observed", "likely_area", "label",
})


def _household_messages(payload, path: str = "") -> list[tuple[str, str]]:
    """Every household-read sentence in a composed snapshot, with its path."""
    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "technical":
                continue
            where = f"{path}.{key}" if path else str(key)
            if key in _MESSAGE_FIELDS and isinstance(value, str):
                found.append((where, value))
            else:
                found.extend(_household_messages(value, where))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(_household_messages(item, f"{path}[{index}]"))
    return found


def _live_parks() -> tuple[dict, ...]:
    """One `transport_park` verdict per park class (#3120).

    Driven from that module's own `_PARK_CASES` table rather than a second
    copy of it, so a fifth class added there is swept here without an edit —
    and each class's operator detail and remedy get their own chance to leak
    onto the household card.
    """
    from jasper.control import transport_park as transport_park_reader
    from tests.test_transport_park import _PARK_CASES

    return tuple(
        transport_park_reader.snapshot(case.values[0], case.values[1])
        for case in _PARK_CASES
    )


def _household_shapes() -> dict[str, dict]:
    """One composed snapshot per household-facing shape this module writes."""
    playing = {"selected": "usbsink", "ladder": "l0_locked"}

    def _mutated(mutate, **kwargs) -> dict:
        airplay = _airplay(**kwargs)
        mutate(airplay)
        return airplay

    def _fanin(airplay) -> dict:
        return airplay["current"]["fanin"]

    def _compose_with(airplay, **kwargs) -> dict:
        kwargs.setdefault("outputd", _outputd())
        kwargs.setdefault("route", _route())
        return compose_audio_health(
            airplay=airplay, issues=[], sampled_at=1000.0, **kwargs
        )

    def _second_tick(airplay_snapshots, outputd_snapshots) -> dict:
        now = [1000.0]
        outputd = list(outputd_snapshots)
        sampler = AudioHealthSampler(
            airplay_sampler=_FakeAirPlay(list(airplay_snapshots)),
            outputd_probe=lambda: outputd.pop(0),
            route_probe=_route,
            time_fn=lambda: now[0],
        )
        sampler._tick()
        now[0] += 5.0
        sampler._tick()
        return sampler.snapshot()

    parked = {"coherence_errors": [_ROUTE_DISCONNECTED], "capability_gap": None}
    # A REAL classifier verdict, not a hand-built one: the household sentence
    # must hold for whatever `transport_park` actually returns (#3120).
    live_park = _live_parks()[0]
    failed_unit = {
        "load_state": "loaded", "active_state": "failed", "result": "exit-code",
    }
    skipped_ping = _airplay()
    skipped_ping["current"]["fanin"]["watchdog"]["pings_skipped"] = 1

    return {
        "clean": _compose_with(_airplay(**playing)),
        "starting": _compose_with(
            _mutated(lambda ap: ap["current"].pop("fanin"), warmup=True)
        ),
        # Same shape from the other warmup branch: outputd not up yet.
        "starting_no_output": _compose_with(_airplay(warmup=True), outputd=None),
        "path_unreported": _compose_with(
            _mutated(lambda ap: ap["current"].pop("fanin"), **playing)
        ),
        "output_absent": _compose_with(_airplay(**playing), outputd=None),
        "output_backend_inactive": _compose_with(
            _airplay(**playing), outputd=_outputd(backend="none")
        ),
        "output_stalled": _compose_with(
            _airplay(**playing), outputd=_outputd(progress_age_ms=30_000)
        ),
        "output_deaf": _compose_with(
            _airplay(**playing), outputd=_outputd(content_deaf=True)
        ),
        "path_stalled": _compose_with(_mutated(
            lambda ap: _fanin(ap)["watchdog"].update(last_progress_age_ms=60_000),
            **playing,
        )),
        "input_absent": _compose_with(_mutated(
            lambda ap: _fanin(ap)["inputs"]["usbsink"].update(present=False),
            **playing,
        )),
        "input_broken": _compose_with(_mutated(
            lambda ap: _fanin(ap)["inputs"]["usbsink"].update(health="broken"),
            **playing,
        )),
        "input_stalled": _compose_with(_mutated(
            lambda ap: _fanin(ap)["inputs"]["usbsink"].update(frames_per_sec=0.0),
            **playing,
        )),
        "tts_queue_full": _compose_with(
            _airplay(**playing), outputd=_outputd(tts_pending_frames=96_000)
        ),
        "activity_unknown": _compose_with(
            _mutated(lambda ap: ap.pop("mux_status"), **playing)
        ),
        "camilla_stopped": _compose_camilla(_CAMILLA_CLEAN_STOP),
        "camilla_not_installed": _compose_camilla({
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
        }),
        "transport_parked": _compose(transport=parked),
        "transport_unservable": _compose_with(
            _airplay(**playing), transport_park=live_park
        ),
        "transport_parked_capability_gap": _compose(transport={
            "coherence_errors": [_ROUTE_DISCONNECTED],
            "capability_gap": {
                "device_id": "innomaker_hifi_amp_pro",
                "device_label": "InnoMaker HiFi AMP Pro",
            },
        }),
        "undeclared_hardware": _compose_with(
            _airplay(),
            outputd=None,
            output_hardware=_output_hardware(),
            output_topology_snapshot=_declared_topology(),
        ),
        "service_failed": _compose(
            service_states={"jasper-usbgadget.service": failed_unit},
        ),
        "off_drift": _compose(
            service_states={"jasper-usbsink.service": {
                "load_state": "loaded",
                "active_state": "active",
                "result": "success",
            }},
            source_intents={"usbsink": False},
        ),
        "source_not_running": _compose(
            service_states={"jasper-usbsink.service": {
                "load_state": "loaded",
                "active_state": "inactive",
                "result": "success",
            }},
        ),
        "usb_latency_fallback": _compose(selected="usbsink", ladder="l2_fallback"),
        "usb_clock_tracking_warn": _compose(selected="usbsink", ladder="l1_warn"),
        "usb_clock_unavailable": _compose(selected="usbsink"),
        "usb_route_unavailable": compose_audio_health(
            airplay=_airplay(selected="usbsink", ladder="l0_locked"),
            outputd=_outputd(),
            route={"status": "unavailable", "low_latency_claim": False},
            issues=[],
            sampled_at=1000.0,
        ),
        "outputd_xrun_recovered": _second_tick(
            [_airplay(), _airplay()], [_outputd(), _outputd(dac_xruns=2)]
        ),
        "fanin_watchdog_recovered": _second_tick(
            [_airplay(), skipped_ping], [_outputd(), _outputd()]
        ),
        "clipping": _second_tick(
            [_airplay(), _airplay()], [_outputd(), _outputd(clipped_samples=64)]
        ),
    }


@pytest.mark.parametrize("shape", sorted(_household_shapes()))
def test_every_household_sentence_stays_out_of_operator_register(shape: str) -> None:
    """#2472: no sentence on the household's audio card names JTS's internals.

    If this fails on copy you just wrote, the fix is the sentence — say what is
    wrong with the household's sound and what they can do. The unit name, the
    systemd state and the `journalctl` line belong in doctor, which fails on
    the same facts and already carries all three.
    """
    offenders = {
        where: text
        for where, text in _household_messages(_household_shapes()[shape])
        if _OPERATOR_VOCABULARY.search(text)
    }
    assert not offenders, (
        f"operator vocabulary on the household audio card ({shape}): {offenders}"
    )


def test_the_household_shapes_cover_every_signal_path_code() -> None:
    """The sweep above is only worth its keep if it reaches every shape.

    Equality against `audio_health.SIGNAL_PATH_CODES` — the module's own
    vocabulary, declared beside the branches that emit it — rather than a
    literal kept here. A literal only fails when a shape is RENAMED or
    RETIRED; this fails in both directions, so registering a new shape's code
    (the one edit a new branch cannot skip and still be legible) is what makes
    the sweep demand a fixture for it. What it cannot see is a branch that
    emits a code it never registered — which is exactly why the vocabulary
    lives in the module with the producers and not in this file.
    """
    swept = {
        snapshot["signal_path"]["code"]
        for snapshot in _household_shapes().values()
    }
    assert swept == audio_health.SIGNAL_PATH_CODES, (
        "every signal-path shape must reach the household-register sweep: "
        f"unswept={sorted(audio_health.SIGNAL_PATH_CODES - swept)} "
        f"unregistered={sorted(swept - audio_health.SIGNAL_PATH_CODES)}"
    )


def _every_incident_row() -> list[dict]:
    """Every row `_state_issues` can raise, over all of its branches.

    The rows reach the same card as the sentences swept above but are built by
    a second function, so they are swept from their own writer rather than
    trusted to echo. Each signal-path shape is crossed with the service,
    intent and latency states the other branches key on.
    """
    failed = {
        "load_state": "loaded", "active_state": "failed", "result": "exit-code",
    }
    running = {
        "load_state": "loaded", "active_state": "active", "result": "success",
    }
    service_states = {
        "jasper-camilla.service": _CAMILLA_CLEAN_STOP,
        "jasper-usbgadget.service": failed,
        "jasper-usbsink.service": running,
        "jasper-usbsink-volume.service": running,
    }
    latencies = (
        {"status": "unknown", "runtime": {"raw_mode": "l2_fallback"}},
        {"status": "ok", "runtime": {"raw_mode": "l1_warn"}},
        {"status": "ok", "runtime": {"raw_mode": "disabled"}},
    )
    without_fanin = _airplay(selected="usbsink")
    without_fanin["current"].pop("fanin")
    parks = (None, *_live_parks())
    rows: list[dict] = []
    for snapshot in _household_shapes().values():
        for airplay in (_airplay(selected="usbsink"), without_fanin):
            for latency in latencies:
                for intents in (None, {"usbsink": False}):
                    for park in parks:
                        rows.extend(audio_health._state_issues(
                            airplay,
                            None,
                            snapshot["signal_path"],
                            latency,
                            "usbsink",
                            service_states,
                            intents,
                            activity_unknown=True,
                            coherence_park=(
                                snapshot["signal_path"]
                                if snapshot["signal_path"].get("code")
                                == "transport_parked"
                                else None
                            ),
                            undeclared_hardware=None,
                            transport_park=park,
                        ))
    return rows


def test_every_incident_row_stays_out_of_operator_register() -> None:
    """#2472, for the incident rows: the same bar as the sentences above.

    An audio incident is read by whoever opens the card, not by whoever can
    read a unit file — the failing unit and its state stay in doctor, which
    fails on the same facts.
    """
    rows = _every_incident_row()
    offenders = {
        row["key"]: text
        for row in rows
        for text in (row["title"], row["detail"])
        if _OPERATOR_VOCABULARY.search(text)
    }
    assert not offenders, (
        f"operator vocabulary in audio incident rows: {offenders}"
    )
    # The sweep is only worth its keep if it reached every row. A new row fails
    # here until it is swept, rather than shipping unlinted. The unit names in
    # the last three are KEYS — structured identifiers, never read as a
    # sentence — which is exactly where a unit name is allowed to live.
    assert {row["key"] for row in rows} == {
        "monitor.mux_status_unavailable",
        "path.transport_park.grouped_dac_content_lane",
        "path.transport_park.mono_full_range",
        "path.transport_park.passive_stereo_composite",
        "path.transport_park.roleful_active_endpoint_unconverged",
        "path.transport_parked",
        "path.camilla_stopped",
        "path.fanin_unavailable",
        "path.fanin_watchdog_stale",
        "path.outputd_backend_inactive",
        "path.outputd_content_deaf",
        "path.outputd_unavailable",
        "path.outputd_watchdog_stale",
        "path.tts_queue_full",
        "usbsink.clock_tracking_warn",
        "usbsink.host_clock_unavailable",
        "usbsink.input_unavailable",
        "usbsink.latency_fallback",
        "usbsink.latency_state_unavailable",
        "usbsink.service.jasper-usbgadget.service",
        "usbsink.service.jasper-usbsink-volume.service.off_drift",
        "usbsink.service.jasper-usbsink.service.off_drift",
    }


@pytest.mark.parametrize(
    ("elapsed", "expected_sleep"),
    [
        (0.0, 5.0),
        (4.5, 1.0),
        (60.0, 1.0),
    ],
)
def test_run_sleep_floor_bounds_the_tick_rate(
    monkeypatch, elapsed: float, expected_sleep: float,
) -> None:
    sampler = AudioHealthSampler(sample_interval_sec=5.0, time_fn=lambda: 1000.0)
    monkeypatch.setattr(sampler, "_tick", lambda: None)
    monotonic_values = iter([0.0, elapsed])
    monkeypatch.setattr(
        audio_health.time, "monotonic", lambda: next(monotonic_values),
    )
    captured: list[float] = []

    def fake_sleep(seconds: float) -> None:
        captured.append(seconds)
        sampler._stopped = True

    monkeypatch.setattr(audio_health.time, "sleep", fake_sleep)
    sampler._run()
    assert captured == [expected_sleep]
