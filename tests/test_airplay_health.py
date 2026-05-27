from __future__ import annotations

import json
import sys
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer

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

    underrun = classify_journal_line(
        "jasper-camilla",
        "PB: Prepare playback after buffer underrun",
    )
    assert underrun is not None
    assert underrun["type"] == "camilla_playback_underrun"
    assert underrun["severity"] == "issue"


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

    sampler = AirPlayHealthSampler(
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


def test_camilla_short_reads_are_watch_when_the_audio_path_recovers() -> None:
    now = [2000.0]

    def journal(unit: str, _since: float, _now: float) -> list[str]:
        if unit == "jasper-camilla":
            return [
                "Capture read 768 frames instead of the requested 1024",
                "Capture read 960 frames instead of the requested 1024",
            ]
        return []

    sampler = AirPlayHealthSampler(
        fanin_probe=lambda: _fanin_status(),
        journal_reader=journal,
        mpris_probe=lambda: {"playing": False},
        camilla_probe=lambda: None,
        time_fn=lambda: now[0],
    )

    sampler._tick()
    snap = sampler.snapshot()

    assert snap["status"] == "watch"
    assert snap["summary_5m"]["camilla_short_reads"] == 2
    assert snap["summary_5m"]["camilla_playback_underruns"] == 0


def test_fanin_input_buffer_regression_is_issue() -> None:
    now = [3000.0]
    sampler = AirPlayHealthSampler(
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
    sampler = AirPlayHealthSampler(
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
    sampler = AirPlayHealthSampler(
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
