# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jasper.control.airplay_health as airplay_health
from jasper.control.airplay_health import (
    AirPlayHealthSampler,
    classify_journal_line,
)
from jasper.control.server import _make_handler


def _fanin_status(
    *,
    airplay_frames: int = 0,
    airplay_xruns: int = 0,
    output_frames: int = 0,
    output_xruns: int = 0,
    input_buffer_frames: int = 4096,
    output_buffer_frames: int = 3072,
    progress_age_ms: int = 0,
) -> dict:
    return {
        "input_buffer_frames": input_buffer_frames,
        "selected_input": None,
        "inputs": [
            {
                "label": "airplay",
                "pcm": "hw:Loopback,1,1",
                "frames_read": airplay_frames,
                "xrun_count": airplay_xruns,
            },
        ],
        "output": {
            "pcm": "hw:Loopback,0,7",
            "sample_rate": 48000,
            "period_frames": 256,
            "buffer_frames": output_buffer_frames,
            "frames_written": output_frames,
            "xrun_count": output_xruns,
        },
        "watchdog": {
            "pings_sent": 10,
            "pings_skipped": 0,
            "last_progress_age_ms": progress_age_ms,
        },
    }


def _sampler(**kwargs) -> AirPlayHealthSampler:
    """Build a sampler isolated from live Pi maintenance markers.

    Warmup + connect-grace default OFF here so the classification tests
    below exercise steady-state behaviour at small clock values; the
    warmup / connect-grace suppression has its own dedicated tests.
    """
    kwargs.setdefault("maintenance_suppress_path", None)
    kwargs.setdefault("warmup_sec", 0.0)
    kwargs.setdefault("connect_grace_sec", 0.0)
    return AirPlayHealthSampler(**kwargs)


def _patch_home_assistant(monkeypatch) -> None:
    async def fake_ha_probe() -> dict:
        return {
            "configured": False,
            "connected": False,
            "url": "",
            "instance_name": None,
            "version": None,
            "error": None,
        }

    monkeypatch.setitem(
        sys.modules,
        "jasper.home_assistant",
        types.SimpleNamespace(probe_status_from_env=fake_ha_probe),
    )


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

    short = classify_journal_line(
        "jasper-camilla",
        "Capture read 768 frames instead of the requested 1024",
    )
    assert short is not None
    assert short["type"] == "camilla_short_read"
    assert short["severity"] == "watch"
    assert short["deficit_frames"] == 256

    underrun = classify_journal_line(
        "jasper-camilla",
        "PB: Prepare playback after buffer underrun",
    )
    assert underrun is not None
    assert underrun["type"] == "camilla_playback_underrun"
    assert underrun["severity"] == "issue"


def test_offset_too_short_warning_rolls_into_shairport_events() -> None:
    """The bonded-leader tight-regime warning must affect the AirPlay-health
    status, not just sit in the raw event list — i.e. it has to roll into the
    `shairport_events` bucket like its siblings (shairport_oos /
    shairport_broken_pipe). Without the EVENT_BUCKET_FIELD mapping the event
    would be invisible to `_status_locked`'s 30 m verdict."""
    too_short = (
        "The stream latency (0.300000 seconds) it too short to accommodate an "
        "offset of 0.550000 seconds and a backend buffer of 0.100000 seconds."
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
    verdict), with the audio path otherwise healthy."""
    now = [2000.0]

    def journal(unit: str, _since: float, _now: float) -> list[str]:
        if unit == "shairport-sync":
            return [
                "The stream latency (0.300000 seconds) it too short to accommodate "
                "an offset of 1.050000 seconds and a backend buffer of 0.500000 "
                "seconds."
            ]
        return []

    sampler = _sampler(
        fanin_probe=lambda: _fanin_status(),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )
    sampler._tick()
    snap = sampler.snapshot()
    assert snap["status"] == "watch"
    assert snap["summary_30m"]["shairport_events"] >= 1


def test_tiny_camilla_short_reads_are_ignored_as_recovered_partials() -> None:
    assert (
        classify_journal_line(
            "jasper-camilla",
            "Capture read 1023 frames instead of the requested 1024",
        )
        is None
    )
    assert (
        classify_journal_line(
            "jasper-camilla",
            "Capture read 1016 frames instead of the requested 1024",
        )
        is None
    )

    material = classify_journal_line(
        "jasper-camilla",
        "Capture read 1008 frames instead of the requested 1024",
    )

    assert material is not None
    assert material["type"] == "camilla_short_read"
    assert material["deficit_frames"] == 16


def test_fanin_xrun_delta_surfaces_issue_without_recounting_baseline() -> None:
    now = [1000.0]
    statuses = [
        _fanin_status(
            airplay_frames=0,
            airplay_xruns=7,
            output_frames=0,
            output_xruns=1,
        ),
        _fanin_status(
            airplay_frames=240000,
            airplay_xruns=8,
            output_frames=240000,
            output_xruns=1,
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
    assert snap["summary_5m"]["fanin_output_xruns"] == 0
    assert snap["current"]["fanin"]["airplay"]["frames_per_sec"] == 48000.0
    assert snap["events"][-1]["type"] == "fanin_airplay_xrun"


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
    journal_calls: list[tuple[str, float, float]] = []

    def journal(unit: str, since: float, until: float) -> list[str]:
        journal_calls.append((unit, since, until))
        if unit == "shairport-sync":
            return ["recovering from a previous underrun"]
        return []

    sampler = AirPlayHealthSampler(
        fanin_probe=lambda: statuses.pop(0),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
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
    shairport_calls = [
        call for call in journal_calls if call[0] == "shairport-sync"
    ]
    assert shairport_calls[0][1] == 1005.0


def test_camilla_short_reads_are_watch_while_actively_streaming() -> None:
    # While AirPlay IS streaming, recoverable Camilla short reads are a
    # non-fatal warning (watch), not a hard issue.
    now = [2000.0]
    frames = [0, 240000]

    def journal(unit: str, _since: float, _now: float) -> list[str]:
        if unit == "jasper-camilla":
            return [
                "Capture read 768 frames instead of the requested 1024",
                "Capture read 960 frames instead of the requested 1024",
            ]
        return []

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

    def journal(unit: str, _since: float, _now: float) -> list[str]:
        if unit == "jasper-camilla":
            return ["Capture read 586 frames instead of the requested 1024"]
        return []

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
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="one\ntwo\n")

    monkeypatch.setattr(airplay_health.subprocess, "run", fake_run)

    lines = AirPlayHealthSampler._read_journal_lines(
        "shairport-sync",
        10.1234,
        40.5678,
    )

    assert lines == ["one", "two"]
    assert calls
    args = calls[0]
    assert args[args.index("--since") + 1] == "@10.123"
    assert args[args.index("--until") + 1] == "@40.568"


def test_default_fanin_status_timeout_allows_state_server_poll_delay() -> None:
    # macOS caps AF_UNIX sun_path at 104 bytes; pytest's tmp_path nests
    # ~123 bytes deep and overflows it (Linux allows 108 with a shorter
    # CI tmp base, so this only bit on macOS). Bind under a short /tmp dir
    # instead — matches the socket-path convention in test_control_server.py.
    sock_dir = tempfile.mkdtemp(dir="/tmp")
    socket_path = Path(sock_dir) / "control.sock"
    ready = threading.Event()

    def serve_once() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            time.sleep(0.35)
            conn, _addr = server.accept()
            with conn:
                assert conn.recv(1024) == b"STATUS\n"
                conn.sendall(b'{"ok": true}\n')

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    try:
        assert ready.wait(timeout=1.0)

        assert AirPlayHealthSampler._read_fanin_status(str(socket_path)) == {
            "ok": True,
        }
        thread.join(timeout=1.0)
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_system_snapshot_endpoint_includes_airplay_health(monkeypatch) -> None:
    _patch_home_assistant(monkeypatch)

    class FakeAirPlay:
        def snapshot(self) -> dict:
            return {"status": "ok", "reason": "clean"}

    handler = _make_handler(
        "127.0.0.1",
        1234,
        "/nonexistent.sock",
        sampler=None,
        airplay_health_sampler=FakeAirPlay(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/system/snapshot", timeout=2) as r:
            assert r.status == 200
            body = json.loads(r.read().decode("utf-8"))
        assert body["metrics"] is None
        assert body["airplay_health"] == {"status": "ok", "reason": "clean"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_system_snapshot_endpoint_fails_soft_when_airplay_snapshot_raises(
    monkeypatch,
) -> None:
    _patch_home_assistant(monkeypatch)

    class BrokenAirPlay:
        def snapshot(self) -> dict:
            raise RuntimeError("boom")

    handler = _make_handler(
        "127.0.0.1",
        1234,
        "/nonexistent.sock",
        sampler=None,
        airplay_health_sampler=BrokenAirPlay(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/system/snapshot", timeout=2) as r:
            assert r.status == 200
            body = json.loads(r.read().decode("utf-8"))
        assert body["airplay_health"]["status"] == "unknown"
        assert "failed" in body["airplay_health"]["reason"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
        fanin_probe=lambda: statuses.pop(0),
        journal_reader=lambda u, _s, _n: (
            ["recovering from a previous underrun"]
            if u == "shairport-sync" else []
        ),
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
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


def test_airplay_connect_grace_suppresses_session_establish() -> None:
    # When a sender connects, the PTP-anchor settle emits expected
    # sync-correction bursts; a per-session grace keeps them off the
    # dashboard, but a sync error AFTER the grace still surfaces.
    now = [5000.0]
    fanin = {"v": _fanin_status(airplay_frames=0, airplay_xruns=0)}
    mpris = {"playing": False}
    journal = {"shairport-sync": []}

    sampler = AirPlayHealthSampler(
        fanin_probe=lambda: fanin["v"],
        journal_reader=lambda u, _s, _n: list(journal.get(u, [])),
        mpris_probe=lambda: dict(mpris),
        camilla_probe=lambda: None,
        maintenance_suppress_path=None,
        warmup_sec=0.0,
        connect_grace_sec=45.0,
        time_fn=lambda: now[0],
    )

    sampler._tick()            # idle baseline, not active
    assert sampler.snapshot()["suppressed_reason"] is None

    # Sender connects: frames flow -> idle->active transition arms grace.
    now[0] += 5.0
    fanin["v"] = _fanin_status(airplay_frames=240000, airplay_xruns=0)
    mpris["playing"] = True
    journal["shairport-sync"] = ["rtp.c sync: Large negative sync error"]
    sampler._tick()

    snap = sampler.snapshot()
    assert snap["suppressed_reason"] == "airplay_connect"
    assert snap["summary_5m"]["shairport_sync_errors"] == 0
    assert snap["events"] == []

    # Grace expires; the same establish-class event would now be real.
    now[0] += 50.0
    sampler._tick()

    snap = sampler.snapshot()
    assert snap["suppressed_reason"] is None
    assert snap["summary_5m"]["shairport_sync_errors"] >= 1
    assert snap["status"] == "issue"
