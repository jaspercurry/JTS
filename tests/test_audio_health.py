# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from jasper.control.airplay_health import AirPlayHealthSampler
from jasper.control.audio_health import (
    AudioHealthSampler,
    IssueTracker,
    compose_audio_health,
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
            },
            "mpris": {"playing": selected == "airplay"},
            "camilla": None,
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
) -> dict:
    return {
        "backend": backend,
        "content": {"xrun_count": content_xruns},
        "dac": {"xrun_count": dac_xruns},
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
) -> dict:
    return compose_audio_health(
        airplay=_airplay(selected=selected, ladder=ladder),
        outputd=_outputd(),
        route=_route(artifact_status=artifact_status),
        issues=[],
        sampled_at=1000.0,
        service_states=service_states,
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


def test_l0_does_not_turn_a_failed_artifact_into_a_verified_claim() -> None:
    health = _compose(
        selected="usbsink",
        ladder="l0_locked",
        artifact_status="fail",
    )

    assert health["latency"]["runtime"]["mode"] == "lowest_latency"
    assert health["latency"]["verification"]["status"] == "unverified"
    assert health["latency"]["status"] == "warn"
    assert "verification needed" in health["latency"]["headline"]


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
    assert health["overall"]["status"] == "warn"
    assert health["overall"]["headline"] == "A playback source needs attention"


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


def test_ancillary_pairing_agent_failure_does_not_disable_bluetooth() -> None:
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
    assert bluetooth["state"] == "ready"
    assert bluetooth["status"] == "ok"
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


def test_selected_source_without_frame_progress_is_a_continuity_issue() -> None:
    airplay = _airplay(selected="spotify")
    airplay["current"]["fanin"]["inputs"]["spotify"]["frames_per_sec"] = 0.0
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert health["signal_path"]["status"] == "issue"
    assert health["signal_path"]["headline"] == "Active audio input is not flowing"
    assert health["overall"]["status"] == "issue"


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


def test_p99_budget_failure_is_not_described_as_incomplete_evidence() -> None:
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
    assert health["latency"]["headline"] == "USB latency target not met"
    assert "42 ms budget" in health["latency"]["detail"]


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


def test_sampler_tracks_source_service_failure_lifecycle() -> None:
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
    assert any(
        issue["key"] == key and issue["status"] == "ongoing"
        for issue in first["issues"]
    )

    now[0] += 5.0
    sampler._tick()
    second = sampler.snapshot()
    assert second is not None
    assert any(
        issue["key"] == key and issue["status"] == "recovered"
        for issue in second["issues"]
    )


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
