# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Certify one actively pulled USB microphone latency window.

The relay remains the measurement engine. This tool samples its bounded live
status, rejects idle or discontinuous host windows, and binds the result to the
installed build, active beam plan, negotiated capture/writer geometry, USB
descriptor revision, and operator-supplied host application identity.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from jasper.atomic_io import atomic_write_text
from jasper.cli.aec_bridge import BRIDGE_STATS_PATH, BRIDGE_STATS_SCHEMA_VERSION
from jasper.cli.usb_mic import SOURCE_AGE_WINDOW_PERIODS
from jasper.percentiles import nearest_rank_percentile
from jasper.usb_mic import (
    GADGET_PATH,
    RELAY_STATUS_PATH,
    USB_MIC_BCD_DEVICE,
    USB_MIC_LATENCY_WARN_MS,
    USB_MIC_RELAY_SCHEMA_VERSION,
    USB_MIC_SOURCE_AGE_BASIS,
    USB_MIC_SOURCE_AGE_SCOPE,
)

BUILD_MANIFEST_PATH = Path("/var/lib/jasper/build.txt")
DEFAULT_OUTPUT_PATH = Path("/tmp/jts-usb-mic-latency.json")
USB_MIC_LATENCY_BUDGET_MS = USB_MIC_LATENCY_WARN_MS
ARTIFACT_SCHEMA_VERSION = 1
MIN_DURATION_SECONDS = 15.0
MIN_SAMPLE_TICKS = 16
MIN_SAMPLE_INTERVAL_SECONDS = 0.55
MAX_SAMPLE_INTERVAL_SECONDS = 1.0
MAX_STATUS_GAP_SECONDS = 1.25
STATUS_FENCE_POLL_SECONDS = 0.05
ROLLING_WINDOW_WARMUP_SECONDS = 11.0
MIN_SOURCE_AGE_SAMPLES_PER_SECOND = 40.0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def _positive_int(value: Any, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_int(value: Any, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _counter_delta(
    snapshots: tuple[Mapping[str, Any], ...],
    key: str,
) -> tuple[int, int]:
    values: list[int] = []
    for snapshot in snapshots:
        if key not in snapshot:
            raise ValueError(f"{key} is required in every relay status sample")
        values.append(_nonnegative_int(snapshot[key], field=key))
    if any(current < previous for previous, current in zip(values, values[1:])):
        raise ValueError(f"{key} reset during the certification window")
    return values[-1] - values[0], values[-1]


def _build_sha(build_manifest: str) -> str:
    values: dict[str, str] = {}
    for line in build_manifest.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    build_sha = values.get("JASPER_GIT_SHA_FULL") or values.get("JASPER_GIT_SHA")
    if not build_sha:
        raise ValueError("build manifest has no JASPER_GIT_SHA_FULL/JASPER_GIT_SHA")
    return build_sha


def _selected_source(bridge_stats: Mapping[str, Any]) -> dict[str, Any]:
    active_plan = _mapping(bridge_stats.get("active_capture_plan"))
    explicit = _mapping(active_plan.get("usb_mic_source"))
    if explicit:
        mode = str(explicit.get("mode") or "").strip()
        leg = str(explicit.get("leg") or "").strip()
        if not mode or not leg:
            raise ValueError("bridge USB microphone source identity is incomplete")
        return {
            "selection": str(explicit.get("selection") or "").strip(),
            "mode": mode,
            "leg": leg,
            "fallback_active": bool(explicit.get("fallback_active")),
        }
    flags = _mapping(active_plan.get("enabled_corpus_flags"))
    if "production_chip_aec" not in flags:
        raise ValueError("bridge stats do not identify the production AEC mode")
    if not bool(flags.get("production_chip_aec")):
        return {
            "selection": "",
            "mode": "software_aec3",
            "leg": "clean",
            "fallback_active": False,
        }
    beam_plan = _mapping(active_plan.get("beam_plan"))
    primary = str(beam_plan.get("primary_leg") or "").strip()
    if not primary:
        raise ValueError("bridge stats do not identify the active primary beam leg")
    return {
        "selection": "",
        "mode": "chip_aec",
        "leg": primary,
        "fallback_active": False,
    }


def _bridge_counter(bridge_stats: Mapping[str, Any], key: str) -> int:
    counters = _mapping(bridge_stats.get("counters"))
    if key not in counters:
        raise ValueError(f"aec_bridge.counters.{key} is required")
    return _nonnegative_int(counters[key], field=f"aec_bridge.counters.{key}")


def _capture_plan_identity(bridge_stats: Mapping[str, Any]) -> dict[str, Any]:
    """Stable plan identity excluding live fallback/effective source state."""

    active = json.loads(json.dumps(_mapping(bridge_stats.get("active_capture_plan"))))
    source = active.get("usb_mic_source")
    if isinstance(source, dict):
        active["usb_mic_source"] = {
            "selection": str(source.get("selection") or "").strip(),
        }
    return active


def _capture_geometry(bridge_stats: Mapping[str, Any]) -> dict[str, Any]:
    stream = _mapping(bridge_stats.get("capture_stream"))
    return {
        "sample_rate_hz": _positive_int(
            stream.get("sample_rate_hz"), field="capture_stream.sample_rate_hz"
        ),
        "block_frames": _positive_int(
            stream.get("block_frames"), field="capture_stream.block_frames"
        ),
        "input_latency_frames": _positive_int(
            stream.get("input_latency_frames"),
            field="capture_stream.input_latency_frames",
        ),
        "input_latency_seconds": _finite_float(
            stream.get("input_latency_seconds"),
            field="capture_stream.input_latency_seconds",
        ),
    }


def _writer_geometry(snapshot: Mapping[str, Any]) -> dict[str, int]:
    return {
        "sample_rate_hz": _positive_int(
            snapshot.get("writer_pcm_rate_hz"), field="writer_pcm_rate_hz"
        ),
        "period_frames": _positive_int(
            snapshot.get("writer_pcm_period_frames"),
            field="writer_pcm_period_frames",
        ),
        "buffer_frames": _positive_int(
            snapshot.get("writer_pcm_buffer_frames"),
            field="writer_pcm_buffer_frames",
        ),
        "gadget_hardware_rate_hz": _positive_int(
            snapshot.get("gadget_hardware_rate_hz"),
            field="gadget_hardware_rate_hz",
        ),
    }


def build_usb_mic_latency_artifact(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    build_manifest: str,
    bcd_device: str,
    bridge_stats_before: Mapping[str, Any],
    bridge_stats: Mapping[str, Any],
    host_os: str,
    host_app: str,
    observation_deadline_epoch_sec: float | None = None,
    validated_at: datetime | None = None,
) -> dict[str, Any]:
    """Bind an uninterrupted active-capture status window to runtime identity."""

    raw_samples = tuple(dict(snapshot) for snapshot in snapshots)
    raw_observed = tuple(
        _finite_float(
            sample.get("sample_observed_epoch_sec"),
            field="sample_observed_epoch_sec",
        )
        for sample in raw_samples
    )
    if any(
        current <= previous for previous, current in zip(raw_observed, raw_observed[1:])
    ):
        raise ValueError("sampler gap or clock regression detected")
    if any(
        current - previous > MAX_STATUS_GAP_SECONDS
        for previous, current in zip(raw_observed, raw_observed[1:])
    ):
        raise ValueError(f"sampler gap exceeded {MAX_STATUS_GAP_SECONDS:.2f}s")
    samples_by_status_tick: list[dict[str, Any]] = []
    last_status_epoch: float | None = None
    for sample in raw_samples:
        status_epoch = _finite_float(
            sample.get("updated_epoch_sec"), field="updated_epoch_sec"
        )
        if last_status_epoch is not None and status_epoch < last_status_epoch:
            raise ValueError("relay status moved backwards during the window")
        if status_epoch == last_status_epoch:
            # Polling and status publication are independent clocks. Keep the
            # latest observation of one atomic status generation without
            # weighting its percentiles twice.
            samples_by_status_tick[-1] = sample
        else:
            samples_by_status_tick.append(sample)
        last_status_epoch = status_epoch
    samples = tuple(samples_by_status_tick)
    if len(samples) < MIN_SAMPLE_TICKS:
        raise ValueError(
            f"at least {MIN_SAMPLE_TICKS} unique relay status ticks are required"
        )
    if not host_os.strip() or not host_app.strip():
        raise ValueError("host OS and application identity are required")
    if bcd_device.strip().lower() != USB_MIC_BCD_DEVICE:
        raise ValueError(
            f"USB microphone descriptor must be {USB_MIC_BCD_DEVICE}; "
            f"observed {bcd_device.strip() or 'missing'}"
        )
    if (
        _nonnegative_int(
            bridge_stats.get("schema_version"),
            field="aec_bridge_stats.schema_version",
        )
        != BRIDGE_STATS_SCHEMA_VERSION
    ):
        raise ValueError(
            f"AEC bridge stats schema must be {BRIDGE_STATS_SCHEMA_VERSION}"
        )

    updated = tuple(
        _finite_float(sample.get("updated_epoch_sec"), field="updated_epoch_sec")
        for sample in samples
    )
    if any(current <= previous for previous, current in zip(updated, updated[1:])):
        raise ValueError("relay status did not advance throughout the window")
    observed = tuple(
        _finite_float(
            sample.get("sample_observed_epoch_sec"),
            field="sample_observed_epoch_sec",
        )
        for sample in samples
    )
    for index, (status_epoch, observed_epoch) in enumerate(zip(updated, observed)):
        age = observed_epoch - status_epoch
        if age < 0 or age > MAX_STATUS_GAP_SECONDS:
            raise ValueError(f"relay status was stale at sample {index}")
    if any(
        current - previous > MAX_STATUS_GAP_SECONDS
        for previous, current in zip(updated, updated[1:])
    ):
        raise ValueError(f"relay status gap exceeded {MAX_STATUS_GAP_SECONDS:.2f}s")
    observation_started_epoch_sec = raw_observed[0]
    observation_ended_epoch_sec = (
        raw_observed[-1]
        if observation_deadline_epoch_sec is None
        else _finite_float(
            observation_deadline_epoch_sec,
            field="observation_deadline_epoch_sec",
        )
    )
    duration_seconds = observation_ended_epoch_sec - observation_started_epoch_sec
    if duration_seconds < MIN_DURATION_SECONDS:
        raise ValueError(
            f"certification window must be at least {MIN_DURATION_SECONDS:.0f}s"
        )
    progress_epochs = tuple(
        _finite_float(
            sample.get("last_host_progress_epoch_sec"),
            field="last_host_progress_epoch_sec",
        )
        for sample in samples
    )
    if any(
        current <= previous
        for previous, current in zip(progress_epochs, progress_epochs[1:])
    ):
        raise ValueError("host hw_ptr did not advance throughout the window")
    for index, sample in enumerate(samples):
        if not bool(sample.get("host_streaming")):
            raise ValueError(f"host was not actively pulling at sample {index}")
        progress_age_ms = _finite_float(
            sample.get("host_progress_age_ms"), field="host_progress_age_ms"
        )
        if progress_age_ms > 1000.0:
            raise ValueError(f"host progress was stale at sample {index}")

    generations = {
        _nonnegative_int(
            sample.get("source_age_window_generation"),
            field="source_age_window_generation",
        )
        for sample in samples
    }
    if len(generations) != 1:
        raise ValueError("relay source-age window reset during certification")
    schemas = {
        _nonnegative_int(sample.get("schema_version"), field="schema_version")
        for sample in samples
    }
    if schemas != {USB_MIC_RELAY_SCHEMA_VERSION}:
        raise ValueError(
            "relay schema must remain "
            f"{USB_MIC_RELAY_SCHEMA_VERSION} during certification"
        )
    bases = {str(sample.get("source_age_basis") or "") for sample in samples}
    scopes = {str(sample.get("source_age_scope") or "") for sample in samples}
    if bases != {USB_MIC_SOURCE_AGE_BASIS} or scopes != {USB_MIC_SOURCE_AGE_SCOPE}:
        raise ValueError("relay source-age metric identity is unsupported or changed")
    relay_pids = {
        _positive_int(sample.get("relay_pid"), field="relay_pid") for sample in samples
    }
    relay_started = {
        _finite_float(
            sample.get("relay_started_epoch_sec"), field="relay_started_epoch_sec"
        )
        for sample in samples
    }
    if len(relay_pids) != 1 or len(relay_started) != 1:
        raise ValueError("USB microphone relay restarted during certification")
    writer_geometries = [_writer_geometry(sample) for sample in samples]
    writer_targets = {
        _finite_float(sample.get("writer_target_ms"), field="writer_target_ms")
        for sample in samples
    }
    if (
        any(geometry != writer_geometries[0] for geometry in writer_geometries[1:])
        or len(writer_targets) != 1
    ):
        raise ValueError("writer geometry or target changed during certification")

    samples_appended = tuple(
        _nonnegative_int(
            sample.get("source_age_samples_appended"),
            field="source_age_samples_appended",
        )
        for sample in samples
    )
    if any(
        current < previous
        for previous, current in zip(samples_appended, samples_appended[1:])
    ):
        raise ValueError("source-age sample counter reset during certification")
    for index in range(1, len(samples)):
        status_gap = updated[index] - updated[index - 1]
        minimum_progress = max(
            1,
            math.floor(status_gap * MIN_SOURCE_AGE_SAMPLES_PER_SECOND),
        )
        if samples_appended[index] - samples_appended[index - 1] < minimum_progress:
            raise ValueError(
                "source-age samples advanced too slowly during certification"
            )
    try:
        turnover_anchor_index = next(
            index
            for index, status_epoch in enumerate(updated)
            if status_epoch >= observation_started_epoch_sec
        )
    except StopIteration as exc:
        raise ValueError(
            "relay status never advanced past the observation start"
        ) from exc
    metric_indexes = tuple(
        index
        for index, observed_epoch in enumerate(observed)
        if observed_epoch - observation_started_epoch_sec
        >= ROLLING_WINDOW_WARMUP_SECONDS
        and samples_appended[index] - samples_appended[turnover_anchor_index]
        >= SOURCE_AGE_WINDOW_PERIODS
    )
    metric_samples = tuple(samples[index] for index in metric_indexes)
    if len(metric_samples) < 2:
        raise ValueError(
            "certification window has insufficient ticks after source-age "
            "window turnover"
        )
    for sample in metric_samples:
        if (
            _nonnegative_int(
                sample.get("source_age_sample_count"),
                field="source_age_sample_count",
            )
            != SOURCE_AGE_WINDOW_PERIODS
        ):
            raise ValueError("source-age rolling window is not full after warmup")
    percentile_inputs: dict[str, list[float]] = {
        "p50_ms": [],
        "p95_ms": [],
        "p99_ms": [],
    }
    for sample in metric_samples:
        for artifact_key, relay_key in (
            ("p50_ms", "source_age_ms_p50"),
            ("p95_ms", "source_age_ms_p95"),
            ("p99_ms", "source_age_ms_p99"),
        ):
            percentile_inputs[artifact_key].append(
                _finite_float(sample.get(relay_key), field=relay_key)
            )
    metrics = {
        "p50_ms": nearest_rank_percentile(percentile_inputs["p50_ms"], 0.50),
        "p95_ms": nearest_rank_percentile(percentile_inputs["p95_ms"], 0.95),
        "p99_ms": nearest_rank_percentile(percentile_inputs["p99_ms"], 0.99),
        "sample_ticks_total": len(samples),
        "sample_ticks_aggregated": len(metric_samples),
        "duration_seconds": round(duration_seconds, 3),
        "rolling_window_warmup_seconds": ROLLING_WINDOW_WARMUP_SECONDS,
        "rolling_window_turnover_samples": SOURCE_AGE_WINDOW_PERIODS,
        "aggregation_started_epoch_sec": observed[metric_indexes[0]],
        "aggregation": "nearest_rank_over_post_warmup_relay_rolling_percentile_ticks",
        "source_age_basis": str(samples[-1].get("source_age_basis") or ""),
        "source_age_scope": str(samples[-1].get("source_age_scope") or ""),
    }

    counter_payload: dict[str, dict[str, int]] = {}
    for key in (
        "writer_splices",
        "writer_xruns",
        "writer_resets",
        "packets_lost",
        "sequence_resets",
        "sequence_reorders",
        "sequence_discontinuities",
        "periods_dropped_streaming",
    ):
        delta, total = _counter_delta(samples, key)
        counter_payload[key] = {"run_delta": delta, "end_total": total}
    fallback_key = "usb_mic_source_fallback_frames"
    fallback_start = _bridge_counter(bridge_stats_before, fallback_key)
    fallback_end = _bridge_counter(bridge_stats, fallback_key)
    if fallback_end < fallback_start:
        raise ValueError(f"aec_bridge.counters.{fallback_key} reset during window")
    counter_payload[fallback_key] = {
        "run_delta": fallback_end - fallback_start,
        "end_total": fallback_end,
    }

    writer_target_ms = writer_targets.pop()
    measurement_contract = {
        "relay_schema_version": USB_MIC_RELAY_SCHEMA_VERSION,
        "source_age_basis": USB_MIC_SOURCE_AGE_BASIS,
        "source_age_scope": USB_MIC_SOURCE_AGE_SCOPE,
        "aggregation": metrics["aggregation"],
    }
    configuration = {
        "build_sha": _build_sha(build_manifest),
        "bcd_device": USB_MIC_BCD_DEVICE,
        "selected_source": _selected_source(bridge_stats),
        "capture_geometry": _capture_geometry(bridge_stats),
        "writer_geometry": writer_geometries[0],
        "writer_target_ms": writer_target_ms,
        "measurement_contract": measurement_contract,
        "bridge_stats_schema_version": BRIDGE_STATS_SCHEMA_VERSION,
    }
    provenance = {
        "relay_runtime": {
            "pid": relay_pids.pop(),
            "started_epoch_sec": relay_started.pop(),
        },
        "bridge_runtime": {
            "pid": _positive_int(bridge_stats.get("pid"), field="aec_bridge.pid"),
            "started_epoch_sec": _finite_float(
                bridge_stats.get("started_epoch_sec"),
                field="aec_bridge.started_epoch_sec",
            ),
        },
        "observation_window": {
            "started_epoch_sec": observation_started_epoch_sec,
            "ended_epoch_sec": observation_ended_epoch_sec,
        },
        "host": {"os": host_os.strip(), "app": host_app.strip()},
    }
    configuration_sha256 = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = {"configuration": configuration, "provenance": provenance}
    identity_sha256 = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    healthy = bool(
        metrics["p95_ms"] <= USB_MIC_LATENCY_BUDGET_MS
        and all(counter["run_delta"] == 0 for counter in counter_payload.values())
    )
    payload: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "kind": "usb_mic_latency",
        "validated_at": (validated_at or datetime.now(timezone.utc)).isoformat(),
        "status": "pass" if healthy else "warn",
        "identity": identity,
        "identity_sha256": identity_sha256,
        "configuration_sha256": configuration_sha256,
        "metrics": metrics,
        "counters": counter_payload,
        "budget_ms": {"p95": USB_MIC_LATENCY_BUDGET_MS},
        "host_streaming_entire_window": True,
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _read_json_mapping(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_json_mapping_through_epoch(
    path: Path,
    *,
    minimum_updated_epoch_sec: float,
    status_name: str,
) -> tuple[dict[str, Any], float]:
    """Wait for one atomic status generation that covers the fixed cutoff."""

    timeout = time.monotonic() + MAX_STATUS_GAP_SECONDS
    while True:
        payload = _read_json_mapping(path)
        observed_epoch_sec = time.time()
        updated_epoch_sec = _finite_float(
            payload.get("updated_epoch_sec"),
            field=f"{status_name}.updated_epoch_sec",
        )
        if updated_epoch_sec >= minimum_updated_epoch_sec:
            return payload, observed_epoch_sec
        if time.monotonic() >= timeout:
            raise ValueError(
                f"{status_name} did not publish through the observation deadline"
            )
        time.sleep(STATUS_FENCE_POLL_SECONDS)


def _capture_status_window(
    *,
    status_path: Path,
    duration_seconds: float,
    interval_seconds: float,
) -> list[dict[str, Any]]:
    if not math.isfinite(duration_seconds) or duration_seconds < MIN_DURATION_SECONDS:
        raise ValueError(
            f"duration must be at least {MIN_DURATION_SECONDS:.0f} seconds"
        )
    if (
        not MIN_SAMPLE_INTERVAL_SECONDS
        <= interval_seconds
        <= MAX_SAMPLE_INTERVAL_SECONDS
    ):
        raise ValueError(
            "sample interval must be between "
            f"{MIN_SAMPLE_INTERVAL_SECONDS:.2f} and "
            f"{MAX_SAMPLE_INTERVAL_SECONDS:.2f} seconds"
        )
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + duration_seconds
    while True:
        sample = _read_json_mapping(status_path)
        sample["sample_observed_epoch_sec"] = time.time()
        samples.append(sample)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_seconds, remaining))
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Certify a live JTS computer-microphone latency window."
    )
    parser.add_argument("--duration-seconds", type=float, default=20.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.65)
    parser.add_argument("--host-os", required=True)
    parser.add_argument("--host-app", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--relay-status", type=Path, default=Path(RELAY_STATUS_PATH))
    parser.add_argument("--bridge-stats", type=Path, default=BRIDGE_STATS_PATH)
    parser.add_argument("--build-manifest", type=Path, default=BUILD_MANIFEST_PATH)
    parser.add_argument(
        "--bcd-device",
        type=Path,
        default=Path(GADGET_PATH) / "bcdDevice",
    )
    args = parser.parse_args(argv)

    try:
        bridge_before = _read_json_mapping(args.bridge_stats)
        build_before = args.build_manifest.read_text(encoding="utf-8")
        bcd_before = args.bcd_device.read_text(encoding="utf-8").strip()
        snapshots = _capture_status_window(
            status_path=args.relay_status,
            duration_seconds=args.duration_seconds,
            interval_seconds=args.sample_interval_seconds,
        )
        observation_deadline_epoch_sec = _finite_float(
            snapshots[-1].get("sample_observed_epoch_sec"),
            field="sample_observed_epoch_sec",
        )
        relay_after, relay_observed_epoch_sec = _read_json_mapping_through_epoch(
            args.relay_status,
            minimum_updated_epoch_sec=observation_deadline_epoch_sec,
            status_name="relay_status",
        )
        relay_after["sample_observed_epoch_sec"] = relay_observed_epoch_sec
        snapshots.append(relay_after)
        bridge_after, _bridge_observed_epoch_sec = _read_json_mapping_through_epoch(
            args.bridge_stats,
            minimum_updated_epoch_sec=observation_deadline_epoch_sec,
            status_name="aec_bridge_stats",
        )
        build_after = args.build_manifest.read_text(encoding="utf-8")
        bcd_after = args.bcd_device.read_text(encoding="utf-8").strip()
        if bridge_before.get("pid") != bridge_after.get("pid"):
            raise ValueError("AEC bridge restarted during the certification window")
        bridge_before_updated = _finite_float(
            bridge_before.get("updated_epoch_sec"),
            field="aec_bridge_stats.updated_epoch_sec",
        )
        bridge_after_updated = _finite_float(
            bridge_after.get("updated_epoch_sec"),
            field="aec_bridge_stats.updated_epoch_sec",
        )
        if bridge_after_updated <= bridge_before_updated:
            raise ValueError("AEC bridge stats did not advance during certification")
        bridge_stats_age = time.time() - bridge_after_updated
        if bridge_stats_age < 0 or bridge_stats_age > MAX_STATUS_GAP_SECONDS:
            raise ValueError("AEC bridge stats are stale")
        if bridge_before.get("capture_stream") != bridge_after.get("capture_stream"):
            raise ValueError("AEC bridge capture_stream changed during the window")
        if _capture_plan_identity(bridge_before) != _capture_plan_identity(bridge_after):
            raise ValueError("AEC bridge active_capture_plan changed during the window")
        if build_before != build_after or bcd_before != bcd_after:
            raise ValueError("build or USB descriptor changed during the window")
        artifact = build_usb_mic_latency_artifact(
            snapshots,
            build_manifest=build_after,
            bcd_device=bcd_after,
            bridge_stats_before=bridge_before,
            bridge_stats=bridge_after,
            host_os=args.host_os,
            host_app=args.host_app,
            observation_deadline_epoch_sec=observation_deadline_epoch_sec,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    encoded = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    atomic_write_text(args.output, encoded, mode=0o644)
    if args.stdout:
        print(encoded, end="")
    else:
        print(
            "usb_mic_latency "
            f"status={artifact['status']} "
            f"p95_ms={artifact['metrics']['p95_ms']} "
            f"ticks={artifact['metrics']['sample_ticks_aggregated']} "
            f"path={args.output}"
        )
    return 1 if args.require_pass and artifact["status"] != "pass" else 0


__all__ = [
    "build_usb_mic_latency_artifact",
    "main",
    "nearest_rank_percentile",
]
