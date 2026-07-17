# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jasper.cli import usb_mic_latency_artifact as artifact_cli
from jasper.cli.usb_mic_latency_artifact import (
    build_usb_mic_latency_artifact,
)


def _snapshot(
    index: int,
    *,
    p50: float,
    p95: float,
    p99: float,
    host_streaming: bool = True,
) -> dict:
    return {
        "schema_version": 4,
        "updated_epoch_sec": 100.0 + index,
        "sample_observed_epoch_sec": 100.1 + index,
        "host_streaming": host_streaming,
        "host_progress_age_ms": 0.0,
        "last_host_progress_epoch_sec": 100.0 + index,
        "source_age_basis": "bridge_emit_monotonic_v2",
        "source_age_scope": "bridge_emit_to_alsa_write",
        "source_age_window_generation": 3,
        "source_age_ms_p50": p50,
        "source_age_ms_p95": p95,
        "source_age_ms_p99": p99,
        "source_age_sample_count": 512,
        "source_age_samples_appended": 1000 + (index * 50),
        "writer_target_ms": 20.0,
        "writer_pcm_rate_hz": 16_000,
        "writer_pcm_period_frames": 160,
        "writer_pcm_buffer_frames": 640,
        "gadget_hardware_rate_hz": 48_000,
        "writer_splices": 2,
        "writer_xruns": 0,
        "writer_resets": 0,
        "packets_lost": 0,
        "sequence_resets": 0,
        "sequence_reorders": 0,
        "sequence_discontinuities": 0,
        "periods_dropped_streaming": 7,
        "relay_pid": 456,
        "relay_started_epoch_sec": 90.0,
    }


def _bridge_stats(
    *,
    input_latency_frames: int = 1280,
    production_chip_aec: bool = True,
) -> dict:
    return {
        "schema_version": 3,
        "pid": 123,
        "started_epoch_sec": 80.0,
        "counters": {"usb_mic_source_fallback_frames": 0},
        "capture_stream": {
            "sample_rate_hz": 16_000,
            "block_frames": 320,
            "input_latency_frames": input_latency_frames,
            "input_latency_seconds": input_latency_frames / 16_000,
        },
        "active_capture_plan": {
            "enabled_corpus_flags": {
                "production_chip_aec": production_chip_aec,
            },
            "beam_plan": {"primary_leg": "chip_aec_150"},
        },
    }


def _artifact(
    snapshots: list[dict],
    *,
    bridge_stats: dict | None = None,
    bridge_stats_before: dict | None = None,
) -> dict:
    bridge_after = bridge_stats or _bridge_stats()
    return build_usb_mic_latency_artifact(
        snapshots,
        build_manifest=(
            "JASPER_GIT_SHA_FULL=abc1234567890def\nJASPER_GIT_BRANCH=codex/test\n"
        ),
        bcd_device="0x0210",
        bridge_stats_before=bridge_stats_before or bridge_after,
        bridge_stats=bridge_after,
        host_os="macOS 15",
        host_app="CoreAudio sounddevice",
        validated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def test_artifact_binds_identity_and_aggregates_known_percentiles() -> None:
    artifact = _artifact(
        [
            _snapshot(index, p50=index, p95=10 + index, p99=20 + index)
            for index in range(16)
        ]
    )

    assert artifact["metrics"]["p50_ms"] == 13
    assert artifact["metrics"]["p95_ms"] == 25
    assert artifact["metrics"]["p99_ms"] == 35
    assert artifact["metrics"]["duration_seconds"] == 15.0
    assert artifact["metrics"]["sample_ticks_aggregated"] == 4
    configuration = artifact["identity"]["configuration"]
    assert configuration["build_sha"] == "abc1234567890def"
    assert configuration["selected_source"] == {
        "selection": "",
        "mode": "chip_aec",
        "leg": "chip_aec_150",
        "fallback_active": False,
    }
    assert configuration["capture_geometry"]["input_latency_frames"] == 1280
    assert configuration["writer_geometry"]["buffer_frames"] == 640
    assert artifact["identity"]["provenance"]["relay_runtime"] == {
        "pid": 456,
        "started_epoch_sec": 90.0,
    }
    assert artifact["identity"]["provenance"]["bridge_runtime"] == {
        "pid": 123,
        "started_epoch_sec": 80.0,
    }
    assert artifact["identity"]["provenance"]["observation_window"] == {
        "started_epoch_sec": 100.1,
        "ended_epoch_sec": 115.1,
    }
    assert len(artifact["identity_sha256"]) == 64
    assert len(artifact["configuration_sha256"]) == 64
    assert len(artifact["content_sha256"]) == 64
    assert artifact["counters"]["writer_splices"] == {
        "run_delta": 0,
        "end_total": 2,
    }


def test_artifact_identity_hash_changes_with_negotiated_geometry() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]

    baseline = _artifact(snapshots)
    changed = _artifact(snapshots, bridge_stats=_bridge_stats(input_latency_frames=640))

    assert baseline["identity_sha256"] != changed["identity_sha256"]
    assert baseline["configuration_sha256"] != changed["configuration_sha256"]


def test_run_identity_changes_but_configuration_hash_stays_stable() -> None:
    first_window = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    second_window = [dict(snapshot) for snapshot in first_window]
    for snapshot in second_window:
        snapshot["updated_epoch_sec"] += 100
        snapshot["sample_observed_epoch_sec"] += 100
        snapshot["last_host_progress_epoch_sec"] += 100

    first = _artifact(first_window)
    second = _artifact(second_window)

    assert first["configuration_sha256"] == second["configuration_sha256"]
    assert first["identity_sha256"] != second["identity_sha256"]


def test_artifact_rejects_any_idle_sample() -> None:
    snapshots = [
        _snapshot(
            index,
            p50=10,
            p95=20,
            p99=30,
            host_streaming=index != 3,
        )
        for index in range(16)
    ]

    with pytest.raises(ValueError, match="not actively pulling"):
        _artifact(snapshots)


def test_artifact_rejects_status_gap_or_relay_restart() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    snapshots[3]["sample_observed_epoch_sec"] += 2.0
    with pytest.raises(ValueError, match="stale|gap"):
        _artifact(snapshots)

    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    snapshots[8]["relay_pid"] = 999
    with pytest.raises(ValueError, match="relay restarted"):
        _artifact(snapshots)


def test_artifact_coalesces_repeated_status_generation() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    repeated = dict(snapshots[5])
    repeated["sample_observed_epoch_sec"] += 0.65
    snapshots.insert(6, repeated)

    artifact = _artifact(snapshots)

    assert artifact["status"] == "pass"
    assert artifact["metrics"]["sample_ticks_total"] == 16


def test_artifact_checks_freshness_at_latest_duplicate_observation() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    repeated = dict(snapshots[5])
    repeated["sample_observed_epoch_sec"] = repeated["updated_epoch_sec"] + 1.3
    for index in range(6, len(snapshots)):
        snapshots[index]["sample_observed_epoch_sec"] += 0.3
    snapshots.insert(6, repeated)

    with pytest.raises(ValueError, match="relay status was stale"):
        _artifact(snapshots)


def test_artifact_requires_minimum_unique_status_ticks_after_coalescing() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    repeated = dict(snapshots[-2])
    repeated["sample_observed_epoch_sec"] = snapshots[-1]["sample_observed_epoch_sec"]
    snapshots[-1] = repeated

    with pytest.raises(ValueError, match="16 unique relay status ticks"):
        _artifact(snapshots)


def test_artifact_rejects_status_epoch_regression() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    snapshots[6]["updated_epoch_sec"] = snapshots[5]["updated_epoch_sec"] - 0.1

    with pytest.raises(ValueError, match="moved backwards"):
        _artifact(snapshots)


@pytest.mark.parametrize(
    "counter",
    (
        "writer_xruns",
        "writer_resets",
        "sequence_resets",
        "sequence_reorders",
        "sequence_discontinuities",
    ),
)
def test_artifact_requires_schema_and_counters(counter: str) -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    snapshots[2].pop(counter)

    with pytest.raises(ValueError, match=rf"{counter} is required"):
        _artifact(snapshots)


@pytest.mark.parametrize(
    "counter",
    (
        "writer_resets",
        "sequence_resets",
        "sequence_reorders",
        "sequence_discontinuities",
    ),
)
def test_artifact_warns_on_in_window_reliability_counter(counter: str) -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    for snapshot in snapshots:
        snapshot[counter] = 7
    for snapshot in snapshots[8:]:
        snapshot[counter] = 8

    artifact = _artifact(snapshots)

    assert artifact["status"] == "warn"
    assert artifact["counters"][counter] == {"run_delta": 1, "end_total": 8}


@pytest.mark.parametrize(
    "counter",
    (
        "writer_resets",
        "sequence_resets",
        "sequence_reorders",
        "sequence_discontinuities",
    ),
)
def test_artifact_allows_historical_reliability_counter(counter: str) -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    for snapshot in snapshots:
        snapshot[counter] = 7

    artifact = _artifact(snapshots)

    assert artifact["status"] == "pass"
    assert artifact["counters"][counter] == {"run_delta": 0, "end_total": 7}


def test_artifact_warns_when_selected_source_fell_back_during_window() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    before = _bridge_stats()
    after = _bridge_stats()
    after["counters"]["usb_mic_source_fallback_frames"] = 3

    artifact = _artifact(
        snapshots,
        bridge_stats=after,
        bridge_stats_before=before,
    )

    assert artifact["status"] == "warn"
    assert artifact["counters"]["usb_mic_source_fallback_frames"] == {
        "run_delta": 3,
        "end_total": 3,
    }


def test_artifact_warns_when_requested_chip_source_resolves_to_software_clean() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    before = _bridge_stats(production_chip_aec=False)
    before["active_capture_plan"]["usb_mic_source"] = {
        "selection": "chip_aec_210",
        "mode": "software_aec3",
        "leg": "clean",
        "fallback_active": True,
    }
    after = {
        **before,
        "counters": {"usb_mic_source_fallback_frames": 3},
    }

    artifact = _artifact(
        snapshots,
        bridge_stats=after,
        bridge_stats_before=before,
    )

    assert artifact["status"] == "warn"
    assert artifact["identity"]["configuration"]["selected_source"] == {
        "selection": "chip_aec_210",
        "mode": "software_aec3",
        "leg": "clean",
        "fallback_active": True,
    }


def test_artifact_requires_source_window_turnover() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    for index, snapshot in enumerate(snapshots):
        snapshot["source_age_samples_appended"] = 1000 + index

    with pytest.raises(ValueError, match="advanced too slowly"):
        _artifact(snapshots)


def test_source_progress_uses_relay_status_cadence_not_sampler_cadence() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(25)]
    updated = 99.5
    appended = 1000
    for index, snapshot in enumerate(snapshots):
        if index:
            status_gap = (0.5, 0.5, 1.0)[(index - 1) % 3]
            updated += status_gap
            appended += round(status_gap * 50)
        snapshot["updated_epoch_sec"] = updated
        snapshot["sample_observed_epoch_sec"] = 100.1 + (index * 0.65)
        snapshot["last_host_progress_epoch_sec"] = updated
        snapshot["source_age_samples_appended"] = appended

    artifact = _artifact(snapshots)

    assert artifact["status"] == "pass"


def test_aggregation_excludes_history_from_maximum_fresh_status_age() -> None:
    snapshots = [
        _snapshot(
            index,
            p50=10,
            p95=999 if index <= 12 else 20,
            p99=1000 if index <= 12 else 30,
        )
        for index in range(17)
    ]
    for snapshot in snapshots:
        snapshot["sample_observed_epoch_sec"] = (
            snapshot["updated_epoch_sec"] + artifact_cli.MAX_STATUS_GAP_SECONDS
        )

    artifact = _artifact(snapshots)

    assert artifact["metrics"]["p95_ms"] == 20
    assert artifact["metrics"]["sample_ticks_aggregated"] == 4
    assert artifact["metrics"]["aggregation_started_epoch_sec"] == 114.25


def test_artifact_rejects_wrong_schema_or_metric_scope() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    snapshots[4]["schema_version"] = 3
    with pytest.raises(ValueError, match="schema must remain 4"):
        _artifact(snapshots)

    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    snapshots[4]["source_age_scope"] = "bridge_emit_to_relay_dequeue"
    with pytest.raises(ValueError, match="metric identity"):
        _artifact(snapshots)


def test_artifact_identifies_software_aec_source() -> None:
    snapshots = [_snapshot(index, p50=10, p95=20, p99=30) for index in range(16)]
    for snapshot in snapshots:
        snapshot["source_age_window_generation"] = 0

    artifact = _artifact(
        snapshots,
        bridge_stats=_bridge_stats(production_chip_aec=False),
    )

    assert artifact["identity"]["configuration"]["selected_source"] == {
        "selection": "",
        "mode": "software_aec3",
        "leg": "clean",
        "fallback_active": False,
    }


def test_cli_writes_artifact_and_require_pass_controls_warning_exit(
    monkeypatch,
    tmp_path,
) -> None:
    build = tmp_path / "build.txt"
    build.write_text("JASPER_GIT_SHA_FULL=abc1234567890def\n")
    bcd = tmp_path / "bcdDevice"
    bcd.write_text("0x0210\n")
    bridge_path = tmp_path / "bridge.json"
    bridge_path.write_text("{}")
    relay_path = tmp_path / "relay.json"
    relay_path.write_text("{}")

    def run(*, p95: float, require_pass: bool, output_name: str) -> int:
        before = {**_bridge_stats(), "updated_epoch_sec": 100.0}
        snapshots = [
            _snapshot(index, p50=10, p95=p95, p99=p95 + 5) for index in range(16)
        ]
        relay_stale = dict(snapshots[-1])
        relay_fenced = dict(relay_stale)
        relay_fenced.update(
            {
                "updated_epoch_sec": 115.2,
                "last_host_progress_epoch_sec": 115.2,
                "source_age_samples_appended": (
                    relay_stale["source_age_samples_appended"] + 10
                ),
            }
        )
        bridge_stale = {**_bridge_stats(), "updated_epoch_sec": 115.0}
        bridge_fenced = {**_bridge_stats(), "updated_epoch_sec": 115.3}
        reads = {"bridge": 0, "relay": 0}
        bridge_reads = iter((before, bridge_stale, bridge_fenced))
        relay_reads = iter((relay_stale, relay_fenced))

        def read_status(path):
            key = "bridge" if path == bridge_path else "relay"
            reads[key] += 1
            return next(bridge_reads if key == "bridge" else relay_reads)

        monkeypatch.setattr(artifact_cli, "_read_json_mapping", read_status)
        monkeypatch.setattr(
            artifact_cli,
            "_capture_status_window",
            lambda **_kwargs: snapshots,
        )
        monkeypatch.setattr(artifact_cli.time, "time", lambda: 115.4)
        monkeypatch.setattr(artifact_cli.time, "sleep", lambda _seconds: None)
        argv = [
            "--duration-seconds",
            "15",
            "--host-os",
            "macOS 15",
            "--host-app",
            "CoreAudio",
            "--output",
            str(tmp_path / output_name),
            "--relay-status",
            str(relay_path),
            "--bridge-stats",
            str(bridge_path),
            "--build-manifest",
            str(build),
            "--bcd-device",
            str(bcd),
        ]
        if require_pass:
            argv.append("--require-pass")
        result = artifact_cli.main(argv)
        assert reads == {"bridge": 3, "relay": 2}
        return result

    assert run(p95=30, require_pass=False, output_name="pass.json") == 0
    assert (tmp_path / "pass.json").is_file()
    assert run(p95=130, require_pass=True, output_name="warn.json") == 1
    assert (tmp_path / "warn.json").is_file()


@pytest.mark.parametrize("duration", [float("nan"), float("inf")])
def test_capture_window_rejects_nonfinite_duration(duration, tmp_path) -> None:
    with pytest.raises(ValueError, match="duration must be at least"):
        artifact_cli._capture_status_window(
            status_path=tmp_path / "unused.json",
            duration_seconds=duration,
            interval_seconds=0.65,
        )
