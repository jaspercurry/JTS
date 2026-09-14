# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.control import audio_health_sampler, audio_signal_path
from jasper.control.audio_health_sampler import AudioHealthSampler
from jasper.control.audio_incidents import IncidentStore
from jasper.output_hardware import OutputHardwareState
from jasper.output_topology import OutputTopologySnapshot

from .audio_health_fixtures import (
    _FakeAirPlay,
    _airplay,
    _declared_topology,
    _outputd,
    _route,
)


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


@pytest.mark.parametrize(
    ("unit", "key"),
    [
        ("jasper-fanin.service", "path.fanin.restarted"),
        ("jasper-camilla.service", "path.camilla.restarted"),
        ("jasper-outputd.service", "path.outputd.restarted"),
    ],
)
def test_shared_path_restarts_are_recorded_as_incidents(
    unit: str, key: str,
) -> None:
    now = [1000.0]
    restarts = [0, 2]

    def service_states() -> dict:
        return {unit: {"n_restarts": restarts.pop(0)}} if restarts else {}

    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([
            _airplay(selected="usbsink", ladder="l0_locked"),
        ]),
        outputd_probe=_outputd,
        mux_probe=lambda: None,
        route_probe=_route,
        service_probe=service_states,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    assert not [i for i in sampler.snapshot()["issues"] if i["key"] == key]

    now[0] += 5.0
    sampler._tick()
    recorded = [i for i in sampler.snapshot()["issues"] if i["key"] == key]

    assert [i["count"] for i in recorded] == [2]
    assert recorded[0]["impact"] == "continuity"


def test_sampler_freezes_host_pressure_onto_an_incident() -> None:
    now = [1000.0]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([
            _airplay(selected="usbsink", ladder="l0_locked"),
        ]),
        outputd_probe=lambda: _outputd(dac_xruns=1 if now[0] > 1000.0 else 0),
        mux_probe=lambda: None,
        route_probe=_route,
        system_probe=lambda: {
            "throttled_now": 0,
            "throttled_history": 4,
            "mem_psi_some_avg60": 12.5,
        },
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    incident = next(
        i for i in sampler.snapshot()["recent_incidents"]
        if i["key"] == "path.outputd_dac_xrun"
    )
    evidence = {row["label"]: row["value"] for row in incident["evidence"]}

    # A sticky since-boot bit must not be rendered as a live condition.
    assert evidence["Power or heat throttling"] == "Earlier this boot"
    assert evidence["Memory pressure"] == "12%"


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
    assert (
        health["overall"]["headline"]
        == audio_signal_path.UNDECLARED_HARDWARE_HEADLINE
    )


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
                "type": "camilla_playback_underrun",
                "detail": "Camilla recovered.",
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
        "path.camilla_playback_underrun",
        "airplay.shairport_packet_drop",
    }


def test_delayed_raw_event_is_not_attributed_to_new_playback_session() -> None:
    now = [1000.0]
    delayed = _airplay(
        selected="usbsink",
        ladder="l0_locked",
        events=[{
            "ts": 990.0,
            "type": "camilla_playback_underrun",
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
                "type": "camilla_playback_underrun",
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
        if row["key"] == "path.camilla_playback_underrun"
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
        audio_health_sampler.time, "monotonic", lambda: next(monotonic_values),
    )
    captured: list[float] = []

    def fake_sleep(seconds: float) -> None:
        captured.append(seconds)
        sampler._stopped = True

    monkeypatch.setattr(audio_health_sampler.time, "sleep", fake_sleep)
    sampler._run()
    assert captured == [expected_sleep]
