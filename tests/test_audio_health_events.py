# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AudioHealthSampler's counter-baseline bookkeeping: one-shot raw events
(``_record_raw_events``, now ``audio_health_events.record_raw_events``) and
per-tick counter deltas (``_record_counter_events`` /
``audio_health_events.record_counter_events``), exercised through the
sampler so a test pins the observable incident, not the private function
shape.
"""

from __future__ import annotations

import pytest

from jasper.control.audio_health_sampler import AudioHealthSampler
from jasper.control.audio_incidents import IncidentStore

from .audio_health_fixtures import _FakeAirPlay, _airplay, _mux, _outputd, _route


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
        mux_probe=lambda: _mux("spotify"),
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
        mux_probe=lambda: _mux("airplay"),
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
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux("usbsink"),
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
        mux_probe=lambda: _mux("spotify"),
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


def test_sampler_records_outputd_xrun_delta_as_a_recovered_blip() -> None:
    now = [1000.0]
    outputd = [_outputd(), _outputd(dac_xruns=2)]
    sampler = AudioHealthSampler(
        airplay_sampler=_FakeAirPlay([_airplay(), _airplay()]),
        outputd_probe=lambda: outputd.pop(0),
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux(),
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
        mux_probe=lambda: _mux("usbsink"),
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
        mux_probe=lambda: _mux("usbsink"),
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
