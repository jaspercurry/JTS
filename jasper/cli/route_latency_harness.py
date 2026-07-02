# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`jasper-route-latency-harness` — the click/capture measurement CLI.

Produces the real per-impulse latency evidence
`jasper.cli.route_latency_artifact --samples` needs to certify (or honestly
fail) the `usb_low_latency_48k` route. This CLI is a measurement producer,
not a certification authority: `analyze` writes samples-JSON in the
artifact's exact schema and prints an honest route-health delta, but never
decides pass/fail itself — that stays `route_latency_artifact`'s job (see
`jasper.audio_validation.route_latency_gate_status`).

Four steps, run separately or via `run`:

  generate  — write the click-track WAV + JSON schedule for a preset
              (quick or promotion). No daemon interaction.
  capture   — the LIVE step: arm the Rust ingress tap, listen on the mic
              reader for the schedule's duration while a human plays the
              WAV on the host, disarm, and record raw evidence (mic
              detections JSONL + a route-health before/after snapshot).
              Mic detection cannot happen after the fact — UDP/ALSA is not
              a recording — so this step must run *during* playback.
  analyze   — pure/offline: read the tap's JSONL (produced directly by the
              Rust process, not by this CLI) plus capture's mic-detections
              JSONL and health snapshot, pair them, compute latency,
              write samples-JSON + a human summary, and refuse to emit a
              feedable file below the match-rate floor.
  run       — capture then analyze in one shot, with an optional
              --invoke-artifact passthrough to
              jasper-route-latency-artifact.

Route-health honesty: this CLI NEVER asserts --route-health-ok on the
operator's behalf. It prints the before/after counter deltas from the
bridge/usbsink/fan-in/outputd status surfaces and states whether the
declaration WOULD be justified; the operator (or an explicit
--assume-route-health-ok flag on `run`) makes the actual call.
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper.audio_validation import (
    ROUTE_LATENCY_P95_BUDGET_MS,
    ROUTE_LATENCY_P99_BUDGET_MS,
    certified_route_latency_percentiles,
)
from jasper.log_event import log_event
from jasper.route_latency import click_track
from jasper.route_latency.impulse_detect import (
    DEFAULT_HYSTERESIS,
    DEFAULT_REFRACTORY_MS,
    DEFAULT_THRESHOLD,
    StreamingDetector,
    refractory_samples_for,
)
from jasper.route_latency.mic_readers import (
    MicSourceUnavailableError,
    build_mic_reader,
)
from jasper.route_latency.pairing import (
    DEFAULT_WINDOW_MS,
    MicDetection,
    PairingResult,
    pair_events,
)
from jasper.route_latency.tap_client import (
    DEFAULT_TAP_PATH,
    TapArmParams,
    TapClient,
    TapClientError,
    read_tap_events,
)


logger = logging.getLogger("jasper.cli.route_latency_harness")

HARNESS_ID = "jts-click-capture-v1"

DEFAULT_OUT_DIR = Path("/var/lib/jasper/audio-validation/route-latency-harness")
DEFAULT_DISTANCE_COMPENSATION_MS = 0.0
# Speed of sound in air ≈ 343 m/s at room temperature -> ~0.029 ms per cm,
# i.e. ~0.29 ms per 10 cm. Documented per the pinned contract's default-0,
# operator-supplied `--mic-distance-cm` flag.
SOUND_MS_PER_CM = 100.0 / 34_300.0

USBSINK_STATE_PATH = Path("/run/jasper-usbsink/state.json")
FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"
OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"

MIN_MATCH_RATE_DEFAULT = 0.90

# Counters where an increase during the measurement window means the route
# was NOT healthy for the whole window — a latency number measured across
# an xrun/drop is not trustworthy evidence either way. This list is
# intentionally a curated subset of the full snapshot (which is diffed and
# printed in full for transparency) rather than the only thing compared:
# any nonzero delta anywhere is worth an operator's eyes, but these are the
# ones this CLI explicitly calls out as "this alone means unhealthy."
KNOWN_HEALTH_COUNTER_PATHS: tuple[tuple[str, ...], ...] = (
    ("usbsink", "counters", "capture_xruns"),
    ("usbsink", "counters", "playback_xruns"),
    ("usbsink", "counters", "underflow_periods"),
    ("usbsink", "counters", "overflow_events"),
    ("usbsink", "counters", "dropped_periods"),
)


# --------------------------------------------------------------------------
# Route-health snapshot (bridge/usbsink/fan-in/outputd) — honesty, not gate
# --------------------------------------------------------------------------


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log_event(
            logger,
            "route_latency_harness.health_snapshot_unavailable",
            source=str(path),
            error=str(e),
            level=logging.DEBUG,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_status_socket(path: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as e:
        log_event(
            logger,
            "route_latency_harness.health_snapshot_unavailable",
            source=path,
            error=str(e),
            level=logging.DEBUG,
        )
        return None
    try:
        parsed = json.loads(b"".join(chunks).decode("utf-8"))
    except json.JSONDecodeError as e:
        log_event(
            logger,
            "route_latency_harness.health_snapshot_invalid",
            source=path,
            error=str(e),
            level=logging.DEBUG,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def snapshot_route_health() -> dict[str, Any]:
    """Best-effort snapshot of the bridge/usbsink/fan-in/outputd surfaces.

    Fails soft per-surface: an unreachable daemon records `null` for that
    key rather than raising, so a snapshot taken before a daemon is up (or
    after it's gone) still captures whatever IS available.
    """

    return {
        "captured_at_monotonic_ns": time.monotonic_ns(),
        "usbsink": _read_json_file(USBSINK_STATE_PATH),
        "fanin": _read_status_socket(FANIN_STATUS_SOCKET),
        "outputd": _read_status_socket(OUTPUTD_STATUS_SOCKET),
    }


def _numeric_deltas(before: Any, after: Any, *, prefix: tuple[str, ...] = ()) -> dict[str, float]:
    """Recursively diff two JSON-like trees, returning {"a.b.c": after-before}
    for every leaf where both sides are numeric and the value changed.

    Generic on purpose: the bridge/usbsink/fan-in/outputd counter surfaces
    evolve independently of this harness, so hardcoding a fixed field list
    would silently stop reporting new counters. New/removed keys (a daemon
    added or removed a counter between snapshots) are skipped rather than
    treated as a numeric change — a schema change is not a health signal.
    """

    deltas: dict[str, float] = {}
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                continue
            deltas.update(_numeric_deltas(before[key], after[key], prefix=(*prefix, str(key))))
        return deltas
    if isinstance(before, (int, float)) and not isinstance(before, bool) and isinstance(after, (int, float)) and not isinstance(after, bool):
        if after != before:
            deltas[".".join(prefix)] = float(after) - float(before)
    return deltas


@dataclass(frozen=True)
class RouteHealthReport:
    """Honest before/after route-health comparison. Never asserts
    `route_health_ok` on the operator's behalf — see module docstring."""

    all_deltas: Mapping[str, float]
    known_counter_deltas: Mapping[str, float]
    before: Mapping[str, Any]
    after: Mapping[str, Any]

    @property
    def would_justify_route_health_ok(self) -> bool:
        """True iff every KNOWN_HEALTH_COUNTER_PATHS delta is <= 0 (i.e. no
        new xruns/drops) AND every surface answered in both snapshots.

        This is advisory only — see class docstring and the CLI's printed
        caveat. A CLI that silently asserted this for the operator would
        be exactly the "auto-declare route health" failure mode the
        architecture brief and `route_latency_artifact` explicitly refuse.
        """

        if any(self.before.get(k) is None or self.after.get(k) is None for k in ("usbsink", "fanin", "outputd")):
            return False
        return all(delta <= 0 for delta in self.known_counter_deltas.values())


def diff_route_health(before: Mapping[str, Any], after: Mapping[str, Any]) -> RouteHealthReport:
    all_deltas = _numeric_deltas(dict(before), dict(after))
    known = {
        ".".join(path): all_deltas.get(".".join(path), 0.0)
        for path in KNOWN_HEALTH_COUNTER_PATHS
    }
    return RouteHealthReport(
        all_deltas=all_deltas,
        known_counter_deltas=known,
        before=before,
        after=after,
    )


# --------------------------------------------------------------------------
# Egress (mic-side) detection over a live capture window
# --------------------------------------------------------------------------


def capture_mic_detections(
    mic_spec: str,
    *,
    duration_seconds: float,
    threshold: float = DEFAULT_THRESHOLD,
    hysteresis: float = DEFAULT_HYSTERESIS,
    refractory_ms: float = DEFAULT_REFRACTORY_MS,
) -> list[MicDetection]:
    """Read the mic source for `duration_seconds`, returning every detected
    impulse re-anchored to its packet's arrival time.

    Per the pinned clock rule (see `jasper.route_latency.mic_readers`
    module docstring): each chunk's detector offset is converted to an
    event time using THAT chunk's own arrival timestamp, never a single
    stream-start anchor plus a cumulative sample count — this bounds clock-
    drift error to one chunk's worth of uncertainty regardless of how long
    the capture runs.

    A `MicSourceUnavailableError` raised on the VERY FIRST read (before any
    chunk has ever arrived) propagates as a hard failure — nothing is
    feeding the socket at all, which the caller must treat loudly (see
    `MicSourceUnavailableError`'s docstring). A source that goes quiet
    AFTER already delivering at least one chunk (a scripted/finite reader
    reaching its end, or a real stream that stops mid-window) ends the
    capture gracefully with whatever was collected so far, rather than
    discarding a real partial measurement.
    """

    reader = build_mic_reader(mic_spec)
    detections: list[MicDetection] = []
    detector: StreamingDetector | None = None
    deadline = time.monotonic() + duration_seconds
    received_any_chunk = False
    try:
        while time.monotonic() < deadline:
            try:
                chunk = reader.read_chunk()
            except MicSourceUnavailableError:
                if received_any_chunk:
                    break
                raise
            received_any_chunk = True
            if detector is None:
                detector = StreamingDetector(
                    threshold=threshold,
                    hysteresis=hysteresis,
                    refractory_samples=refractory_samples_for(
                        refractory_ms, chunk.sample_rate_hz,
                    ),
                )
            samples = _bytes_to_int16(chunk.samples)
            n = len(samples)
            for detection in detector.feed(samples):
                remaining = n - detection.sample_offset
                event_ns = chunk.arrival_monotonic_ns - round(
                    remaining * 1_000_000_000 / chunk.sample_rate_hz
                )
                detections.append(MicDetection(monotonic_ns=event_ns, peak=detection.peak))
    finally:
        reader.close()
    return detections


def _bytes_to_int16(data: bytes) -> list[int]:
    count = len(data) // 2
    if count == 0:
        return []
    import array

    values = array.array("h")
    values.frombytes(data[: count * 2])
    if sys.byteorder != "little":
        values.byteswap()
    return values.tolist()


def write_mic_detections_jsonl(detections: list[MicDetection], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for d in detections:
            f.write(json.dumps({"monotonic_ns": d.monotonic_ns, "peak": d.peak}) + "\n")


def read_mic_detections_jsonl(path: Path) -> list[MicDetection]:
    detections: list[MicDetection] = []
    if not path.exists():
        return detections
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            detections.append(
                MicDetection(monotonic_ns=int(obj["monotonic_ns"]), peak=float(obj["peak"]))
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return detections


# --------------------------------------------------------------------------
# Latency math + samples-JSON emission (matches route_latency_artifact's
# accepted schema exactly — see jasper.cli.route_latency_artifact
# `_latency_values_from_json`)
# --------------------------------------------------------------------------


def latency_ms_for_match(match, *, mic_distance_compensation_ms: float) -> float:  # type: ignore[no-untyped-def]
    """(t_mic_detect - t_tap_detect) + ring_fill_frames/48.0 - distance_ms.

    `ring_fill_frames` is the Rust ring's fill depth (in FRAMES, not
    periods — see the pinned JSONL schema) at the moment of tap detection:
    it is dwell time the audio spent queued in the ring before this
    measurement's ingress timestamp, so it is additive latency the tap
    timestamp alone does not capture. Divided by 48.0 because the ring
    runs at 48 kHz (48 frames/ms).
    """

    raw_ms = match.raw_delta_ns / 1_000_000.0
    ring_dwell_ms = match.tap.ring_fill_frames / 48.0
    return raw_ms + ring_dwell_ms - mic_distance_compensation_ms


@dataclass(frozen=True)
class AnalyzeResult:
    latencies_ms: tuple[float, ...]
    pairing: PairingResult
    match_rate_floor: float

    @property
    def match_rate_ok(self) -> bool:
        return self.pairing.match_rate >= self.match_rate_floor


def analyze_matches(
    tap_events,  # type: ignore[no-untyped-def]
    mic_detections: list[MicDetection],
    *,
    window_ms: float = DEFAULT_WINDOW_MS,
    mic_distance_compensation_ms: float = DEFAULT_DISTANCE_COMPENSATION_MS,
    match_rate_floor: float = MIN_MATCH_RATE_DEFAULT,
) -> AnalyzeResult:
    pairing = pair_events(list(tap_events), mic_detections, window_ms=window_ms)
    latencies = tuple(
        latency_ms_for_match(m, mic_distance_compensation_ms=mic_distance_compensation_ms)
        for m in pairing.matched
    )
    return AnalyzeResult(latencies_ms=latencies, pairing=pairing, match_rate_floor=match_rate_floor)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    import math

    idx = max(0, min(len(ordered) - 1, math.ceil(pct * len(ordered)) - 1))
    return ordered[idx]


def summarize_latencies(latencies_ms: tuple[float, ...]) -> dict[str, Any]:
    values = list(latencies_ms)
    return {
        "count": len(values),
        "duration_covered_ms": (max(values) - min(values)) if values else 0.0,
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
    }


def write_samples_json(latencies_ms: tuple[float, ...], path: Path) -> Path:
    """Write samples in the bare-list shape `route_latency_artifact --samples`
    accepts (see `_latency_values_from_json` — list-of-floats is option 1,
    the simplest of the three accepted shapes)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(latencies_ms), indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# CLI subcommands
# --------------------------------------------------------------------------


def _cmd_generate(args: argparse.Namespace) -> int:
    schedule = click_track.build_schedule(
        args.preset,
        amplitude_dbfs=args.amplitude_dbfs,
        seed=args.seed,
    )
    out_dir = Path(args.out_dir)
    wav_path = click_track.render_wav(schedule, out_dir / f"{args.preset}-click-track.wav")
    schedule_path = click_track.write_schedule_json(schedule, out_dir / f"{args.preset}-schedule.json")
    print(
        f"generated preset={args.preset} impulses={schedule.impulse_count} "
        f"duration_s={schedule.duration_seconds:g} jittered={schedule.jittered} "
        f"amplitude_dbfs={schedule.amplitude_dbfs:g}"
    )
    print(f"wav={wav_path}")
    print(f"schedule={schedule_path}")
    print(
        "Play the WAV on the Mac/Windows host into the JTS USB audio device "
        "at a modest, comfortable volume — start very quiet and confirm by "
        "ear before raising it (CamillaDSP's volume_limit stays the 0 dB "
        "ceiling either way). Then run `capture` for the same duration."
    )
    return 0


def _cmd_arm(args: argparse.Namespace) -> int:
    client = TapClient(host=args.tap_host, port=args.tap_port)
    try:
        response = client.arm(
            TapArmParams(
                threshold=args.tap_threshold,
                hysteresis=args.tap_hysteresis,
                refractory_ms=args.tap_refractory_ms,
                path=args.tap_path,
            )
        )
    except TapClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2))
    return 0


def _cmd_disarm(args: argparse.Namespace) -> int:
    client = TapClient(host=args.tap_host, port=args.tap_port)
    try:
        response = client.disarm()
    except TapClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2))
    return 0


def _cmd_capture(args: argparse.Namespace) -> int:
    schedule = click_track.load_schedule_json(Path(args.schedule))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = TapClient(host=args.tap_host, port=args.tap_port)
    try:
        client.arm(
            TapArmParams(
                threshold=args.tap_threshold,
                hysteresis=args.tap_hysteresis,
                refractory_ms=args.tap_refractory_ms,
                path=args.tap_path,
            )
        )
    except TapClientError as e:
        print(f"error arming tap: {e}", file=sys.stderr)
        return 1

    before = snapshot_route_health()
    print(
        f"tap armed. Play {schedule.preset_name}'s click-track WAV now — "
        f"capturing mic for {schedule.duration_seconds:g}s."
    )
    try:
        detections = capture_mic_detections(
            args.mic,
            duration_seconds=schedule.duration_seconds,
            threshold=args.mic_threshold,
            hysteresis=args.mic_hysteresis,
            refractory_ms=args.mic_refractory_ms,
        )
    except MicSourceUnavailableError as e:
        print(f"error: {e}", file=sys.stderr)
        try:
            client.disarm()
        except TapClientError:
            pass
        return 1
    after = snapshot_route_health()

    try:
        client.disarm()
    except TapClientError as e:
        print(f"warning: disarm failed: {e}", file=sys.stderr)

    mic_path = out_dir / "mic-detections.jsonl"
    write_mic_detections_jsonl(detections, mic_path)
    health_path = out_dir / "route-health-snapshot.json"
    health_path.write_text(json.dumps({"before": before, "after": after}, indent=2), encoding="utf-8")

    print(f"mic detections: {len(detections)} -> {mic_path}")
    print(f"route-health snapshot -> {health_path}")
    print(f"tap events are written directly by the Rust process to {args.tap_path}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    tap_events = read_tap_events(Path(args.tap_events))
    mic_detections = read_mic_detections_jsonl(Path(args.mic_detections))
    if not tap_events:
        print(
            f"error: no tap events found in {args.tap_events} — was the tap "
            "armed and did the Rust process detect any impulses?",
            file=sys.stderr,
        )
        return 1
    if not mic_detections:
        print(
            f"error: no mic detections found in {args.mic_detections} — did "
            "capture run for the full window while the WAV was playing?",
            file=sys.stderr,
        )
        return 1

    result = analyze_matches(
        tap_events,
        mic_detections,
        window_ms=args.pairing_window_ms,
        mic_distance_compensation_ms=args.mic_distance_compensation_ms,
        match_rate_floor=args.min_match_rate,
    )
    summary = summarize_latencies(result.latencies_ms)

    print(
        "pairing: "
        f"tap_events={result.pairing.tap_count} "
        f"matched={len(result.pairing.matched)} "
        f"unmatched_tap={len(result.pairing.unmatched_tap)} "
        f"unmatched_mic={len(result.pairing.unmatched_mic)} "
        f"ambiguous_rejected={len(result.pairing.ambiguous_tap)} "
        f"match_rate={result.pairing.match_rate:.1%}"
    )
    print(
        "latency: "
        f"count={summary['count']} "
        f"p50_ms={_fmt(summary['p50_ms'])} "
        f"p95_ms={_fmt(summary['p95_ms'])} "
        f"p99_ms={_fmt(summary['p99_ms'])}"
    )
    certified = certified_route_latency_percentiles(
        sample_count=summary["count"],
        duration_seconds=args.duration_seconds,
        jittered_impulse_spacing=args.impulse_spacing_jittered,
    )
    print(
        f"certifiable_percentiles={list(certified)} "
        f"(p95 budget {ROUTE_LATENCY_P95_BUDGET_MS:g} ms, "
        f"p99 budget {ROUTE_LATENCY_P99_BUDGET_MS:g} ms — "
        "certification itself happens in jasper-route-latency-artifact)"
    )

    if not result.match_rate_ok:
        print(
            f"error: match rate {result.pairing.match_rate:.1%} is below the "
            f"floor {result.match_rate_floor:.1%} — refusing to emit an "
            "artifact-feedable samples file. Check playback volume, mic "
            "threshold, and that the WAV played to completion.",
            file=sys.stderr,
        )
        return 1

    health_report: RouteHealthReport | None = None
    health_snapshot_path = Path(args.route_health_snapshot) if args.route_health_snapshot else None
    if health_snapshot_path and health_snapshot_path.exists():
        raw = json.loads(health_snapshot_path.read_text(encoding="utf-8"))
        health_report = diff_route_health(raw.get("before") or {}, raw.get("after") or {})
        _print_health_report(health_report)

    out_dir = Path(args.out_dir)
    samples_path = write_samples_json(result.latencies_ms, out_dir / "latency-samples.json")
    print(f"samples -> {samples_path}")

    if args.invoke_artifact:
        return _invoke_artifact(
            samples_path,
            duration_seconds=args.duration_seconds,
            impulse_spacing_jittered=args.impulse_spacing_jittered,
            measurement_id=args.measurement_id,
            route_health_ok=bool(health_report and health_report.would_justify_route_health_ok and args.confirm_route_health_ok),
            require_pass=args.require_pass,
        )
    return 0


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _print_health_report(report: RouteHealthReport) -> None:
    print("route-health deltas (before -> after the measurement window):")
    if not report.all_deltas:
        print("  no numeric deltas observed on any reachable surface")
    for key, delta in sorted(report.all_deltas.items()):
        flag = " *** known counter ***" if key in report.known_counter_deltas and delta != 0 else ""
        print(f"  {key}: {delta:+g}{flag}")
    verdict = "WOULD be justified" if report.would_justify_route_health_ok else "would NOT be justified"
    print(
        f"--route-health-ok on jasper-route-latency-artifact {verdict} by "
        "this snapshot. This tool never asserts it for you — review the "
        "deltas above and pass --confirm-route-health-ok (with "
        "--invoke-artifact) only if you agree."
    )


def _invoke_artifact(
    samples_path: Path,
    *,
    duration_seconds: float,
    impulse_spacing_jittered: bool,
    measurement_id: str,
    route_health_ok: bool,
    require_pass: bool,
) -> int:
    cmd = [
        "jasper-route-latency-artifact",
        "--samples",
        str(samples_path),
        "--duration-seconds",
        str(duration_seconds),
        "--harness-id",
        HARNESS_ID,
    ]
    if impulse_spacing_jittered:
        cmd.append("--impulse-spacing-jittered")
    if measurement_id:
        cmd.extend(["--measurement-id", measurement_id])
    if route_health_ok:
        cmd.append("--route-health-ok")
    if require_pass:
        cmd.append("--require-pass")
    print(f"invoking: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def _cmd_run(args: argparse.Namespace) -> int:
    rc = _cmd_capture(args)
    if rc != 0:
        return rc
    args.tap_events = args.tap_path
    args.mic_detections = str(Path(args.out_dir) / "mic-detections.jsonl")
    args.route_health_snapshot = str(Path(args.out_dir) / "route-health-snapshot.json")
    schedule = click_track.load_schedule_json(Path(args.schedule))
    args.duration_seconds = schedule.duration_seconds
    args.impulse_spacing_jittered = schedule.jittered
    return _cmd_analyze(args)


# --------------------------------------------------------------------------
# argparse wiring
# --------------------------------------------------------------------------


def _add_tap_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tap-host", default="127.0.0.1", help="Rust tap HTTP host (default: 127.0.0.1).")
    parser.add_argument("--tap-port", type=int, default=8781, help="Rust tap HTTP port (default: 8781).")


def _add_tap_arm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tap-threshold", type=float, default=None, help="Ingress detector threshold (0..1). Rust default 0.2.")
    parser.add_argument("--tap-hysteresis", type=float, default=None, help="Ingress detector hysteresis. Rust default 0.05.")
    parser.add_argument("--tap-refractory-ms", type=float, default=None, help="Ingress refractory window in ms. Rust default 250.")
    parser.add_argument("--tap-path", default=DEFAULT_TAP_PATH, help=f"Tap JSONL path (default: {DEFAULT_TAP_PATH}).")


def _add_mic_detector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mic", default="udp:9879", help="Mic source: udp:<port> (default, raw0 leg) or alsa:<device>.")
    parser.add_argument("--mic-threshold", type=float, default=DEFAULT_THRESHOLD, help=f"Egress detector threshold (default {DEFAULT_THRESHOLD}).")
    parser.add_argument("--mic-hysteresis", type=float, default=DEFAULT_HYSTERESIS, help=f"Egress detector hysteresis (default {DEFAULT_HYSTERESIS}).")
    parser.add_argument("--mic-refractory-ms", type=float, default=DEFAULT_REFRACTORY_MS, help=f"Egress refractory window in ms (default {DEFAULT_REFRACTORY_MS:g}).")


def _add_schedule_metadata_args(parser: argparse.ArgumentParser) -> None:
    """`--duration-seconds` / `--impulse-spacing-jittered` — needed by
    `analyze`, which has no schedule file to derive them from (it only sees
    raw JSONL evidence), but NOT by `run`, which loads the schedule
    directly and would otherwise silently discard whatever the operator
    typed here in favor of the schedule's own values."""
    parser.add_argument("--duration-seconds", type=float, required=True, help="Measurement window duration in seconds (passed through to --invoke-artifact).")
    parser.add_argument("--impulse-spacing-jittered", action="store_true", help="Declare jittered impulse spacing (required for p99 promotion certification).")


def _add_analyze_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pairing-window-ms", type=float, default=DEFAULT_WINDOW_MS, help=f"Max plausible tap->mic latency window (default {DEFAULT_WINDOW_MS:g} ms).")
    parser.add_argument("--mic-distance-compensation-ms", type=float, default=DEFAULT_DISTANCE_COMPENSATION_MS, help="Subtract this fixed acoustic travel time (default 0). See --mic-distance-cm for a distance-based shortcut.")
    parser.add_argument("--mic-distance-cm", type=float, default=None, help=f"Alternative to --mic-distance-compensation-ms: mic-to-speaker distance in cm (~{SOUND_MS_PER_CM * 10:.3g} ms per 10 cm at room temperature).")
    parser.add_argument("--min-match-rate", type=float, default=MIN_MATCH_RATE_DEFAULT, help=f"Refuse to emit samples below this tap-side match rate (default {MIN_MATCH_RATE_DEFAULT * 100:g} pct).")
    parser.add_argument("--measurement-id", default="", help="Optional run id, passed through to --invoke-artifact.")
    parser.add_argument("--invoke-artifact", action="store_true", help="Shell out to jasper-route-latency-artifact after writing samples.")
    parser.add_argument("--confirm-route-health-ok", action="store_true", help="Operator confirms the printed route-health deltas justify --route-health-ok on the artifact CLI. Never inferred automatically.")
    parser.add_argument("--require-pass", action="store_true", help="Passed through to --invoke-artifact.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Click/capture route-latency measurement harness. Produces the "
            "per-impulse samples jasper-route-latency-artifact --samples "
            "consumes to certify (or honestly fail) the usb_low_latency_48k "
            "route's p95/p99 claims."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_generate = sub.add_parser("generate", help="Write a click-track WAV + JSON schedule for a preset.")
    p_generate.add_argument("preset", choices=sorted(click_track.PRESETS), help="quick (p95 gate) or promotion (p99 gate).")
    p_generate.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"Output directory (default: {DEFAULT_OUT_DIR}).")
    p_generate.add_argument("--amplitude-dbfs", type=float, default=click_track.DEFAULT_AMPLITUDE_DBFS, help=f"Click amplitude in dBFS (default {click_track.DEFAULT_AMPLITUDE_DBFS:g} — modest by design; start quiet).")
    p_generate.add_argument("--seed", type=int, default=0, help="Schedule RNG seed (default 0; reproducible).")
    p_generate.set_defaults(func=_cmd_generate)

    p_arm = sub.add_parser("arm", help="Arm the Rust ingress tap.")
    _add_tap_connection_args(p_arm)
    _add_tap_arm_args(p_arm)
    p_arm.set_defaults(func=_cmd_arm)

    p_disarm = sub.add_parser("disarm", help="Disarm the Rust ingress tap.")
    _add_tap_connection_args(p_disarm)
    p_disarm.set_defaults(func=_cmd_disarm)

    p_capture = sub.add_parser("capture", help="Live step: arm, capture mic for the schedule's duration, disarm.")
    p_capture.add_argument("schedule", help="Path to the JSON schedule from `generate`.")
    p_capture.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"Output directory (default: {DEFAULT_OUT_DIR}).")
    _add_tap_connection_args(p_capture)
    _add_tap_arm_args(p_capture)
    _add_mic_detector_args(p_capture)
    p_capture.set_defaults(func=_cmd_capture)

    p_analyze = sub.add_parser("analyze", help="Offline step: pair tap/mic evidence, emit samples-JSON.")
    p_analyze.add_argument("--tap-events", default=DEFAULT_TAP_PATH, help=f"Tap JSONL path (default: {DEFAULT_TAP_PATH}).")
    p_analyze.add_argument("--mic-detections", required=True, help="Mic-detections JSONL from `capture`.")
    p_analyze.add_argument("--route-health-snapshot", default=None, help="route-health-snapshot.json from `capture`.")
    p_analyze.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"Output directory (default: {DEFAULT_OUT_DIR}).")
    _add_schedule_metadata_args(p_analyze)
    _add_analyze_args(p_analyze)
    p_analyze.set_defaults(func=_cmd_analyze)

    p_run = sub.add_parser("run", help="capture then analyze in one shot.")
    p_run.add_argument("schedule", help="Path to the JSON schedule from `generate`.")
    p_run.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"Output directory (default: {DEFAULT_OUT_DIR}).")
    _add_tap_connection_args(p_run)
    _add_tap_arm_args(p_run)
    _add_mic_detector_args(p_run)
    _add_analyze_args(p_run)
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "mic_distance_cm", None) is not None:
        args.mic_distance_compensation_ms = args.mic_distance_cm * SOUND_MS_PER_CM
    return args.func(args)


__all__ = [
    "HARNESS_ID",
    "AnalyzeResult",
    "RouteHealthReport",
    "analyze_matches",
    "capture_mic_detections",
    "diff_route_health",
    "latency_ms_for_match",
    "main",
    "read_mic_detections_jsonl",
    "snapshot_route_health",
    "summarize_latencies",
    "write_mic_detections_jsonl",
    "write_samples_json",
]


if __name__ == "__main__":
    raise SystemExit(main())
