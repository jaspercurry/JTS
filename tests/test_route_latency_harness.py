# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Integration coverage for the click/capture route-latency harness CLI.

These tests exercise the real production code paths (never a duplicated
re-implementation): `capture_mic_detections` with an injected fake mic
reader, `analyze_matches`/`latency_ms_for_match` against synthetic tap +
mic evidence with known ground-truth latencies, the samples-JSON writer
checked against `jasper.cli.route_latency_artifact`'s REAL parser (imported
directly, never duplicated), and the CLI's argparse wiring end-to-end via
`main()`.
"""
from __future__ import annotations

import json
import struct

import pytest

from jasper import wake_legs
from jasper.audio_validation import percentile_min_samples
from jasper.cli import route_latency_artifact
from jasper.cli import route_latency_harness as harness
from jasper.route_latency.mic_readers import (
    RAW0_BYTES_PER_PACKET,
    RAW0_SAMPLE_RATE_HZ,
    RAW0_SAMPLES_PER_PACKET,
    RAW0_UDP_PORT,
    MicChunk,
    MicSourceUnavailableError,
)
from jasper.route_latency.pairing import MicDetection, TapEvent


# --------------------------------------------------------------------------
# Wire-format cross-checks — pin the duplicated raw0 constants in
# mic_readers.py against the single source of truth in jasper.wake_legs, so
# a future frozen-token/port change fails loudly here rather than silently
# desyncing the harness's default mic source.
# --------------------------------------------------------------------------


def test_raw0_wire_constants_match_wake_legs_registry():
    raw0 = wake_legs.by_token("raw0")
    assert raw0.udp_port == RAW0_UDP_PORT
    assert raw0.wake_input is False, "raw0 must stay corpus-only, never a wake-detection leg"


def test_raw0_packet_size_matches_1280_samples_16k_mono_s16le():
    assert RAW0_SAMPLES_PER_PACKET == 1280
    assert RAW0_SAMPLE_RATE_HZ == 16_000
    assert RAW0_BYTES_PER_PACKET == RAW0_SAMPLES_PER_PACKET * 2


# --------------------------------------------------------------------------
# capture_mic_detections: fake mic reader, known offsets in, exact
# latencies (well, exact event times) out.
# --------------------------------------------------------------------------


class _FakeMicReader:
    """A scripted MicReader: replays a fixed sequence of chunks, then
    raises MicSourceUnavailableError once exhausted (mirrors a real
    reader's behavior once the capture window nominally ends)."""

    def __init__(self, chunks: list[MicChunk]) -> None:
        self._chunks = list(chunks)
        self._closed = False

    def read_chunk(self) -> MicChunk:
        if not self._chunks:
            raise MicSourceUnavailableError("fake reader exhausted")
        return self._chunks.pop(0)

    def close(self) -> None:
        self._closed = True


def _silent_chunk(arrival_ns: int, n: int = RAW0_SAMPLES_PER_PACKET) -> MicChunk:
    payload = struct.pack(f"<{n}h", *([0] * n))
    return MicChunk(samples=payload, arrival_monotonic_ns=arrival_ns, sample_rate_hz=RAW0_SAMPLE_RATE_HZ)


def _impulse_chunk(arrival_ns: int, sample_offset: int, peak: int = 20000, n: int = RAW0_SAMPLES_PER_PACKET) -> MicChunk:
    values = [0] * n
    values[sample_offset] = peak
    payload = struct.pack(f"<{n}h", *values)
    return MicChunk(samples=payload, arrival_monotonic_ns=arrival_ns, sample_rate_hz=RAW0_SAMPLE_RATE_HZ)


def test_capture_mic_detections_reanchors_to_packet_arrival_time(monkeypatch):
    # One packet, one impulse at sample offset 500 of 1280, arriving at a
    # known monotonic timestamp. The detection's event time must be
    # derived from THIS packet's arrival time minus the remaining-samples
    # offset, matching the pinned formula exactly.
    arrival_ns = 5_000_000_000
    offset = 500
    chunk = _impulse_chunk(arrival_ns, offset)
    reader = _FakeMicReader([chunk])
    monkeypatch.setattr(harness, "build_mic_reader", lambda spec: reader)

    detections = harness.capture_mic_detections("udp:9879", duration_seconds=0.01)

    assert len(detections) == 1
    remaining = RAW0_SAMPLES_PER_PACKET - offset
    expected_ns = arrival_ns - round(remaining * 1_000_000_000 / RAW0_SAMPLE_RATE_HZ)
    assert detections[0].monotonic_ns == expected_ns
    assert reader._closed is True


def test_capture_mic_detections_across_multiple_chunks_preserves_detector_state(monkeypatch):
    # Detector state (refractory) must persist across chunk boundaries —
    # exactly like a real UDP stream delivers one packet at a time. The
    # refractory window is sample-count-based (matching the pinned Rust tap
    # contract, which has no notion of "wall-clock gap" either — it only
    # ever sees a continuous ALSA stream), so the silent packets in
    # between must actually cover the refractory duration in SAMPLES, at
    # the real raw0 packet cadence (80ms of audio per packet) — a
    # timestamp gap alone, without the corresponding silent samples having
    # been fed, would not be a realistic production scenario.
    refractory_ms = 250.0
    packet_ms = 1000.0 * RAW0_SAMPLES_PER_PACKET / RAW0_SAMPLE_RATE_HZ  # 80ms
    silent_packets_needed = int(refractory_ms // packet_ms) + 1
    arrival_ns = 1_000_000_000
    chunks = [_impulse_chunk(arrival_ns, 0)]
    for _ in range(silent_packets_needed):
        arrival_ns += int(packet_ms * 1_000_000)
        chunks.append(_silent_chunk(arrival_ns))
    arrival_ns += int(packet_ms * 1_000_000)
    chunks.append(_impulse_chunk(arrival_ns, 0))  # refractory has cleared by now

    reader = _FakeMicReader(list(chunks))
    monkeypatch.setattr(harness, "build_mic_reader", lambda spec: reader)

    detections = harness.capture_mic_detections(
        "udp:9879",
        duration_seconds=1.0,
        refractory_ms=refractory_ms,
    )

    assert len(detections) == 2


def test_capture_mic_detections_propagates_source_unavailable(monkeypatch):
    class _DeadReader:
        def read_chunk(self):
            raise MicSourceUnavailableError("nothing feeding :9879")

        def close(self):
            pass

    monkeypatch.setattr(harness, "build_mic_reader", lambda spec: _DeadReader())

    with pytest.raises(MicSourceUnavailableError):
        harness.capture_mic_detections("udp:9879", duration_seconds=0.01)


# --------------------------------------------------------------------------
# Clock-drift injection: per-impulse re-anchoring must NOT accumulate the
# ~180ms error a naive single stream-start anchor would over a 30-min
# promotion-scale run at 100ppm drift (the exact figure cited in the
# pinned architecture brief).
# --------------------------------------------------------------------------


def test_per_impulse_reanchoring_eliminates_100ppm_drift_over_promotion_window(monkeypatch):
    drift_ppm = 100.0
    nominal_rate = float(RAW0_SAMPLE_RATE_HZ)
    actual_rate = nominal_rate * (1 + drift_ppm * 1e-6)
    duration_seconds = 1800.0  # promotion-scale window

    n_packets = int(duration_seconds * actual_rate / RAW0_SAMPLES_PER_PACKET)
    # Place one impulse near the END of the run, where a naive
    # stream-start anchor's error is at its maximum.
    impulse_packet_index = n_packets - 1

    chunks: list[MicChunk] = []
    for i in range(n_packets):
        # REAL arrival time uses the drifted (actual) rate — this is what
        # a real UDP reader's time.monotonic_ns() would observe.
        real_arrival_ns = int((i + 1) * RAW0_SAMPLES_PER_PACKET / actual_rate * 1e9)
        if i == impulse_packet_index:
            chunks.append(_impulse_chunk(real_arrival_ns, sample_offset=0))
        else:
            chunks.append(_silent_chunk(real_arrival_ns))

    reader = _FakeMicReader(chunks)
    monkeypatch.setattr(harness, "build_mic_reader", lambda spec: reader)

    detections = harness.capture_mic_detections(
        "udp:9879", duration_seconds=duration_seconds, refractory_ms=0.0,
    )

    assert len(detections) == 1
    # Ground truth: the impulse sample sits at offset 0 of its packet, i.e.
    # exactly one packet's duration (in REAL, drifted samples) before that
    # packet's arrival timestamp. Re-anchoring uses THIS packet's own
    # arrival_ns for the subtraction, so it should match near-exactly —
    # bounded only by rounding, not by any accumulated stream-length drift.
    packet_real_arrival_ns = int(
        (impulse_packet_index + 1) * RAW0_SAMPLES_PER_PACKET / actual_rate * 1e9
    )
    expected_real_ns = packet_real_arrival_ns - round(
        RAW0_SAMPLES_PER_PACKET * 1e9 / RAW0_SAMPLE_RATE_HZ
    )
    reanchor_error_ms = abs(detections[0].monotonic_ns - expected_real_ns) / 1e6
    assert reanchor_error_ms < 1.0

    # Contrast: a NAIVE stream-start anchor (event_ns = 0 +
    # cumulative_samples / nominal_rate) would have accumulated the full
    # drift by this point in the run — demonstrating why re-anchoring is
    # required, not just a nice-to-have.
    naive_cumulative_samples = (impulse_packet_index + 1) * RAW0_SAMPLES_PER_PACKET
    naive_event_ns = int(naive_cumulative_samples / nominal_rate * 1e9)
    naive_error_ms = abs(naive_event_ns - expected_real_ns) / 1e6
    assert naive_error_ms > 150.0, (
        f"expected the naive anchor to have drifted >150ms by the end of a "
        f"{duration_seconds:g}s run at {drift_ppm:g}ppm, got {naive_error_ms:.2f}ms"
    )
    assert reanchor_error_ms < naive_error_ms / 100.0


# --------------------------------------------------------------------------
# Latency math + samples-JSON: exact values in, exact values out; schema
# checked against route_latency_artifact's REAL parser.
# --------------------------------------------------------------------------


def test_latency_ms_for_match_combines_delta_ring_fill_and_distance():
    from jasper.route_latency.pairing import MatchedImpulse

    tap = TapEvent(monotonic_ns=0, frame_index=0, ring_fill_frames=480, peak=0.8)
    mic = MicDetection(monotonic_ns=30_000_000, peak=0.5)  # 30ms later
    match = MatchedImpulse(tap=tap, mic=mic)

    latency = harness.latency_ms_for_match(match, mic_distance_compensation_ms=0.0)

    # 30ms raw + 480/48.0 = 10ms ring dwell = 40ms
    assert latency == pytest.approx(40.0)


def test_latency_ms_for_match_subtracts_distance_compensation():
    from jasper.route_latency.pairing import MatchedImpulse

    tap = TapEvent(monotonic_ns=0, frame_index=0, ring_fill_frames=0, peak=0.8)
    mic = MicDetection(monotonic_ns=30_000_000, peak=0.5)
    match = MatchedImpulse(tap=tap, mic=mic)

    latency = harness.latency_ms_for_match(match, mic_distance_compensation_ms=2.9)

    assert latency == pytest.approx(27.1)


def test_analyze_matches_produces_expected_latencies_and_match_rate():
    taps = [
        TapEvent(monotonic_ns=i * 1_000_000_000, frame_index=i, ring_fill_frames=0, peak=0.8)
        for i in range(10)
    ]
    mics = [
        MicDetection(monotonic_ns=i * 1_000_000_000 + 32_000_000, peak=0.5) for i in range(10)
    ]

    result = harness.analyze_matches(taps, mics)

    assert len(result.latencies_ms) == 10
    assert all(v == pytest.approx(32.0) for v in result.latencies_ms)
    assert result.pairing.match_rate == 1.0
    assert result.match_rate_ok is True


def test_analyze_matches_respects_match_rate_floor():
    taps = [
        TapEvent(monotonic_ns=i * 1_000_000_000, frame_index=i, ring_fill_frames=0, peak=0.8)
        for i in range(10)
    ]
    # Only 3 of 10 get a mic response.
    mics = [MicDetection(monotonic_ns=i * 1_000_000_000 + 32_000_000, peak=0.5) for i in range(3)]

    result = harness.analyze_matches(taps, mics, match_rate_floor=0.9)

    assert result.pairing.match_rate == pytest.approx(0.3)
    assert result.match_rate_ok is False


def test_write_samples_json_matches_route_latency_artifact_bare_list_schema(tmp_path):
    latencies = (30.1, 31.4, 29.8, 30.0)

    path = harness.write_samples_json(latencies, tmp_path / "samples.json")

    # Written as the bare-list shape (option 1 of the three the artifact
    # accepts) — round-trip through the REAL artifact parser, not a
    # reimplementation.
    parsed = route_latency_artifact.load_latency_samples(path)
    assert parsed == pytest.approx(list(latencies))


def test_write_samples_json_survives_a_full_promotion_scale_round_trip(tmp_path):
    # >=1000 samples (the p99 certification floor) round-tripping through
    # the real artifact parser without truncation/precision loss beyond
    # float round-trip tolerance.
    n = percentile_min_samples(99)
    latencies = tuple(30.0 + (i % 7) * 0.5 for i in range(n))

    path = harness.write_samples_json(latencies, tmp_path / "samples.json")
    parsed = route_latency_artifact.load_latency_samples(path)

    assert len(parsed) == n
    assert parsed == pytest.approx(list(latencies))


def test_summarize_latencies_computes_percentiles_and_bounds():
    latencies = tuple(float(v) for v in range(1, 101))  # 1..100

    summary = harness.summarize_latencies(latencies)

    assert summary["count"] == 100
    assert summary["min_ms"] == 1.0
    assert summary["max_ms"] == 100.0
    assert summary["p50_ms"] == 50.0
    assert summary["p95_ms"] == 95.0
    assert summary["p99_ms"] == 99.0


def test_summarize_latencies_empty_does_not_crash():
    summary = harness.summarize_latencies(())

    assert summary["count"] == 0
    assert summary["p50_ms"] is None
    assert summary["min_ms"] is None


# --------------------------------------------------------------------------
# Route-health honesty: never auto-asserts route_health_ok.
# --------------------------------------------------------------------------


def test_diff_route_health_clean_snapshot_would_justify_ok():
    counters = {"capture_xruns": 0, "playback_xruns": 0, "underflow_periods": 0, "overflow_events": 0, "dropped_periods": 0}
    before = {"usbsink": {"counters": dict(counters)}, "fanin": {"ok": True}, "outputd": {"ok": True}}
    after = {"usbsink": {"counters": dict(counters)}, "fanin": {"ok": True}, "outputd": {"ok": True}}

    report = harness.diff_route_health(before, after)

    assert report.would_justify_route_health_ok is True
    assert all(v == 0.0 for v in report.known_counter_deltas.values())


def test_diff_route_health_new_xruns_would_not_justify_ok():
    before = {
        "usbsink": {"counters": {"capture_xruns": 2, "playback_xruns": 0, "underflow_periods": 0, "overflow_events": 0, "dropped_periods": 0}},
        "fanin": {"ok": True},
        "outputd": {"ok": True},
    }
    after = {
        "usbsink": {"counters": {"capture_xruns": 5, "playback_xruns": 0, "underflow_periods": 0, "overflow_events": 0, "dropped_periods": 0}},
        "fanin": {"ok": True},
        "outputd": {"ok": True},
    }

    report = harness.diff_route_health(before, after)

    assert report.would_justify_route_health_ok is False
    assert report.known_counter_deltas["usbsink.counters.capture_xruns"] == 3.0


def test_diff_route_health_unreachable_daemon_never_justifies_ok():
    before = {"usbsink": None, "fanin": {"ok": True}, "outputd": {"ok": True}}
    after = {"usbsink": None, "fanin": {"ok": True}, "outputd": {"ok": True}}

    report = harness.diff_route_health(before, after)

    assert report.would_justify_route_health_ok is False


def test_diff_route_health_reports_all_nonzero_deltas_generically():
    # A counter this module doesn't specifically curate in
    # KNOWN_HEALTH_COUNTER_PATHS must still show up in all_deltas — the
    # honesty report is a generic diff, not limited to the curated subset.
    before = {"fanin": {"custom_new_counter": 1}}
    after = {"fanin": {"custom_new_counter": 4}}

    report = harness.diff_route_health(before, after)

    assert report.all_deltas["fanin.custom_new_counter"] == 3.0


# --------------------------------------------------------------------------
# CLI end-to-end (main()) — generate, analyze (with real evidence files),
# match-rate refusal, --invoke-artifact passthrough.
# --------------------------------------------------------------------------


def test_cli_generate_writes_wav_and_schedule(tmp_path, capsys):
    rc = harness.main(["generate", "quick", "--out-dir", str(tmp_path), "--seed", "1"])

    assert rc == 0
    assert (tmp_path / "quick-click-track.wav").exists()
    assert (tmp_path / "quick-schedule.json").exists()
    out = capsys.readouterr().out
    assert "impulses=240" in out


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_cli_analyze_end_to_end_writes_artifact_compatible_samples(tmp_path, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [
            {"monotonic_ns": i * 1_500_000_000, "frame_index": i * 256, "ring_fill_frames": 96, "peak": 0.8}
            for i in range(n)
        ],
    )
    _write_jsonl(
        mic_path,
        [{"monotonic_ns": i * 1_500_000_000 + 28_000_000, "peak": 0.5} for i in range(n)],
    )

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
    ])

    assert rc == 0
    samples_path = tmp_path / "latency-samples.json"
    assert samples_path.exists()
    values = route_latency_artifact.load_latency_samples(samples_path)
    assert len(values) == n
    # 28ms raw + 96/48.0=2ms ring dwell = 30ms
    assert all(v == pytest.approx(30.0) for v in values)
    out = capsys.readouterr().out
    assert "match_rate=100.0%" in out
    assert "certifiable_percentiles=[95]" in out


def test_cli_analyze_refuses_below_match_rate_floor_and_writes_nothing(tmp_path, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_000_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(10)],
    )
    _write_jsonl(
        mic_path,
        [{"monotonic_ns": i * 1_000_000_000 + 30_000_000, "peak": 0.5} for i in range(3)],
    )

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
    ])

    assert rc == 1
    assert not (tmp_path / "latency-samples.json").exists()


def test_cli_analyze_errors_cleanly_on_missing_tap_events(tmp_path, capsys):
    mic_path = tmp_path / "mic.jsonl"
    _write_jsonl(mic_path, [{"monotonic_ns": 30_000_000, "peak": 0.5}])

    rc = harness.main([
        "analyze",
        "--tap-events", str(tmp_path / "does-not-exist.jsonl"),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
    ])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no tap events found" in err


def test_cli_analyze_errors_cleanly_on_missing_mic_detections(tmp_path, capsys):
    tap_path = tmp_path / "tap.jsonl"
    _write_jsonl(tap_path, [{"monotonic_ns": 0, "frame_index": 0, "ring_fill_frames": 0, "peak": 0.8}])

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(tmp_path / "does-not-exist.jsonl"),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
    ])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no mic detections found" in err


def test_cli_analyze_prints_route_health_and_never_auto_confirms(tmp_path, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(mic_path, [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)])
    health_path = tmp_path / "route-health-snapshot.json"
    health_path.write_text(
        json.dumps(
            {
                "before": {"usbsink": {"counters": {"capture_xruns": 0}}},
                "after": {"usbsink": {"counters": {"capture_xruns": 2}}},
            }
        ),
        encoding="utf-8",
    )

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--route-health-snapshot", str(health_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "would NOT be justified" in out
    assert "usbsink.counters.capture_xruns: +2" in out


def test_cli_invoke_artifact_shells_out_with_harness_id_and_duration(tmp_path, monkeypatch, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(mic_path, [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)])

    captured_cmd: list[str] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(harness.subprocess, "run", _fake_run)

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
        "--invoke-artifact",
        "--measurement-id", "run-7",
    ])

    assert rc == 0
    assert "jasper-route-latency-artifact" in captured_cmd
    assert "--harness-id" in captured_cmd
    assert captured_cmd[captured_cmd.index("--harness-id") + 1] == harness.HARNESS_ID
    assert "--duration-seconds" in captured_cmd
    assert captured_cmd[captured_cmd.index("--duration-seconds") + 1] == "300.0"
    assert "--measurement-id" in captured_cmd
    assert captured_cmd[captured_cmd.index("--measurement-id") + 1] == "run-7"
    # route-health-ok is never auto-passed without --confirm-route-health-ok
    assert "--route-health-ok" not in captured_cmd


def test_cli_invoke_artifact_passes_route_health_ok_only_with_explicit_confirm(tmp_path, monkeypatch):
    # The positive case: --route-health-ok reaches the artifact CLI only
    # when BOTH the harness's own health-diff verdict says it would be
    # justified AND the operator explicitly passed --confirm-route-health-ok
    # (see RouteHealthReport.would_justify_route_health_ok's docstring for
    # why this is never inferred alone).
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(mic_path, [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)])
    health_path = tmp_path / "route-health-snapshot.json"
    clean_counters = {"capture_xruns": 0, "playback_xruns": 0, "underflow_periods": 0, "overflow_events": 0, "dropped_periods": 0}
    health_path.write_text(
        json.dumps(
            {
                "before": {"usbsink": {"counters": dict(clean_counters)}, "fanin": {}, "outputd": {}},
                "after": {"usbsink": {"counters": dict(clean_counters)}, "fanin": {}, "outputd": {}},
            }
        ),
        encoding="utf-8",
    )

    captured_cmd: list[str] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(harness.subprocess, "run", _fake_run)

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--route-health-snapshot", str(health_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
        "--invoke-artifact",
        "--confirm-route-health-ok",
    ])

    assert rc == 0
    assert "--route-health-ok" in captured_cmd


def test_cli_run_subcommand_does_not_accept_duration_or_jitter_flags(tmp_path):
    # Regression guard: `run` loads the schedule file directly and derives
    # duration/jitteredness from it. It must NOT accept --duration-seconds
    # or --impulse-spacing-jittered as separate flags — an earlier version
    # of this CLI required --duration-seconds on `run` and then silently
    # discarded whatever the operator typed in favor of the schedule's own
    # value, which is confusing and error-prone. Those flags exist only on
    # `analyze` (which has no schedule file to read them from).
    schedule_path = tmp_path / "schedule.json"
    schedule = harness.click_track.build_schedule("quick", seed=1)
    harness.click_track.write_schedule_json(schedule, schedule_path)

    with pytest.raises(SystemExit) as exc_info:
        harness.main(["run", str(schedule_path), "--duration-seconds", "300"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        harness.main(["run", str(schedule_path), "--impulse-spacing-jittered"])
    assert exc_info.value.code == 2


def test_cli_mic_distance_cm_converts_to_compensation_ms(tmp_path):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(200)],
    )
    _write_jsonl(mic_path, [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(200)])

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
        "--mic-distance-cm", "10",
    ])

    assert rc == 0
    values = route_latency_artifact.load_latency_samples(tmp_path / "latency-samples.json")
    # 10cm ~= 0.29ms of compensation subtracted from the raw 30ms latency.
    assert all(v == pytest.approx(30.0 - harness.SOUND_MS_PER_CM * 10, abs=0.01) for v in values)


@pytest.mark.parametrize("subcommand", ["generate", "arm", "disarm", "capture", "analyze", "run"])
def test_cli_help_does_not_raise_for_every_subcommand(subcommand):
    # Regression guard: argparse treats a literal '%' in a help= string as
    # a format specifier and raises ValueError at parser-build time if it
    # isn't a valid %(name)s substitution. Every subcommand's --help must
    # build and print cleanly.
    with pytest.raises(SystemExit) as exc_info:
        harness.main([subcommand, "--help"])
    assert exc_info.value.code == 0
