# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
import types

import pytest


import jasper.control.airplay_health as airplay_health
from jasper import service_units
from jasper.control._health_fields import as_int
from jasper.control.airplay_health import (
    AirPlayHealthSampler,
    classify_journal_line,
)
from jasper.control.camilla_health import CamillaHealth
from jasper.control.camilla_rate_storm import STORM_EXIT_DEBOUNCE_SEC, CamillaRateStorm
from jasper.control.fanin_view import FaninView


def _fanin_status(
    *,
    airplay_frames: int = 0,
    airplay_xruns: int = 0,
    output_frames: int = 0,
    input_buffer_frames: int = 4096,
    progress_age_ms: int = 0,
    selected_input: str | None = None,
    ring: dict | None = None,
) -> dict:
    airplay_input = {
        "label": "airplay",
        "pcm": "hw:Loopback,1,1",
        "frames_read": airplay_frames,
        "xrun_count": airplay_xruns,
    }
    if ring is not None:
        airplay_input["ring"] = ring
    return {
        "input_buffer_frames": input_buffer_frames,
        "selected_input": selected_input,
        "inputs": [airplay_input],
        "output": {
            "sample_rate": 48000,
            "period_frames": 256,
            "frames_written": output_frames,
        },
        "watchdog": {
            "pings_sent": 10,
            "pings_skipped": 0,
            "last_progress_age_ms": progress_age_ms,
        },
    }


def _sampler(
    *, fanin_probe=None, camilla_probe=None, rate_storm=None,
    time_fn=time.time, **kwargs,
) -> AirPlayHealthSampler:
    """Build a sampler isolated from live Pi maintenance markers.

    Warmup + connect-grace default OFF here so the classification tests
    below exercise steady-state behaviour at small clock values; the
    warmup / connect-grace suppression has its own dedicated tests.
    ``fanin_probe`` feeds the composed :class:`FaninView`; ``camilla_probe``
    and ``rate_storm`` feed the composed :class:`CamillaHealth`.
    """
    kwargs.setdefault("maintenance_suppress_path", None)
    kwargs.setdefault("warmup_sec", 0.0)
    kwargs.setdefault("connect_grace_sec", 0.0)
    return AirPlayHealthSampler(
        fanin_view=FaninView(probe=fanin_probe),
        camilla=CamillaHealth(
            probe=camilla_probe, rate_storm=rate_storm, time_fn=time_fn,
        ),
        time_fn=time_fn,
        **kwargs,
    )


@pytest.mark.parametrize("value", [True, False, float("inf")])
def test_as_int_treats_bool_and_infinity_as_absent(value: object) -> None:
    """A stray bool or infinity in upstream JSON reads as "couldn't tell" (the
    default), never as True/False's numeric identity or an OverflowError."""
    assert as_int(value, default=7) == 7


def test_classify_journal_lines_for_documented_airplay_patterns() -> None:
    drop = classify_journal_line(
        "shairport-sync",
        "player.c:1130 Dropping out of date packet 123. "
        "Lead time is 0.118 seconds",
    )
    assert drop is not None
    assert drop["type"] == "shairport_packet_drop"
    assert drop["severity"] == "issue"
    assert drop["lead_time_sec"] == 0.118


def test_offset_too_short_warning_rolls_into_shairport_events() -> None:
    """The bonded-leader tight-regime warning must affect the AirPlay-health
    status, not just sit in the raw event list — i.e. it has to roll into the
    `shairport_events` bucket like its siblings (shairport_oos /
    shairport_broken_pipe). Without the EVENT_BUCKET_FIELD mapping the event
    would be invisible to `_status_locked`'s 30 m verdict."""
    too_short = (
        "The stream latency (0.300000 seconds) is too short to accommodate an "
        "audio backend latency offset of 0.550000 seconds and a backend buffer "
        "of 0.100000 seconds. The audio_backend_latency_offset has been set to "
        "zero."
    )
    ev = classify_journal_line("shairport-sync", too_short)
    assert ev is not None and ev["type"] == "shairport_offset_too_short"

    now = [1000.0]
    sampler = _sampler(time_fn=lambda: now[0])
    sampler._record_event(now[0], ev)
    summary = sampler._summary_locked(window_sec=1800.0)
    assert summary["shairport_events"] >= 1


def test_offset_too_short_warning_moves_status_verdict_end_to_end() -> None:
    """End-to-end pin of the 'moves the status verdict, not just the event list'
    promise: drive the full journal -> classify -> record -> status path and
    assert the AirPlay-health status becomes 'watch' (the 30 m shairport_events
    verdict), with the audio path otherwise healthy.

    The warning is a bonded-leader lip-sync event that only occurs *while
    actively streaming*, so the scenario is mpris=playing with a frame-rate
    baseline established — the condition under which non-fatal warnings
    surface as 'watch' (idle reads 'inactive'; see _status_locked)."""
    now = [2000.0]
    frames = [0]

    def fanin_probe() -> dict:
        frames[0] += 480000  # keep the rate above the 1000 floor
        return _fanin_status(airplay_frames=frames[0])

    def journal(_units, _since: float, _now: float) -> list[tuple[str, str]]:
        return [(
            "shairport-sync",
            "The stream latency (0.300000 seconds) is too short to accommodate "
            "an audio backend latency offset of 1.050000 seconds and a backend "
            "buffer of 0.500000 seconds. The audio_backend_latency_offset has "
            "been set to zero.",
        )]

    sampler = _sampler(
        fanin_probe=fanin_probe,
        journal_reader=journal,
        mpris_probe=lambda: {"playing": True},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )
    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    snap = sampler.snapshot()
    assert snap["status"] == "watch"
    assert snap["summary_30m"]["shairport_events"] >= 1


def test_deploy_maintenance_suppresses_events_and_advances_journal_cursor(
    tmp_path,
) -> None:
    marker = tmp_path / "airplay-health-suppress-until"
    marker.write_text("1020\n", encoding="utf-8")
    now = [1000.0]
    statuses = [
        _fanin_status(airplay_frames=0, airplay_xruns=0, output_frames=0),
        _fanin_status(
            airplay_frames=240000,
            airplay_xruns=2,
            output_frames=240000,
        ),
        _fanin_status(
            airplay_frames=1680000,
            airplay_xruns=3,
            output_frames=1680000,
        ),
    ]
    journal_calls: list[tuple[tuple[str, ...], float, float]] = []

    def journal(units, since: float, until: float) -> list[tuple[str, str]]:
        journal_calls.append((units, since, until))
        if "shairport-sync" not in units:
            return []
        return [("shairport-sync", "recovering from a previous underrun")]

    sampler = AirPlayHealthSampler(
        fanin_view=FaninView(probe=lambda: statuses.pop(0)),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": False},
        camilla=CamillaHealth(probe=lambda: None, time_fn=lambda: now[0]),
        maintenance_suppress_path=str(marker),
        warmup_sec=0.0,
        connect_grace_sec=0.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()

    snap = sampler.snapshot()
    assert snap["maintenance_suppressed"] is True
    assert snap["maintenance_suppressed_until"] == 1020.0
    assert snap["summary_5m"]["fanin_airplay_xruns"] == 0
    assert snap["summary_5m"]["shairport_underruns"] == 0
    assert snap["events"] == []
    assert journal_calls == []

    marker.write_text("1000\n", encoding="utf-8")
    now[0] = 1035.0
    sampler._tick()

    snap = sampler.snapshot()
    assert snap["maintenance_suppressed"] is False
    assert snap["maintenance_suppressed_until"] is None
    assert snap["summary_5m"]["fanin_airplay_xruns"] == 1
    assert snap["summary_5m"]["shairport_underruns"] == 1
    assert snap["events"][-1]["type"] == "shairport_underrun"
    assert "shairport-sync" in journal_calls[0][0]
    assert journal_calls[0][1] == 1005.0


def test_journal_scan_widens_after_no_airplay_session_for_5_minutes() -> None:
    """R21 (#4416): an idle box forks journalctl every JOURNAL_INTERVAL_SEC
    forever. Once no session has been seen for JOURNAL_IDLE_THRESHOLD_SEC,
    SHAIRPORT's scan cadence widens to JOURNAL_IDLE_INTERVAL_SEC. CAMILLA's
    scan feeds the short-read storm detector, which fires on any source's
    audio path (not only AirPlay's), so it stays on the base cadence."""
    now = [1000.0]
    shairport_calls: list[float] = []
    camilla_calls: list[float] = []

    def journal(units, since, until) -> list[tuple[str, str]]:
        if "shairport-sync" in units:
            shairport_calls.append(now[0])
        if "jasper-camilla" in units:
            camilla_calls.append(now[0])
        return []

    sampler = AirPlayHealthSampler(
        fanin_view=FaninView(probe=lambda: _fanin_status()),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": False},
        camilla=CamillaHealth(probe=lambda: None, time_fn=lambda: now[0]),
        maintenance_suppress_path=None,
        warmup_sec=0.0,
        connect_grace_sec=0.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()  # t=1000, idle_for=0: default 30 s cadence, both scan.
    assert len(shairport_calls) == 1
    assert len(camilla_calls) == 1

    now[0] += 305.0  # t=1305, idle_for=305 >= the 300 s threshold: widened.
    sampler._tick()  # shairport: 305 >= 120 s widened -> scans.
    assert len(shairport_calls) == 2
    assert len(camilla_calls) == 2  # camilla: unaffected, still base cadence.

    now[0] += 100.0  # t=1405, only 100 s since shairport's last scan.
    sampler._tick()  # shairport: 100 < 120 s widened -> no scan.
    assert len(shairport_calls) == 2
    assert len(camilla_calls) == 3  # camilla: 100 >= 30 s base -> scans.

    now[0] += 30.0  # t=1435, 130 s since shairport's last scan.
    sampler._tick()  # shairport: 130 >= 120 s widened -> scans.
    assert len(shairport_calls) == 3
    assert len(camilla_calls) == 4  # camilla: 30 >= 30 s base -> scans.


def test_journal_scan_returns_to_default_cadence_once_a_session_starts() -> None:
    """The idle->active transition resets the idle clock, so SHAIRPORT's next
    scan after a session starts is back on the 30 s cadence, not still
    widened."""
    now = [1000.0]
    shairport_calls: list[float] = []
    mpris = {"playing": False}

    def journal(units, since, until) -> list[tuple[str, str]]:
        if "shairport-sync" in units:
            shairport_calls.append(now[0])
        return []

    sampler = AirPlayHealthSampler(
        fanin_view=FaninView(probe=lambda: _fanin_status()),
        journal_reader=journal,
        mpris_probe=lambda: dict(mpris),
        camilla=CamillaHealth(probe=lambda: None, time_fn=lambda: now[0]),
        maintenance_suppress_path=None,
        warmup_sec=0.0,
        connect_grace_sec=0.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()  # t=1000, scans.
    now[0] += 305.0  # t=1305, idle -> widened cadence.
    sampler._tick()  # 305 >= 120 -> scans.
    assert len(shairport_calls) == 2

    mpris["playing"] = True
    now[0] += 30.0  # t=1335: the MPRIS resample (30 s interval) sees the
    sampler._tick()  # session start; idle_for resets to 0, still 30 >= 30.
    assert len(shairport_calls) == 3

    now[0] += 35.0  # t=1370, 35 s since the last scan.
    sampler._tick()  # 35 >= the DEFAULT 30 s (not still 120 s) -> scans.
    assert len(shairport_calls) == 4


def test_camilla_short_reads_are_watch_while_actively_streaming() -> None:
    # While AirPlay IS streaming, recoverable Camilla short reads are a
    # non-fatal warning (watch), not a hard issue.
    now = [2000.0]
    frames = [0, 240000]

    def journal(_units, _since: float, _now: float) -> list[tuple[str, str]]:
        if "jasper-camilla" not in _units:
            return []
        return [
            ("jasper-camilla", "Capture read 768 frames instead of the requested 1024"),
            ("jasper-camilla", "Capture read 960 frames instead of the requested 1024"),
        ]

    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(airplay_frames=frames.pop(0)),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": True},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()            # records short reads + frame baseline
    now[0] += 5.0
    sampler._tick()            # frame rate now ~48 kHz
    snap = sampler.snapshot()

    assert snap["status"] == "watch"
    assert snap["summary_5m"]["camilla_short_reads"] == 2
    assert snap["summary_5m"]["camilla_playback_underruns"] == 0


def test_idle_silence_at_full_rate_reads_inactive_not_ok() -> None:
    # The airplay input lane free-runs at ~48 kHz of SILENCE with no
    # sender, so a high frame rate must NOT read as "AirPlay path clean".
    # shairport PlaybackStatus (MPRIS) is authoritative -> inactive.
    # (2026-06-22 report: idle JTS2 showing frames with nothing playing.)
    now = [4000.0]
    frames = [0, 240000]
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(airplay_frames=frames.pop(0)),
        journal_reader=lambda _unit, _since, _now: [],
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    snap = sampler.snapshot()

    assert snap["current"]["fanin"]["airplay"]["frames_per_sec"] == 48000.0
    assert snap["status"] == "inactive"
    assert snap["reason"] == "AirPlay not currently streaming"


def test_idle_camilla_short_reads_do_not_escalate_to_watch() -> None:
    # Benign Camilla short reads can occur on the idle (silence) pipeline.
    # With AirPlay not streaming they must read "inactive", never "watch" —
    # but they are still RECORDED for history/diagnostics.
    now = [5000.0]

    def journal(_units, _since: float, _now: float) -> list[tuple[str, str]]:
        if "jasper-camilla" not in _units:
            return []
        return [(
            "jasper-camilla",
            "Capture read 586 frames instead of the requested 1024",
        )]

    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(airplay_frames=240000),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    snap = sampler.snapshot()

    assert snap["status"] == "inactive"
    assert snap["reason"] == "AirPlay not currently streaming"
    assert snap["summary_5m"]["camilla_short_reads"] == 1


def test_mpris_unavailable_reads_unknown_not_guessed_ok() -> None:
    # If the shairport MPRIS probe fails, the silent free-running rate
    # can't substitute -> unknown, not a guessed "ok".
    now = [6000.0]
    frames = [0, 240000]
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(airplay_frames=frames.pop(0)),
        journal_reader=lambda _unit, _since, _now: [],
        mpris_probe=lambda: None,
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    snap = sampler.snapshot()

    assert snap["status"] == "unknown"
    assert "playback status unavailable" in snap["reason"]


def test_playing_but_frames_not_arriving_is_issue() -> None:
    # shairport reports playing but the airplay lane is barely advancing
    # (sender stalled / substream broke) -> a real fault, not "ok".
    now = [7000.0]
    frames = [0, 500]  # ~100 frames/s over 5 s, well under the 1000 floor
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(airplay_frames=frames.pop(0)),
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
    assert "not receiving frames" in snap["reason"]


def test_fanin_input_buffer_regression_is_issue() -> None:
    now = [3000.0]
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(input_buffer_frames=2048),
        journal_reader=lambda _unit, _since, _now: [],
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    snap = sampler.snapshot()

    assert snap["status"] == "issue"
    assert "4096" in snap["reason"]


def test_snapshot_returns_independent_nested_copies() -> None:
    now = [4000.0]
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(),
        journal_reader=lambda _unit, _since, _now: [],
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    snap = sampler.snapshot()
    snap["current"]["fanin"]["airplay"]["xrun_count"] = 999
    snap["events"].append({"type": "mutated"})

    fresh = sampler.snapshot()
    assert fresh["current"]["fanin"]["airplay"]["xrun_count"] == 0
    assert fresh["events"] == []


def test_mpris_playing_waits_for_fanin_rate_baseline() -> None:
    now = [5000.0]
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(airplay_frames=48000),
        journal_reader=lambda _unit, _since, _now: [],
        mpris_probe=lambda: {"playing": True},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    snap = sampler.snapshot()

    assert snap["status"] == "unknown"
    assert "baseline" in snap["reason"]


def test_default_journal_reader_uses_since_and_until(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="\n".join([
            json.dumps({"_SYSTEMD_UNIT": "shairport-sync.service", "MESSAGE": "one"}),
            json.dumps({"_SYSTEMD_UNIT": "librespot.service", "MESSAGE": "two"}),
            json.dumps({"_SYSTEMD_UNIT": "sshd.service", "MESSAGE": "not scanned"}),
            json.dumps({"_SYSTEMD_UNIT": "librespot.service", "MESSAGE": [1, 2]}),
            json.dumps(["not", "an", "object"]),
            "not json",
        ]) + "\n")

    monkeypatch.setattr(service_units.subprocess, "run", fake_run)

    lines = AirPlayHealthSampler._read_journal_lines(
        ("shairport-sync", "librespot"),
        10.1234,
        40.5678,
    )

    assert lines == [("shairport-sync", "one"), ("librespot", "two")]
    assert calls
    args, kwargs = calls[0]
    assert args.count("-u") == 2
    assert args[args.index("--since") + 1] == "@10.123"
    assert args[args.index("--until") + 1] == "@40.568"
    assert "--output-fields=_SYSTEMD_UNIT,MESSAGE" in args
    assert kwargs["timeout"] == airplay_health.SUBPROCESS_TIMEOUT_SEC


def test_default_journal_reader_fails_soft_when_journal_is_unavailable(
    monkeypatch,
) -> None:
    def fail(*_a, **_kw):
        raise service_units.JournalctlUnavailable("timed out")

    monkeypatch.setattr(airplay_health, "run_journalctl_json", fail)

    assert AirPlayHealthSampler._read_journal_lines(("shairport-sync",), 1, 2) == []


def test_boot_warmup_suppresses_transient_audio_path_events() -> None:
    # A reboot's content-xrun + AirPlay-resync settling must NOT flip the
    # dashboard straight to "issue: recent audio-path recovery event"
    # during the warmup window (the 2026-06-21 post-reboot dashboard).
    now = [1000.0]
    statuses = [
        _fanin_status(airplay_frames=0, airplay_xruns=5, output_frames=0),
        _fanin_status(
            airplay_frames=240000, airplay_xruns=7, output_frames=240000,
        ),
        _fanin_status(
            airplay_frames=6000000, airplay_xruns=9, output_frames=6000000,
        ),
    ]
    sampler = AirPlayHealthSampler(
        fanin_view=FaninView(probe=lambda: statuses.pop(0)),
        journal_reader=lambda _u, _s, _n: [
            ("shairport-sync", "recovering from a previous underrun"),
        ],
        mpris_probe=lambda: {"playing": False},
        camilla=CamillaHealth(probe=lambda: None, time_fn=lambda: now[0]),
        maintenance_suppress_path=None,
        warmup_sec=120.0,
        connect_grace_sec=0.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()            # t=1000, within warmup (started_at=1000)
    now[0] += 5.0
    sampler._tick()            # t=1005, still within warmup

    snap = sampler.snapshot()
    assert snap["warmup_active"] is True
    assert snap["suppressed_reason"] == "warmup"
    assert snap["summary_5m"]["fanin_airplay_xruns"] == 0
    assert snap["summary_5m"]["shairport_underruns"] == 0
    assert snap["events"] == []
    assert snap["status"] != "issue"

    # Past the warmup window a genuine recovery event surfaces again.
    now[0] = 1000.0 + 121.0
    sampler._tick()

    snap = sampler.snapshot()
    assert snap["warmup_active"] is False
    assert snap["suppressed_reason"] is None
    assert snap["summary_5m"]["fanin_airplay_xruns"] == 2
    assert snap["status"] == "issue"


def test_airplay_connect_grace_suppresses_session_establish(tmp_path) -> None:
    now = [5000.0]
    mpris = {"playing": False}
    journal: dict[str, list[str]] = {"shairport-sync": [], "jasper-camilla": []}
    calls = []

    def reader(units, since, until):
        calls.append((units, since, until))
        return [(unit, line) for unit in units for line in journal[unit]]

    sampler = _storm_sampler(
        now, reader=reader, tmp_dir=str(tmp_path),
        fanin_probe=lambda: _fanin_status(
            airplay_frames=int(now[0] * 48000),
            airplay_xruns=int(now[0] >= 5036.0) + int(now[0] >= 5076.0),
        ),
        mpris_probe=lambda: dict(mpris),
        connect_grace_sec=45.0,
    )
    sampler._tick()
    assert sampler.snapshot()["suppressed_reason"] is None

    mpris["playing"] = True
    journal["shairport-sync"] = ["rtp.c sync: Large negative sync error"]
    journal["jasper-camilla"] = _material_short_read_lines(100)
    for offset, count in ((31.0, 100), (36.0, 100), (61.0, 200)):
        now[0] = 5000.0 + offset
        sampler._tick()
        snap = sampler.snapshot()
        assert snap["suppressed_reason"] == "airplay_connect"
        assert snap["connect_grace_until"] == 5076.0
        assert snap["summary_5m"]["shairport_sync_errors"] == 0
        assert snap["summary_5m"]["fanin_airplay_xruns"] == 0
        assert snap["summary_5m"]["camilla_short_reads"] == count
        assert {event["type"] for event in snap["events"]} == {"camilla_short_read"}
        assert snap["storm"]["active"] is True
        assert snap["status"] == "watch"
    assert calls == [
        (("shairport-sync",), 5000.0, 5000.0),
        (("jasper-camilla",), 5000.0, 5000.0),
        (("jasper-camilla",), 5000.0, 5031.0),
        (("jasper-camilla",), 5031.0, 5061.0),
    ]
    assert snap["storm"]["material_per_min"] == 200.0
    assert snap["storm"]["samples"] == 3

    now[0] += 46.0
    sampler._tick()
    snap = sampler.snapshot()
    assert snap["suppressed_reason"] is None
    assert snap["summary_5m"]["shairport_sync_errors"] == 1
    assert snap["summary_5m"]["fanin_airplay_xruns"] == 1
    assert snap["status"] == "issue"


def _material_short_read_lines(count: int, frames: int = 970) -> list[str]:
    # deficit 1024-970 = 54 > 11 => material; matches CAMILLA_SHORT_READ_RE.
    return [
        f"PB: Capture read {frames} frames instead of the requested 1024"
        for _ in range(count)
    ]


def _storm_sampler(
    now, *, reader, tmp_dir,
    storm_exit_debounce_sec: float = STORM_EXIT_DEBOUNCE_SEC, **kw,
) -> AirPlayHealthSampler:
    cam = {"rate_adjust": 1.0002, "capture_rate": 48125, "buffer_level": 2040}
    ctx = {
        "soc_temp_c": 52.0,
        "cpu_governor": "ondemand",
        "cpu_freq_khz": 1_500_000,
        "sec_since_camilla_restart": 600.0,
        "sec_since_deploy": 7200.0,
    }
    kw.setdefault("fanin_probe", lambda: _fanin_status(airplay_frames=240000))
    kw.setdefault("mpris_probe", lambda: {"playing": True})
    kw.setdefault("camilla_probe", lambda: dict(cam))
    kw.setdefault("time_fn", lambda: now[0])
    rate_storm = CamillaRateStorm(
        exit_debounce_sec=storm_exit_debounce_sec,
        trajectory_dir=tmp_dir,
        context_probe=lambda: dict(ctx),
    )
    return _sampler(journal_reader=reader, rate_storm=rate_storm, **kw)


def _ring(**overrides) -> dict:
    ring = {
        "attached": True,
        "writer_alive": True,
        "writer_pid": 1,
        "occupancy": 1,
        "empty_reads": 1000,
        "startup_empty_reads": 0,
        "epoch_resets": 0,
        "slot_frames": 256,
        "n_slots": 8,
        "detach_reason": "none",
    }
    ring.update(overrides)
    return ring


def test_link_counters_read_iface_and_snmp_fields(tmp_path, monkeypatch) -> None:
    wireless = tmp_path / "wireless"
    wireless.write_text(
        "Inter-| sta-|   Quality        |   Discarded packets               "
        "| Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc "
        "| beacon | 22\n"
        " wlan0: 0000   61.  -49.  -256        0      0      0      2      0"
        "        0\n",
        encoding="utf-8",
    )
    snmp = tmp_path / "snmp"
    snmp.write_text(
        "Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors\n"
        "Udp: 123 4 0 100 7 0\n"
        "Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens "
        "AttemptFails EstabResets CurrEstab InSegs OutSegs\n"
        "Tcp: 1 200 120000 -1 10 5 0 0 2 5000 4800\n",
        encoding="utf-8",
    )
    (tmp_path / "wlan0_rx_bytes").write_text("1000\n", encoding="utf-8")

    monkeypatch.setattr(airplay_health, "PROC_NET_WIRELESS_PATH", str(wireless))
    monkeypatch.setattr(airplay_health, "PROC_NET_SNMP_PATH", str(snmp))
    monkeypatch.setattr(
        airplay_health,
        "SYS_CLASS_NET_RX_BYTES_TMPL",
        str(tmp_path / "{iface}_rx_bytes"),
    )

    counters = airplay_health._read_link_counters()

    assert counters == {
        "iface": "wlan0",
        "rx_bytes": 1000,
        "udp_in_datagrams": 123,
        "udp_rcvbuf_errors": 7,
        "tcp_in_segs": 5000,
    }


def test_link_counters_absent_proc_files_yield_none_not_zero(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        airplay_health, "PROC_NET_WIRELESS_PATH", str(tmp_path / "no-wireless"),
    )
    monkeypatch.setattr(
        airplay_health, "PROC_NET_SNMP_PATH", str(tmp_path / "no-snmp"),
    )
    monkeypatch.setattr(
        airplay_health,
        "SYS_CLASS_NET_RX_BYTES_TMPL",
        str(tmp_path / "{iface}_rx_bytes"),
    )

    counters = airplay_health._read_link_counters()

    assert counters == {
        "iface": None,
        "rx_bytes": None,
        "udp_in_datagrams": None,
        "udp_rcvbuf_errors": None,
        "tcp_in_segs": None,
    }


def test_link_probe_counter_deltas_become_per_second_rates() -> None:
    counters = [
        {
            "iface": "wlan0", "rx_bytes": 1000, "udp_in_datagrams": 100,
            "udp_rcvbuf_errors": 0, "tcp_in_segs": 50,
        },
        {
            "iface": "wlan0", "rx_bytes": 6000, "udp_in_datagrams": 600,
            "udp_rcvbuf_errors": 2, "tcp_in_segs": 300,
        },
    ]
    now = [1000.0]
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(),
        link_probe=lambda: counters.pop(0),
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()

    link = sampler.snapshot()["current"]["link"]
    assert link["rx_bytes_per_sec"] == 1000.0
    assert link["udp_in_datagrams_per_sec"] == 100.0
    assert link["udp_rcvbuf_errors_delta"] == 2
    assert link["tcp_in_segs_per_sec"] == 50.0


def test_link_baseline_resets_on_ring_epoch_change() -> None:
    now = [1000.0]
    statuses = [
        _fanin_status(selected_input="airplay", airplay_frames=0, ring=_ring()),
        _fanin_status(selected_input="airplay", airplay_frames=5000, ring=_ring()),
        _fanin_status(
            selected_input="airplay", airplay_frames=5000,
            ring=_ring(epoch_resets=1),
        ),
    ]
    rx_bytes = iter([10000, 15000, 15000])
    sampler = _sampler(
        fanin_probe=lambda: statuses.pop(0),
        link_probe=lambda: {
            "iface": "wlan0", "rx_bytes": next(rx_bytes),
            "udp_in_datagrams": 0, "udp_rcvbuf_errors": 0, "tcp_in_segs": 0,
        },
        time_fn=lambda: now[0],
    )

    sampler._tick()
    now[0] += 5.0
    sampler._tick()
    assert (
        sampler.snapshot()["current"]["link"]["rx_bytes_per_sec_baseline"] == 1000.0
    )

    now[0] += 5.0
    sampler._tick()
    assert sampler.snapshot()["current"]["link"]["rx_bytes_per_sec_baseline"] is None


def test_receiver_is_none_when_pid_comm_does_not_match_shairport() -> None:
    # writer_pid can outlive shairport-sync (SIGKILL) and be recycled by an
    # unrelated process; comm must gate before any counter is trusted.
    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(selected_input="airplay", ring=_ring()),
        receiver_probe=lambda pid: {
            "pid": pid, "comm": "some-recycled-proc",
            "state": "S", "majflt": 5, "cpu_ticks": 100,
        },
        time_fn=lambda: 1000.0,
    )

    sampler._tick()

    assert sampler.snapshot()["current"]["link"]["receiver"] is None
