# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Write route-latency validation artifacts from measured latency samples.

This CLI is deliberately an artifact producer, not an audio measurement
engine. The measurement harness owns click playback/capture and passes either
raw per-impulse latency samples or aggregate percentiles here. This module binds
that measured evidence to the live audio runtime route identity, then writes the
schema-v1 artifact consumed by /state and jasper-doctor.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import socket
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jasper.audio_runtime_plan import (
    ROUTE_USB_LOW_LATENCY_48K,
    build_audio_runtime_plan_from_system,
)
from jasper.audio_validation import (
    ROUTE_LATENCY_P95_BUDGET_MS,
    ROUTE_LATENCY_P95_MIN_DURATION_SECONDS,
    ROUTE_LATENCY_P99_BUDGET_MS,
    ROUTE_LATENCY_P99_MIN_DURATION_SECONDS,
    ValidationArtifact,
    artifact_directory,
    make_route_latency_artifact,
    percentile_min_samples,
    route_live_state_issues,
    write_artifact,
    write_latest_pointer,
)


FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"
USBSINK_STATE_PATH = "/run/jasper-usbsink/state.json"


@dataclass(frozen=True)
class RouteLatencyMetrics:
    """Measured latency metrics ready to persist in a route artifact."""

    p95_ms: float | None
    p99_ms: float | None
    sample_count: int
    duration_seconds: float
    source: str
    provenance: Mapping[str, Any] = field(default_factory=dict)


def nearest_rank_percentile(samples: Iterable[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile for measured latency samples."""

    values = sorted(float(sample) for sample in samples)
    if not values:
        return None
    p = float(percentile)
    if p > 1.0:
        p = p / 100.0
    if not 0.0 < p < 1.0:
        raise ValueError(f"percentile must be in (0, 1), got {percentile!r}")
    idx = max(0, min(len(values) - 1, int(math.ceil(p * len(values))) - 1))
    return values[idx]


def metrics_from_samples(
    samples_ms: Iterable[float],
    *,
    duration_seconds: float,
    source: str = "raw_samples",
    provenance: Mapping[str, Any] | None = None,
) -> RouteLatencyMetrics:
    """Compute route-latency metrics from raw per-impulse latencies."""

    values = tuple(float(sample) for sample in samples_ms)
    if not values:
        raise ValueError("at least one latency sample is required")
    for value in values:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"latency sample must be finite and non-negative: {value!r}")
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    evidence = {"input_kind": "raw_samples", "source": source}
    if provenance:
        evidence.update(provenance)
    return RouteLatencyMetrics(
        p95_ms=nearest_rank_percentile(values, 0.95),
        p99_ms=nearest_rank_percentile(values, 0.99),
        sample_count=len(values),
        duration_seconds=float(duration_seconds),
        source=source,
        provenance=evidence,
    )


def metrics_from_aggregates(
    *,
    p95_ms: float | None,
    p99_ms: float | None,
    sample_count: int,
    duration_seconds: float,
) -> RouteLatencyMetrics:
    """Accept aggregate metrics from an external measurement harness."""

    if sample_count < 0:
        raise ValueError("sample_count must be non-negative")
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    for name, value in (("p95_ms", p95_ms), ("p99_ms", p99_ms)):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{name} must be finite and non-negative")
    return RouteLatencyMetrics(
        p95_ms=p95_ms,
        p99_ms=p99_ms,
        sample_count=sample_count,
        duration_seconds=float(duration_seconds),
        source="aggregate_metrics",
        provenance={"input_kind": "aggregate_metrics"},
    )


def _latency_values_from_json(payload: Any) -> list[float]:
    if isinstance(payload, list):
        raw_values = payload
    elif isinstance(payload, dict):
        raw_values = (
            payload.get("latencies_ms")
            or payload.get("latency_ms")
            or payload.get("samples_ms")
            or payload.get("samples")
        )
    else:
        raw_values = None
    if not isinstance(raw_values, list):
        raise ValueError(
            "JSON latency input must be a list, or an object with "
            "latencies_ms/latency_ms/samples_ms/samples"
        )
    values: list[float] = []
    for item in raw_values:
        if isinstance(item, Mapping):
            item = item.get("latency_ms")
        values.append(float(item))
    return values


def _read_sample_text(path: Path | None) -> tuple[str, dict[str, Any]]:
    if path is None or str(path) == "-":
        text = sys.stdin.read()
        label = "stdin"
        raw_bytes = text.encode("utf-8")
    else:
        label = str(path)
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
    return text, {
        "sample_source": label,
        "sample_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "sample_bytes": len(raw_bytes),
    }


def _parse_latency_samples_text(text: str, *, label: str) -> list[float]:
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{label} did not contain latency samples")
    if stripped[0] in "[{":
        return _latency_values_from_json(json.loads(stripped))

    rows = list(csv.reader(stripped.splitlines()))
    if not rows:
        raise ValueError(f"{label} did not contain latency samples")
    header = [cell.strip().lower() for cell in rows[0]]
    data_rows = rows
    column = 0
    if "latency_ms" in header:
        column = header.index("latency_ms")
        data_rows = rows[1:]
    values: list[float] = []
    for row in data_rows:
        if not row:
            continue
        values.append(float(row[column]))
    return values


def load_latency_samples_with_provenance(
    path: Path | None,
) -> tuple[list[float], Mapping[str, Any]]:
    """Load measured latency samples plus provenance for the parsed bytes."""

    text, provenance = _read_sample_text(path)
    return _parse_latency_samples_text(
        text,
        label=str(provenance["sample_source"]),
    ), provenance


def load_latency_samples(path: Path | None) -> list[float]:
    """Load measured latency samples from JSON, CSV, plain text, or stdin."""

    samples, _provenance = load_latency_samples_with_provenance(path)
    return samples


def build_route_latency_artifact_from_metrics(
    metrics: RouteLatencyMetrics,
    *,
    impulse_spacing_jittered: bool,
    route_health_ok: bool,
    route_health_issues: tuple[str, ...] = (),
    measurement_provenance: Mapping[str, Any] | None = None,
) -> ValidationArtifact:
    """Bind measured route metrics to the live runtime plan identity."""

    plan = build_audio_runtime_plan_from_system()
    route = plan.route_profile
    if route.route_id != ROUTE_USB_LOW_LATENCY_48K:
        raise ValueError(
            f"route-latency artifacts are only valid for {ROUTE_USB_LOW_LATENCY_48K}; "
            f"current route is {route.route_id}"
        )
    if plan.errors:
        raise ValueError(
            "route-latency artifacts require a clean audio runtime plan; "
            + "; ".join(plan.errors)
        )
    identity = plan.route_latency_identity()
    dac_id = str(identity.get("dac_profile_id") or plan.profile_id or "unknown")
    provenance: dict[str, Any] = dict(metrics.provenance)
    if measurement_provenance:
        provenance.update(
            {
                str(key): value
                for key, value in measurement_provenance.items()
                if value is not None and value != ""
            }
        )
    return make_route_latency_artifact(
        route_id=str(identity["route_id"]),
        source_id=str(identity["source_id"]),
        dac_id=dac_id,
        route_config_hash=str(identity["route_config_hash"]),
        camilla_config_hash=str(identity.get("camilla_config_hash") or ""),
        fanin_resampler_config=_mapping(identity.get("fanin_resampler_config")),
        outputd_config=_mapping(identity.get("outputd_config")),
        rust_bridge_config=_mapping(identity.get("rust_bridge_config")),
        uac2_gadget_attrs=_mapping(identity.get("uac2_gadget_attrs")),
        p95_ms=metrics.p95_ms,
        p99_ms=metrics.p99_ms,
        sample_count=metrics.sample_count,
        duration_seconds=metrics.duration_seconds,
        measurement_provenance=provenance,
        impulse_spacing_jittered=impulse_spacing_jittered,
        route_health_ok=route_health_ok,
        route_health_issues=route_health_issues,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_status_socket(path: str) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        sock.connect(path)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    parsed = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("STATUS response root is not an object")
    return parsed


def _route_live_state_issues_for_current_route() -> tuple[str, ...]:
    plan = build_audio_runtime_plan_from_system()
    issues: list[str] = []
    usbsink_state: dict[str, Any] | None = None
    fanin_status: dict[str, Any] | None = None
    try:
        parsed = json.loads(Path(USBSINK_STATE_PATH).read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            usbsink_state = parsed
        else:
            issues.append("live_usbsink_state_malformed")
    except (OSError, json.JSONDecodeError) as e:
        issues.append(f"live_usbsink_state_unreadable:{type(e).__name__}")
    try:
        fanin_status = _read_status_socket(FANIN_STATUS_SOCKET)
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        issues.append(f"live_fanin_status_unreadable:{type(e).__name__}")
    return tuple(
        dict.fromkeys(
            (
                *issues,
                *route_live_state_issues(
                    plan.route_latency_identity(),
                    usbsink_state=usbsink_state,
                    fanin_status=fanin_status,
                ),
            )
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Write a route-latency artifact from measured USB route latency "
            "samples or aggregate metrics. Quick validation requires "
            f"p95<={ROUTE_LATENCY_P95_BUDGET_MS:g} ms with at least "
            f"{percentile_min_samples(95)} impulses over "
            f"{ROUTE_LATENCY_P95_MIN_DURATION_SECONDS:g}s; promotion also "
            f"requires p99<={ROUTE_LATENCY_P99_BUDGET_MS:g} ms with at least "
            f"{percentile_min_samples(99)} jittered impulses over "
            f"{ROUTE_LATENCY_P99_MIN_DURATION_SECONDS:g}s."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--samples",
        type=Path,
        help=(
            "JSON/CSV/text latency samples in milliseconds. Use '-' for stdin. "
            "JSON may be a list or an object with latencies_ms/samples."
        ),
    )
    source.add_argument(
        "--p95-ms",
        type=float,
        help="Measured p95 latency in milliseconds from an external harness.",
    )
    parser.add_argument(
        "--p99-ms",
        type=float,
        default=None,
        help="Measured p99 latency in milliseconds from an external harness.",
    )
    parser.add_argument(
        "--sample-count",
        type=int,
        default=None,
        help="Impulse count when using aggregate --p95-ms/--p99-ms metrics.",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        required=True,
        help="Measurement duration in seconds.",
    )
    parser.add_argument(
        "--impulse-spacing-jittered",
        action="store_true",
        help="Declare that p99 samples used jittered impulse spacing.",
    )
    parser.add_argument(
        "--harness-id",
        default="",
        help=(
            "Stable id/name of the measurement harness. Required when using "
            "aggregate --p95-ms/--p99-ms metrics."
        ),
    )
    parser.add_argument(
        "--harness-version",
        default="",
        help="Version, git SHA, or build id of the measurement harness.",
    )
    parser.add_argument(
        "--measurement-id",
        default="",
        help="External run id or artifact id for the measurement window.",
    )
    parser.add_argument(
        "--route-health-ok",
        action="store_true",
        help=(
            "Declare that bridge/fan-in/outputd health counters stayed clean "
            "during the measurement window."
        ),
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Artifact directory (default: /var/lib/jasper/audio-validation).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full artifact JSON to stdout.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Build and optionally print the artifact without writing it.",
    )
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Exit non-zero unless the artifact status is pass.",
    )
    return parser


def _metrics_from_args(args: argparse.Namespace) -> RouteLatencyMetrics:
    if args.samples is not None:
        samples, provenance = load_latency_samples_with_provenance(args.samples)
        return metrics_from_samples(
            samples,
            duration_seconds=args.duration_seconds,
            source=str(args.samples),
            provenance=provenance,
        )
    if args.sample_count is None:
        raise ValueError("--sample-count is required with aggregate --p95-ms")
    if not args.harness_id.strip():
        raise ValueError("--harness-id is required with aggregate --p95-ms metrics")
    return metrics_from_aggregates(
        p95_ms=args.p95_ms,
        p99_ms=args.p99_ms,
        sample_count=args.sample_count,
        duration_seconds=args.duration_seconds,
    )


def _measurement_provenance_from_args(args: argparse.Namespace) -> Mapping[str, Any]:
    return {
        "harness_id": args.harness_id.strip(),
        "harness_version": args.harness_version.strip(),
        "measurement_id": args.measurement_id.strip(),
    }


def _print_summary(artifact: ValidationArtifact, *, path: Path | None) -> None:
    checks = artifact.checks
    issues = checks.get("issues")
    certified = checks.get("certified_percentiles")
    print(
        "route_latency "
        f"status={artifact.status} "
        f"profile={artifact.profile} "
        f"p95_ms={checks.get('p95_ms')} "
        f"p99_ms={checks.get('p99_ms')} "
        f"samples={checks.get('sample_count')} "
        f"duration_seconds={checks.get('duration_seconds')} "
        f"certified={certified} "
        f"issues={issues} "
        f"path={path or 'report-only'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        metrics = _metrics_from_args(args)
        route_health_issues = (
            _route_live_state_issues_for_current_route()
            if args.route_health_ok
            else ()
        )
        artifact = build_route_latency_artifact_from_metrics(
            metrics,
            impulse_spacing_jittered=args.impulse_spacing_jittered,
            route_health_ok=args.route_health_ok,
            route_health_issues=route_health_issues,
            measurement_provenance=_measurement_provenance_from_args(args),
        )
    except (OSError, ValueError, json.JSONDecodeError) as e:
        parser.error(str(e))

    path: Path | None = None
    if not args.report_only:
        directory = args.directory or artifact_directory()
        path = write_artifact(artifact, directory=directory)
        write_latest_pointer(artifact, directory=directory)
    if args.stdout:
        json.dump(artifact.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        _print_summary(artifact, path=path)

    if artifact.status == "fail":
        return 1
    if args.require_pass and artifact.status != "pass":
        return 1
    return 0


__all__ = [
    "RouteLatencyMetrics",
    "build_route_latency_artifact_from_metrics",
    "load_latency_samples",
    "load_latency_samples_with_provenance",
    "main",
    "metrics_from_aggregates",
    "metrics_from_samples",
    "nearest_rank_percentile",
]
