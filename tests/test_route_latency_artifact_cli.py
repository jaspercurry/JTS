# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import hashlib

from jasper.audio_runtime_plan import build_audio_runtime_plan
from jasper.cli import route_latency_artifact


def _usb_plan():
    return build_audio_runtime_plan(
        base_env={"JASPER_AUDIO_ROUTE_PROFILE": "usb_low_latency_48k"},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
            "JASPER_OUTPUTD_PERIOD_FRAMES": "128",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "256",
        },
        fanin_env={
            "JASPER_FANIN_INPUT_BUFFER_FRAMES": "4096",
            "JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "1024",
        },
        profile_id="apple_usb_c_dongle",
        route_mode="solo",
    )


def _usb_plan_with_legacy_transport_error():
    # The deferred lab `rate_match` outputd bridge (a partial flip without a
    # matching shm_ring coupling) makes the USB low-latency plan error.
    return build_audio_runtime_plan(
        base_env={"JASPER_AUDIO_ROUTE_PROFILE": "usb_low_latency_48k"},
        outputd_env={"JASPER_OUTPUTD_CONTENT_BRIDGE": "rate_match"},
        profile_id="apple_usb_c_dongle",
        route_mode="solo",
    )


def test_nearest_rank_percentile_uses_latency_tail():
    samples = [1, 2, 3, 4, 100]

    assert route_latency_artifact.nearest_rank_percentile(samples, 0.95) == 100
    assert route_latency_artifact.nearest_rank_percentile(samples, 95) == 100


def test_metrics_from_samples_computes_percentiles():
    samples = list(range(1, 201))

    metrics = route_latency_artifact.metrics_from_samples(
        samples,
        duration_seconds=5 * 60,
    )

    assert metrics.sample_count == 200
    assert metrics.p95_ms == 190
    assert metrics.p99_ms == 198


def test_build_artifact_binds_live_route_identity(monkeypatch):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )
    metrics = route_latency_artifact.metrics_from_aggregates(
        p95_ms=39.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
    )

    artifact = route_latency_artifact.build_route_latency_artifact_from_metrics(
        metrics,
        impulse_spacing_jittered=True,
        route_health_ok=True,
    )

    assert artifact.status == "pass"
    assert artifact.dac_id == "apple_usb_c_dongle"
    identity = artifact.checks["identity"]
    assert identity["route_id"] == "usb_low_latency_48k"
    assert identity["source_id"] == "usbsink"
    assert identity["dac_profile_id"] == "apple_usb_c_dongle"
    assert identity["rust_bridge_config"] == {
        "implementation": "rust",
        "latency_hint": "low",
        "output_mode": "aloop",
        "period_frames": 256,
        "ring_periods": 3,
    }
    assert artifact.checks["evidence"] == {"input_kind": "aggregate_metrics"}


def test_artifact_fails_without_clean_route_health(monkeypatch):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )
    metrics = route_latency_artifact.metrics_from_aggregates(
        p95_ms=39.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
    )

    artifact = route_latency_artifact.build_route_latency_artifact_from_metrics(
        metrics,
        impulse_spacing_jittered=True,
        route_health_ok=False,
    )

    assert artifact.status == "fail"
    assert "route_health_anomaly" in artifact.checks["issues"]


def test_build_artifact_refuses_runtime_plan_errors(monkeypatch):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan_with_legacy_transport_error,
    )
    metrics = route_latency_artifact.metrics_from_aggregates(
        p95_ms=39.0,
        p99_ms=41.0,
        sample_count=1000,
        duration_seconds=30 * 60,
    )

    try:
        route_latency_artifact.build_route_latency_artifact_from_metrics(
            metrics,
            impulse_spacing_jittered=True,
            route_health_ok=True,
        )
    except ValueError as e:
        assert "requires JASPER_OUTPUTD_CONTENT_BRIDGE=direct" in str(e)
    else:  # pragma: no cover - explicit assertion message
        raise AssertionError("runtime-plan errors did not block artifact build")


def test_main_writes_quick_validation_warn_artifact(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )
    monkeypatch.setattr(
        route_latency_artifact,
        "_route_live_state_issues_for_current_route",
        lambda: (),
    )
    samples_path = tmp_path / "samples.json"
    samples_path.write_text(json.dumps([30.0] * 200), encoding="utf-8")

    rc = route_latency_artifact.main([
        "--samples",
        str(samples_path),
        "--duration-seconds",
        str(5 * 60),
        "--route-health-ok",
        "--directory",
        str(tmp_path),
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "status=warn" in out
    assert "certified=[95]" in out
    artifacts = sorted(p.name for p in tmp_path.glob("*.json"))
    assert "latest.json" in artifacts
    assert any("__route_latency__" in name for name in artifacts)


def test_main_route_health_ok_fails_when_live_state_mismatches(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )
    monkeypatch.setattr(
        route_latency_artifact,
        "_route_live_state_issues_for_current_route",
        lambda: ("live_fanin_resampler_unlocked:usbsink",),
    )

    rc = route_latency_artifact.main([
        "--p95-ms",
        "39",
        "--p99-ms",
        "59",
        "--sample-count",
        "1000",
        "--duration-seconds",
        str(30 * 60),
        "--impulse-spacing-jittered",
        "--route-health-ok",
        "--harness-id",
        "unit-test-harness",
        "--require-pass",
        "--directory",
        str(tmp_path),
    ])

    assert rc == 1
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "fail"
    assert "route_health_anomaly" in latest["checks"]["issues"]
    assert "live_fanin_resampler_unlocked:usbsink" in latest["checks"]["issues"]


def test_main_require_pass_rejects_missing_p99(monkeypatch, tmp_path):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )
    monkeypatch.setattr(
        route_latency_artifact,
        "_route_live_state_issues_for_current_route",
        lambda: (),
    )

    rc = route_latency_artifact.main([
        "--p95-ms",
        "39",
        "--sample-count",
        "200",
        "--duration-seconds",
        str(5 * 60),
        "--route-health-ok",
        "--harness-id",
        "unit-test-harness",
        "--require-pass",
        "--report-only",
    ])

    assert rc == 1


def test_main_records_raw_sample_file_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )
    monkeypatch.setattr(
        route_latency_artifact,
        "_route_live_state_issues_for_current_route",
        lambda: (),
    )
    body = json.dumps({"latencies_ms": [30.0] * 200})
    samples_path = tmp_path / "samples.json"
    samples_path.write_text(body, encoding="utf-8")

    rc = route_latency_artifact.main([
        "--samples",
        str(samples_path),
        "--duration-seconds",
        str(5 * 60),
        "--route-health-ok",
        "--harness-id",
        "jts-click-capture",
        "--harness-version",
        "abc123",
        "--measurement-id",
        "run-42",
        "--directory",
        str(tmp_path),
    ])

    assert rc == 0
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    evidence = latest["checks"]["evidence"]
    assert evidence["input_kind"] == "raw_samples"
    assert evidence["sample_source"] == str(samples_path)
    assert evidence["sample_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert evidence["sample_bytes"] == len(body.encode("utf-8"))
    assert evidence["harness_id"] == "jts-click-capture"
    assert evidence["harness_version"] == "abc123"
    assert evidence["measurement_id"] == "run-42"


def test_main_rejects_anonymous_aggregate_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr(
        route_latency_artifact,
        "build_audio_runtime_plan_from_system",
        _usb_plan,
    )

    try:
        route_latency_artifact.main([
            "--p95-ms",
            "39",
            "--sample-count",
            "200",
            "--duration-seconds",
            str(5 * 60),
            "--route-health-ok",
            "--directory",
            str(tmp_path),
        ])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - explicit assertion message
        raise AssertionError("anonymous aggregate metrics were accepted")
