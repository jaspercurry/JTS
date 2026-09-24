# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.control.fanin_view import FaninView
from jasper.music_sources import MUSIC_SOURCE_SPECS
from tests.test_airplay_health import _fanin_status, _ring, _sampler


def _ignore_events(*_args, **_kwargs) -> None:
    pass


def test_fanin_xrun_delta_surfaces_issue_without_recounting_baseline() -> None:
    now = [1000.0]
    statuses = [
        _fanin_status(
            airplay_frames=0,
            airplay_xruns=7,
            output_frames=0,
        ),
        _fanin_status(
            airplay_frames=240000,
            airplay_xruns=8,
            output_frames=240000,
        ),
    ]

    sampler = _sampler(
        fanin_probe=lambda: statuses.pop(0),
        journal_reader=lambda _unit, _since, _now: [],
        mpris_probe=lambda: {"playing": True},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()

    snap = sampler.snapshot()
    assert snap["status"] == "issue"
    assert snap["summary_5m"]["fanin_airplay_xruns"] == 1
    assert snap["current"]["fanin"]["airplay"]["frames_per_sec"] == 48000.0
    assert snap["events"][-1]["type"] == "fanin_airplay_xrun"


def test_fanin_output_ring_and_tts_reach_the_composer() -> None:
    """The shaped fan-in observation carries the output ring, its per-second
    rates and the TTS lane; absent blocks stay None."""
    now = 3000.0
    statuses = [
        {
            **_fanin_status(output_frames=0),
            "tts": {"enabled": True, "pending_frames": 0, "budget_frames": 96000},
        },
        {
            **_fanin_status(output_frames=480000),
            "tts": {"enabled": True, "pending_frames": 0, "budget_frames": 96000},
        },
    ]
    for status in statuses:
        started = status["output"]["frames_written"] != 0
        status["output"]["ring"] = {
            "occupancy": 2,
            "slots": 2,
            "stall_active": False,
            "full_waits": 810 if started else 0,
            "stuck_reader_drops": 1 if started else 0,
            "drop_no_reader": 1 if started else 0,
        }

    view = FaninView(probe=lambda: statuses.pop(0))
    view.sample(now, record_event=_ignore_events)
    now += 5.0
    view.sample(now, record_event=_ignore_events)
    output = view.current["output"]

    assert output["ring"]["occupancy"] == 2
    assert output["ring"]["full_waits_per_sec"] == 162.0
    assert output["ring"]["drops_per_sec"] == 0.4
    assert view.current["tts"]["enabled"] is True


def test_fanin_ring_and_tts_absent_stay_none() -> None:
    view = FaninView(probe=lambda: _fanin_status())
    view.sample(3000.0, record_event=_ignore_events)
    fanin = view.current

    assert fanin["output"]["ring"] is None
    assert fanin["inputs"]["airplay"]["xruns_per_sec"] is None
    assert fanin["tts"] is None


def test_ring_block_surfaces_empty_reads_rate_and_silent_ms() -> None:
    now = 1000.0
    statuses = [
        _fanin_status(selected_input="airplay", ring=_ring(empty_reads=1000)),
        _fanin_status(selected_input="airplay", ring=_ring(empty_reads=1100)),
    ]
    view = FaninView(probe=lambda: statuses.pop(0))

    view.sample(now, record_event=_ignore_events)
    now += 5.0
    view.sample(now, record_event=_ignore_events)

    airplay_obs = view.current["inputs"]["airplay"]
    assert airplay_obs["ring"]["empty_reads"] == 1100
    assert airplay_obs["empty_reads_per_sec"] == 20.0
    # 20 empty_reads/s * 256 slot_frames / 48000 Hz * 1000 = 106.67 ms/s.
    assert airplay_obs["silent_ms_per_sec"] == 106.7


def test_airplay_collector_exposes_fixed_declared_inputs_and_host_clock() -> None:
    now = 1000.0
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
    view = FaninView(probe=lambda: status)

    view.sample(now, record_event=_ignore_events)
    fanin = view.current
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
    # The decontamination gauge rides the wholesale decay block (#3466).
    assert fanin["inputs"]["usbsink"]["resampler"]["decay"]["demand_ppm"] == 125.33
    assert fanin["inputs"]["spotify"]["present"] is False
    assert fanin["host_clock"]["ladder"] == "l0_locked"

    status["inputs"][0]["frames_read"] += 48000
    now += 1.0
    view.sample(now, record_event=_ignore_events)
    fanin = view.current
    assert fanin["inputs"]["usbsink"]["frames_per_sec"] == 48000.0
