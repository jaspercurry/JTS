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
from pathlib import Path

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
    UdpMicReader,
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


def test_udp_mic_reader_bind_conflict_raises_guided_unavailable():
    # A live wake-corpus session (or anything) already bound to raw0's UDP
    # port must surface as the harness's own guided "mic unavailable" error,
    # not a raw EADDRINUSE traceback — and the caller's socket must be closed
    # so we don't leak an fd on the failed construction.
    import socket

    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    held_port = holder.getsockname()[1]
    try:
        with pytest.raises(MicSourceUnavailableError, match="could not bind"):
            UdpMicReader(port=held_port, timeout_seconds=0.5)
    finally:
        holder.close()


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

    result = harness.capture_mic_detections("udp:9879", duration_seconds=0.01)
    detections = result.detections

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

    result = harness.capture_mic_detections(
        "udp:9879",
        duration_seconds=1.0,
        refractory_ms=refractory_ms,
    )

    assert len(result.detections) == 2


def test_capture_mic_detections_propagates_source_unavailable(monkeypatch):
    class _DeadReader:
        def read_chunk(self):
            raise MicSourceUnavailableError("nothing feeding :9879")

        def close(self):
            pass

    monkeypatch.setattr(harness, "build_mic_reader", lambda spec: _DeadReader())

    with pytest.raises(MicSourceUnavailableError):
        harness.capture_mic_detections("udp:9879", duration_seconds=0.01)


def test_capture_mic_detections_flags_early_stop_after_at_least_one_chunk(monkeypatch):
    # A source that delivers a chunk then goes quiet BEFORE the deadline must
    # record stopped_early=True (not just silently end): a promotion capture
    # that died at minute 12 of 36 can't look like a quiet success.
    reader = _FakeMicReader([_impulse_chunk(1_000_000_000, 0)])
    monkeypatch.setattr(harness, "build_mic_reader", lambda spec: reader)

    # A long requested window the single chunk can't fill -> early stop.
    result = harness.capture_mic_detections("udp:9879", duration_seconds=60.0)

    assert result.stopped_early is True
    assert result.elapsed_seconds < result.requested_seconds
    assert len(result.detections) == 1


def test_cmd_capture_warns_loudly_when_mic_stops_early(tmp_path, monkeypatch, capsys):
    schedule = harness.click_track.build_schedule("quick", seed=1)
    schedule_path = tmp_path / "schedule.json"
    harness.click_track.write_schedule_json(schedule, schedule_path)

    # Stub the resolved tap (no real daemon) and the health snapshot. The CLI
    # builds its arm/disarm client through build_resolved_tap now, so stub that
    # seam rather than the concrete client class.
    class _StubTapClient:
        def arm(self, _params):
            return {"ok": True, "armed": True}

        def disarm(self):
            return {"ok": True, "armed": False}

    monkeypatch.setattr(
        harness,
        "build_resolved_tap",
        lambda **_kwargs: harness.ResolvedTap(
            transport="usbsink",
            client=_StubTapClient(),
            tap_path="/run/jasper-usbsink/impulse-tap.jsonl",
            reason="stubbed for test",
        ),
    )
    monkeypatch.setattr(harness, "snapshot_route_health", lambda: {})
    monkeypatch.setattr(
        harness,
        "capture_mic_detections",
        lambda *a, **k: harness.MicCaptureResult(
            detections=(MicDetection(monotonic_ns=1, peak=0.5),),
            stopped_early=True,
            elapsed_seconds=12.0,
            requested_seconds=schedule.duration_seconds,
        ),
    )

    rc = harness.main(["capture", str(schedule_path), "--out-dir", str(tmp_path)])

    assert rc == 0
    err = capsys.readouterr().err
    assert "mic source went quiet at t=12.0s" in err
    assert "TRUNCATED" in err


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

    result = harness.capture_mic_detections(
        "udp:9879", duration_seconds=duration_seconds, refractory_ms=0.0,
    )
    detections = result.detections

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


def test_latency_ms_for_match_is_the_raw_tap_to_mic_delta():
    from jasper.route_latency.pairing import MatchedImpulse

    tap = TapEvent(monotonic_ns=0, frame_index=0, ring_fill_frames=0, peak=0.8)
    mic = MicDetection(monotonic_ns=30_000_000, peak=0.5)  # 30ms later
    match = MatchedImpulse(tap=tap, mic=mic)

    latency = harness.latency_ms_for_match(match, mic_distance_compensation_ms=0.0)

    assert latency == pytest.approx(30.0)


def test_latency_ms_for_match_does_not_add_ring_fill_dwell():
    # Regression guard for the ring-dwell double-count fix. The tap
    # timestamps the click at ingress (before it enters the ring), so the
    # ring dwell is already inside the t_mic - t_tap subtraction. Two matches
    # with an identical raw delta but very different ring_fill_frames must
    # yield the SAME latency — the fill is diagnostic context, not an
    # additive latency term.
    from jasper.route_latency.pairing import MatchedImpulse

    mic = MicDetection(monotonic_ns=30_000_000, peak=0.5)
    empty_ring = MatchedImpulse(
        tap=TapEvent(monotonic_ns=0, frame_index=0, ring_fill_frames=0, peak=0.8),
        mic=mic,
    )
    full_ring = MatchedImpulse(
        tap=TapEvent(monotonic_ns=0, frame_index=0, ring_fill_frames=768, peak=0.8),
        mic=mic,
    )

    a = harness.latency_ms_for_match(empty_ring, mic_distance_compensation_ms=0.0)
    b = harness.latency_ms_for_match(full_ring, mic_distance_compensation_ms=0.0)

    assert a == pytest.approx(30.0)
    assert b == pytest.approx(30.0)


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


def _clean_usbsink_counters() -> dict:
    return {"capture_xruns": 0, "playback_xruns": 0, "underflow_periods": 0, "overflow_events": 0, "dropped_periods": 0}


def test_diff_route_health_negative_known_delta_means_restart_not_clean():
    # S1: a NEGATIVE delta on a per-process monotonic counter can only mean the
    # daemon restarted mid-window (counter reset to 0). A restart is an unclean
    # window by definition — it must NOT justify the declaration, even though
    # "fewer xruns after" superficially looks cleaner.
    before = {
        "usbsink": {"counters": {**_clean_usbsink_counters(), "capture_xruns": 7}},
        "fanin": {"ok": True},
        "outputd": {"ok": True},
    }
    after = {
        "usbsink": {"counters": _clean_usbsink_counters()},  # reset to 0 -> -7
        "fanin": {"ok": True},
        "outputd": {"ok": True},
    }

    report = harness.diff_route_health(before, after)

    assert report.known_counter_deltas["usbsink.counters.capture_xruns"] == -7.0
    assert report.would_justify_route_health_ok is False


def test_diff_route_health_fanin_output_xrun_would_not_justify_ok():
    # S2: a new fan-in OUTPUT xrun is on the route's own path — the HANDOFF
    # clean-window contract names "no outputd/fan-in xruns" explicitly, so it
    # must disqualify even though nothing on the usbsink surface moved.
    before = {
        "usbsink": {"counters": _clean_usbsink_counters()},
        "fanin": {"output": {"xrun_count": 0}},
        "outputd": {"ok": True},
    }
    after = {
        "usbsink": {"counters": _clean_usbsink_counters()},
        "fanin": {"output": {"xrun_count": 4}},
        "outputd": {"ok": True},
    }

    report = harness.diff_route_health(before, after)

    assert report.known_counter_deltas["fanin.output.xrun_count"] == 4.0
    assert report.would_justify_route_health_ok is False


def test_diff_route_health_outputd_content_and_dac_xruns_would_not_justify_ok():
    # S2: outputd content-capture and final-DAC xruns are both on the route.
    for surface_after in (
        {"content": {"xrun_count": 1}, "dac": {"xrun_count": 0}},
        {"content": {"xrun_count": 0}, "dac": {"xrun_count": 2}},
    ):
        before = {
            "usbsink": {"counters": _clean_usbsink_counters()},
            "fanin": {"ok": True},
            "outputd": {"content": {"xrun_count": 0}, "dac": {"xrun_count": 0}},
        }
        after = {
            "usbsink": {"counters": _clean_usbsink_counters()},
            "fanin": {"ok": True},
            "outputd": surface_after,
        }

        report = harness.diff_route_health(before, after)

        assert report.would_justify_route_health_ok is False


def test_diff_route_health_per_lane_resampler_unlock_would_not_justify_ok():
    # S2: the fan-in USB resampler unlock/silence/overrun counters live inside
    # the `inputs` ARRAY, matched by dotted-path suffix regardless of lane
    # index. A resampler unlock (or silence/overrun) on ANY lane disqualifies.
    def snapshot(unlock: int, silence: int, overrun: int, lane_xrun: int) -> dict:
        return {
            "usbsink": {"counters": _clean_usbsink_counters()},
            "fanin": {
                "inputs": [
                    {"label": "spotify", "xrun_count": 0},
                    {
                        "label": "usb",
                        "xrun_count": lane_xrun,
                        "resampler": {
                            "unlock_count": unlock,
                            "silence_frames": silence,
                            "overrun_frames": overrun,
                        },
                    },
                ],
                "output": {"xrun_count": 0},
            },
            "outputd": {"content": {"xrun_count": 0}, "dac": {"xrun_count": 0}},
        }

    # A clean window (no per-lane movement) still justifies.
    clean = harness.diff_route_health(snapshot(0, 0, 0, 0), snapshot(0, 0, 0, 0))
    assert clean.would_justify_route_health_ok is True

    # A resampler unlock on the USB lane disqualifies, and the array-indexed
    # path is what gets flagged.
    report = harness.diff_route_health(snapshot(0, 0, 0, 0), snapshot(1, 0, 0, 0))
    assert report.known_counter_deltas["fanin.inputs.1.resampler.unlock_count"] == 1.0
    assert report.would_justify_route_health_ok is False

    # Silence / overrun / per-lane xrun each independently disqualify.
    assert harness.diff_route_health(snapshot(0, 0, 0, 0), snapshot(0, 256, 0, 0)).would_justify_route_health_ok is False
    assert harness.diff_route_health(snapshot(0, 0, 0, 0), snapshot(0, 0, 128, 0)).would_justify_route_health_ok is False
    assert harness.diff_route_health(snapshot(0, 0, 0, 0), snapshot(0, 0, 0, 3)).would_justify_route_health_ok is False


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


def test_cli_generate_unwritable_out_dir_fails_gracefully(tmp_path, capsys):
    # `generate` needs no root; run unprivileged against an unwritable out-dir
    # (a file where a dir is expected here — same OSError class as the
    # root-owned default /var/lib/jasper path). It must return 1 with a guided
    # message pointing at --out-dir, never a raw traceback.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")

    rc = harness.main(["generate", "quick", "--out-dir", str(blocker / "sub"), "--seed", "1"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "--out-dir" in err
    assert "could not write" in err


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_cli_analyze_end_to_end_writes_artifact_compatible_samples(tmp_path, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    # Non-zero ring_fill_frames on every tap event proves it is NOT added to
    # the latency (the ring-dwell double-count fix): the raw tap→mic delta is
    # 30ms and the emitted samples are exactly 30ms regardless of the fill.
    _write_jsonl(
        tap_path,
        [
            {"monotonic_ns": i * 1_500_000_000, "frame_index": i * 256, "ring_fill_frames": 512, "peak": 0.8}
            for i in range(n)
        ],
    )
    _write_jsonl(
        mic_path,
        [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)],
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
    # 30ms raw tap→mic delta; ring_fill_frames=512 is diagnostic only.
    assert all(v == pytest.approx(30.0) for v in values)
    out = capsys.readouterr().out
    assert "match_rate=100.0%" in out
    assert "certifiable_percentiles=[95]" in out


def test_warn_if_tap_count_far_below_schedule_fires_only_on_large_shortfall(capsys):
    # S3: the schedule-vs-detected sanity check. Unknown expected count → no-op;
    # a small deficit within the floor → no warning; a large shortfall → warn.
    assert (
        harness._warn_if_tap_count_far_below_schedule(
            detected_tap_events=50, expected_impulse_count=None, min_tap_detect_rate=0.90
        )
        is False
    )
    assert (
        harness._warn_if_tap_count_far_below_schedule(
            detected_tap_events=95, expected_impulse_count=100, min_tap_detect_rate=0.90
        )
        is False
    )
    fired = harness._warn_if_tap_count_far_below_schedule(
        detected_tap_events=40, expected_impulse_count=100, min_tap_detect_rate=0.90
    )
    assert fired is True
    err = capsys.readouterr().err
    assert "TRUNCATED" in err
    assert "40" in err and "100" in err


def test_cli_analyze_warns_on_tap_side_truncation_but_still_emits(tmp_path, capsys):
    # A truncated tap window keeps match-rate high (every tap event pairs), so
    # match-rate alone would certify a half-length run. The schedule count check
    # is the catch: analyze warns loudly, naming the truncation, while the
    # samples file still writes (the artifact CLI's duration/percentile gates
    # remain the hard authority).
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200  # every one pairs -> match_rate 100%
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i * 256, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(
        mic_path,
        [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)],
    )

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
        "--expected-impulse-count", "1000",  # schedule planned 1000, tap saw 200
    ])

    assert rc == 0
    out = capsys.readouterr()
    assert "match_rate=100.0%" in out.out  # match-rate is blind to truncation
    assert "TRUNCATED" in out.err  # ...but the schedule count check catches it
    assert (tmp_path / "latency-samples.json").exists()


def test_cli_analyze_no_truncation_warning_when_count_matches(tmp_path, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i * 256, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(
        mic_path,
        [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)],
    )

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
        "--expected-impulse-count", "200",
    ])

    assert rc == 0
    assert "TRUNCATED" not in capsys.readouterr().err


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


def test_health_report_excludes_timestamp_noise_leaves(tmp_path, capsys):
    # captured_at_monotonic_ns / last_progress_epoch_ms / updated_at always
    # change between snapshots and are not health counters — they must not
    # appear in the printed deltas, or they bury the counters that matter.
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
                "before": {
                    "captured_at_monotonic_ns": 1_000,
                    "usbsink": {"counters": {"capture_xruns": 0}, "last_progress_epoch_ms": 100},
                },
                "after": {
                    "captured_at_monotonic_ns": 999_999_999,
                    "usbsink": {"counters": {"capture_xruns": 1}, "last_progress_epoch_ms": 5_000_000},
                },
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
    # The real counter delta is printed...
    assert "usbsink.counters.capture_xruns: +1" in out
    # ...but the timestamp leaves are not.
    assert "captured_at_monotonic_ns" not in out
    assert "last_progress_epoch_ms" not in out


def test_analyze_tolerates_malformed_route_health_snapshot(tmp_path, capsys):
    # A corrupt snapshot must not crash analyze — the samples are still valid.
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(mic_path, [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)])
    health_path = tmp_path / "route-health-snapshot.json"
    health_path.write_text("{not valid json", encoding="utf-8")

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--route-health-snapshot", str(health_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
    ])

    assert rc == 0
    assert (tmp_path / "latency-samples.json").exists()
    err = capsys.readouterr().err
    assert "could not read route-health snapshot" in err


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
    # The artifact CLI is resolved to a real path (sibling of sys.executable,
    # then PATH) so it works under sudo where the venv bin dir isn't on PATH;
    # the invoked command's basename is always the artifact CLI name.
    assert Path(captured_cmd[0]).name == harness.ARTIFACT_CLI_NAME
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


def test_resolve_artifact_cli_prefers_sibling_of_sys_executable(tmp_path, monkeypatch):
    # The documented invocation runs the harness from a venv bin dir that is
    # NOT on sudo's PATH; the artifact CLI is a sibling console entry point, so
    # resolution must find it next to sys.executable first.
    sibling = tmp_path / harness.ARTIFACT_CLI_NAME
    sibling.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(harness.sys, "executable", str(tmp_path / "python"))

    assert harness._resolve_artifact_cli() == str(sibling)


def test_resolve_artifact_cli_falls_back_to_bare_name_when_absent(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(harness.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(harness.sys, "argv", [str(tmp_path / "harness")])
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert harness._resolve_artifact_cli() == harness.ARTIFACT_CLI_NAME


def test_cli_invoke_artifact_reports_clean_error_when_artifact_missing(tmp_path, monkeypatch, capsys):
    tap_path = tmp_path / "tap.jsonl"
    mic_path = tmp_path / "mic.jsonl"
    n = 200
    _write_jsonl(
        tap_path,
        [{"monotonic_ns": i * 1_500_000_000, "frame_index": i, "ring_fill_frames": 0, "peak": 0.8} for i in range(n)],
    )
    _write_jsonl(mic_path, [{"monotonic_ns": i * 1_500_000_000 + 30_000_000, "peak": 0.5} for i in range(n)])

    def _raise_not_found(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(harness.subprocess, "run", _raise_not_found)

    rc = harness.main([
        "analyze",
        "--tap-events", str(tap_path),
        "--mic-detections", str(mic_path),
        "--out-dir", str(tmp_path),
        "--duration-seconds", "300",
        "--invoke-artifact",
    ])

    # The samples file is still written; only the passthrough failed, and it
    # fails with a clean message, not an uncaught traceback.
    assert rc == 1
    assert (tmp_path / "latency-samples.json").exists()
    err = capsys.readouterr().err
    assert harness.ARTIFACT_CLI_NAME in err
    assert "could not find" in err


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
