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
operator's behalf. It prints the before/after counter deltas from the three
route status surfaces (usbsink — the Rust ingress daemon — plus fan-in and
outputd) and states whether the
declaration WOULD be justified; the operator makes the actual call by passing
--confirm-route-health-ok (alongside --invoke-artifact), which is only honored
when the printed health diff would itself justify it.
"""
from __future__ import annotations

import argparse
import json
import logging
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
from jasper.cli.route_latency_artifact import nearest_rank_percentile
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
    MatchedImpulse,
    MicDetection,
    PairingResult,
    TapEvent,
    pair_events,
)
from jasper.route_latency.status_socket import (
    FANIN_STATUS_SOCKET,
    OUTPUTD_STATUS_SOCKET,
    USBSINK_STATE_PATH,
    read_status_socket_or_none,
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

# Control-socket / state paths come from the shared status_socket module (one
# route-latency home for "where do I read route health"), imported above.

MIN_MATCH_RATE_DEFAULT = 0.90

# Warn (not fail) when the tap detected fewer than this fraction of the
# schedule's planned impulses. This is the schedule-vs-detected sanity check
# the click_track schedule exists for: it catches TAP-SIDE truncation (a
# daemon restart, an early auto-disarm, the WAV not played to completion) that
# match-rate cannot — a truncated tap window shrinks BOTH the paired count and
# the tap-side denominator, so match-rate can stay high while half the run is
# missing. Kept generous (mirrors the match-rate floor) because the tap fires
# at ingress before pairing, so some scheduled clicks legitimately fail to
# cross the tap threshold at a conservative volume; a LARGE deficit is the
# truncation signal, not a small one.
MIN_TAP_DETECT_RATE_DEFAULT = 0.90

# Counters where a nonzero change during the measurement window means the
# route was NOT healthy for the whole window — a latency number measured
# across an xrun/drop/resampler-unlock is not trustworthy evidence either way.
# This is a curated subset of the full snapshot (which is diffed and printed in
# full for transparency) rather than the only thing compared: any nonzero delta
# anywhere is worth an operator's eyes, but these are the ones this CLI
# explicitly calls out as "this alone means unhealthy."
#
# The set encodes the same "clean window" contract the HANDOFF names (see
# docs/HANDOFF-usb-low-latency.md "Only pass `--route-health-ok` when…"): no
# usbsink capture/playback xruns or underflow/overflow/drops, no fan-in USB
# resampler unlock/silence/overrun, and no outputd/fan-in xruns. The names are
# cross-checked against the Rust status serializers by
# `test_known_health_counter_names_exist_in_rust_status_json`, so a Rust-side
# rename fails loudly rather than silently degrading the verdict to
# vacuous-true.
#
# Two shapes, because the route's counters live at two kinds of location:
#   * KNOWN_HEALTH_COUNTER_PATHS — counters at a STABLE dict path (navigated to
#     a single dotted key). usbsink counters + the fan-in OUTPUT xrun + the
#     outputd content/DAC xruns.
#   * KNOWN_HEALTH_COUNTER_SUFFIXES — counters that live inside the fan-in
#     `inputs` ARRAY (per-lane xruns and the per-lane USB-resampler
#     unlock/silence/overrun). Their dotted path carries a lane INDEX
#     (`fanin.inputs.0.xrun_count`) that is not stable across a topology
#     change, so they are matched by dotted-path SUFFIX instead of exact path —
#     any lane's xrun/unlock/silence/overrun disqualifies. `_numeric_deltas`
#     recurses into lists so these are visible in `all_deltas`.
KNOWN_HEALTH_COUNTER_PATHS: tuple[tuple[str, ...], ...] = (
    ("usbsink", "counters", "capture_xruns"),
    ("usbsink", "counters", "playback_xruns"),
    ("usbsink", "counters", "underflow_periods"),
    ("usbsink", "counters", "overflow_events"),
    ("usbsink", "counters", "dropped_periods"),
    # fan-in output (post-mix ALSA loopback) xruns — a stable dict path.
    ("fanin", "output", "xrun_count"),
    # outputd content-capture and final-DAC xruns — stable dict paths.
    ("outputd", "content", "xrun_count"),
    ("outputd", "dac", "xrun_count"),
)

# Dotted-path suffixes matched against any array-indexed fan-in input lane. The
# fan-in STATUS `inputs` array carries per-lane `xrun_count` and, on the
# clock-crossing USB lane, a `resampler` object with `unlock_count` /
# `silence_frames` / `overrun_frames`. Any lane's nonzero delta on one of these
# disqualifies (the contract's "no fan-in USB resampler unlock/silence/overrun,
# and no outputd/fan-in xruns"). Suffix-matched because the lane index is not
# stable across topology changes; leaf names cross-checked against
# `rust/jasper-fanin/src/state.rs` by the contract test.
KNOWN_HEALTH_COUNTER_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("fanin", "inputs", "xrun_count"),
    ("fanin", "inputs", "resampler", "unlock_count"),
    ("fanin", "inputs", "resampler", "silence_frames"),
    ("fanin", "inputs", "resampler", "overrun_frames"),
)


# --------------------------------------------------------------------------
# Route-health snapshot (usbsink/fan-in/outputd) — honesty, not gate
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


def snapshot_route_health() -> dict[str, Any]:
    """Best-effort snapshot of the three route surfaces: usbsink (the Rust
    ingress daemon — its state.json also carries the impulse tap's counters),
    fan-in, and outputd.

    Fails soft per-surface: an unreachable daemon records `null` for that
    key rather than raising, so a snapshot taken before a daemon is up (or
    after it's gone) still captures whatever IS available. The fan-in/outputd
    `STATUS\n` sockets are read through the shared
    `jasper.route_latency.status_socket` helper (one owner of that protocol);
    the usbsink surface is a `state.json` FILE, read locally.
    """

    return {
        "captured_at_monotonic_ns": time.monotonic_ns(),
        "usbsink": _read_json_file(Path(USBSINK_STATE_PATH)),
        "fanin": read_status_socket_or_none(
            FANIN_STATUS_SOCKET, event="route_latency_harness.health_snapshot_unavailable"
        ),
        "outputd": read_status_socket_or_none(
            OUTPUTD_STATUS_SOCKET, event="route_latency_harness.health_snapshot_unavailable"
        ),
    }


# Numeric leaf keys that are timestamps, not health counters: they always
# change between two snapshots (that's their whole purpose) and a huge
# meaningless delta on them buries the counters that matter. Excluded from the
# printed/compared deltas by leaf-key name so the generic diff still catches
# any genuinely new counter a daemon adds.
_IGNORED_DELTA_LEAF_KEYS = frozenset(
    {
        "captured_at_monotonic_ns",  # this harness's own snapshot timestamp
        "last_progress_epoch_ms",  # usbsink liveness heartbeat
        "updated_at",  # usbsink state.json write time (string, but guard anyway)
    }
)


def _numeric_deltas(before: Any, after: Any, *, prefix: tuple[str, ...] = ()) -> dict[str, float]:
    """Recursively diff two JSON-like trees, returning {"a.b.c": after-before}
    for every leaf where both sides are numeric and the value changed.

    Generic on purpose: the usbsink/fan-in/outputd counter surfaces
    evolve independently of this harness, so hardcoding a fixed field list
    would silently stop reporting new counters. New/removed keys (a daemon
    added or removed a counter between snapshots) are skipped rather than
    treated as a numeric change — a schema change is not a health signal.
    Timestamp leaves (see `_IGNORED_DELTA_LEAF_KEYS`) are excluded by name:
    they always change and are noise in a health report.

    Recurses into LISTS as well as maps, using the element's integer index as
    the path component (`fanin.inputs.0.xrun_count`). The fan-in STATUS carries
    its per-lane counters (and the per-lane USB-resampler unlock/silence/overrun
    counters) inside the `inputs` array, so without list recursion those
    route-health counters would never appear in a delta at all. Only positional
    pairs present on BOTH sides are compared; a list that changed length (a lane
    appeared/disappeared — itself a topology change, not a counter tick) has its
    extra elements skipped, mirroring the map new/removed-key rule.
    """

    deltas: dict[str, float] = {}
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                continue
            deltas.update(_numeric_deltas(before[key], after[key], prefix=(*prefix, str(key))))
        return deltas
    if isinstance(before, list) and isinstance(after, list):
        for i in range(min(len(before), len(after))):
            deltas.update(_numeric_deltas(before[i], after[i], prefix=(*prefix, str(i))))
        return deltas
    if prefix and prefix[-1] in _IGNORED_DELTA_LEAF_KEYS:
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
        """True iff every KNOWN_HEALTH_COUNTER_PATHS delta is EXACTLY 0 AND
        every surface answered in both snapshots.

        Any nonzero delta on a known counter disqualifies — including a
        NEGATIVE one. These counters are per-process monotonic, so a delta
        below zero can only mean the daemon under test restarted mid-window
        (its counter reset to 0). A restart is by definition an unclean
        window — the audio path dropped out, the tap disarmed, and the JSONL
        can be lost on `RuntimeDirectory` teardown — so a "-7 capture_xruns"
        must NOT read as "cleaner than clean." (The earlier `delta <= 0`
        treated a restart as justifying the declaration, contradicting the
        `*** known counter ***` flag the printout puts on that same line.)

        This is advisory only — see class docstring and the CLI's printed
        caveat. A CLI that silently asserted this for the operator would
        be exactly the "auto-declare route health" failure mode the
        architecture brief and `route_latency_artifact` explicitly refuse.
        """

        if any(self.before.get(k) is None or self.after.get(k) is None for k in ("usbsink", "fanin", "outputd")):
            return False
        return all(delta == 0 for delta in self.known_counter_deltas.values())


def _dotted_key_matches_input_lane_suffix(key: str, suffix: tuple[str, ...]) -> bool:
    """True iff `key` is an array-indexed fan-in-input path matching `suffix`.

    A suffix like ``("fanin", "inputs", "resampler", "unlock_count")`` matches
    any dotted key of the form ``fanin.inputs.<N>.resampler.unlock_count`` where
    ``<N>`` is a numeric lane index — i.e. the fixed ``fanin.inputs`` head, then
    exactly one integer index component, then the remaining suffix components.
    Matching by shape (not by a fixed index) keeps the verdict correct across a
    topology change that reorders or adds lanes.
    """

    head = suffix[:2]  # ("fanin", "inputs")
    tail = suffix[2:]
    parts = key.split(".")
    if len(parts) != len(head) + 1 + len(tail):
        return False
    if tuple(parts[: len(head)]) != head:
        return False
    if not parts[len(head)].isdigit():  # the lane index
        return False
    return tuple(parts[len(head) + 1 :]) == tail


def diff_route_health(before: Mapping[str, Any], after: Mapping[str, Any]) -> RouteHealthReport:
    all_deltas = _numeric_deltas(dict(before), dict(after))
    # Stable dict-path counters: present-or-zero so a clean run still reports
    # each known counter (and the cross-check that the names exist stays honest).
    known: dict[str, float] = {
        ".".join(path): all_deltas.get(".".join(path), 0.0)
        for path in KNOWN_HEALTH_COUNTER_PATHS
    }
    # Array-indexed fan-in-input lane counters: fold in every observed delta
    # whose dotted path matches a per-lane suffix. Unlike the stable paths these
    # are only added when actually present (the lane index is not known ahead of
    # time), so a clean/absent lane contributes nothing — but any nonzero
    # per-lane xrun/unlock/silence/overrun lands here and disqualifies.
    for key, delta in all_deltas.items():
        if any(_dotted_key_matches_input_lane_suffix(key, s) for s in KNOWN_HEALTH_COUNTER_SUFFIXES):
            known[key] = delta
    return RouteHealthReport(
        all_deltas=all_deltas,
        known_counter_deltas=known,
        before=before,
        after=after,
    )


# --------------------------------------------------------------------------
# Egress (mic-side) detection over a live capture window
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MicCaptureResult:
    """Outcome of a mic capture window.

    `stopped_early` is True iff the mic source went quiet BEFORE the requested
    duration elapsed (after delivering at least one chunk). `elapsed_seconds`
    is how long the window actually ran, and `requested_seconds` what was
    asked — the caller surfaces the gap loudly so a promotion capture that
    died at minute 12 of 36 never looks like a quiet success.
    """

    detections: tuple[MicDetection, ...]
    stopped_early: bool
    elapsed_seconds: float
    requested_seconds: float


def capture_mic_detections(
    mic_spec: str,
    *,
    duration_seconds: float,
    threshold: float = DEFAULT_THRESHOLD,
    hysteresis: float = DEFAULT_HYSTERESIS,
    refractory_ms: float = DEFAULT_REFRACTORY_MS,
) -> MicCaptureResult:
    """Read the mic source for `duration_seconds`, returning every detected
    impulse re-anchored to its packet's arrival time plus early-stop metadata.

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
    capture gracefully with whatever was collected so far, but records
    `stopped_early=True` so the caller can report the truncation rather than
    hide it.
    """

    reader = build_mic_reader(mic_spec)
    detections: list[MicDetection] = []
    detector: StreamingDetector | None = None
    started = time.monotonic()
    deadline = started + duration_seconds
    received_any_chunk = False
    stopped_early = False
    try:
        while time.monotonic() < deadline:
            try:
                chunk = reader.read_chunk()
            except MicSourceUnavailableError:
                if received_any_chunk:
                    stopped_early = True
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
    return MicCaptureResult(
        detections=tuple(detections),
        stopped_early=stopped_early,
        elapsed_seconds=time.monotonic() - started,
        requested_seconds=duration_seconds,
    )


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


def latency_ms_for_match(match: MatchedImpulse, *, mic_distance_compensation_ms: float) -> float:
    """(t_mic_detect - t_tap_detect) - mic_distance_compensation_ms.

    The whole route latency lives in the `t_mic - t_tap` subtraction: the
    Rust tap timestamps each click at ingress (`run_audio_loop` runs the tap
    over the just-read period BEFORE `stage_capture_period` pushes it into
    the ring), and the click's entire physical journey — ring dwell,
    fan-in, CamillaDSP, outputd, DAC, air, mic — then elapses in real time
    between `t_tap` and `t_mic`. The ring dwell is therefore already inside
    the subtraction; adding `ring_fill_frames / 48.0` on top would count it
    twice (it inflated every sample by ~10.7 ms at the steady-state fill of
    2 periods).

    `match.tap.ring_fill_frames` is kept in the JSONL and available here as
    diagnostic context — the pre-read backlog the click had to drain
    through is genuinely useful when reading a run's per-impulse spread —
    but it is NOT added to the latency, because the subtraction already
    captures it. `mic_distance_compensation_ms` (CLI flag, default 0)
    subtracts the fixed acoustic travel time speaker→mic that is not part
    of the electrical route.
    """

    raw_ms = match.raw_delta_ns / 1_000_000.0
    return raw_ms - mic_distance_compensation_ms


@dataclass(frozen=True)
class AnalyzeResult:
    latencies_ms: tuple[float, ...]
    pairing: PairingResult
    match_rate_floor: float

    @property
    def match_rate_ok(self) -> bool:
        return self.pairing.match_rate >= self.match_rate_floor


def analyze_matches(
    tap_events: list[TapEvent],
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


def summarize_latencies(latencies_ms: tuple[float, ...]) -> dict[str, Any]:
    # Reuse the CERTIFYING percentile implementation so the harness's printed
    # p50/p95/p99 can never drift from what jasper-route-latency-artifact
    # actually gates on (nearest-rank; empty -> None).
    values = list(latencies_ms)
    return {
        "count": len(values),
        "p50_ms": nearest_rank_percentile(values, 0.50),
        "p95_ms": nearest_rank_percentile(values, 0.95),
        "p99_ms": nearest_rank_percentile(values, 0.99),
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
    try:
        wav_path = click_track.render_wav(schedule, out_dir / f"{args.preset}-click-track.wav")
        schedule_path = click_track.write_schedule_json(schedule, out_dir / f"{args.preset}-schedule.json")
    except OSError as e:
        # The default --out-dir lives under the root-owned /var/lib/jasper; a
        # bare `generate` run unprivileged (or on a laptop) can't create it.
        # generate needs no daemon/root — point the operator at --out-dir
        # instead of a raw traceback. Docs always pass an explicit --out-dir.
        print(
            f"error: could not write to {out_dir} ({e}). Pass --out-dir to a "
            "writable location, e.g. --out-dir ./route-latency (generate "
            "needs no root — it only writes a WAV + JSON schedule).",
            file=sys.stderr,
        )
        return 1
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

    def _disarm_quietly() -> None:
        # Best-effort: never let a disarm failure mask the real outcome. The
        # tap also auto-disarms after `auto_disarm_min`, so a missed disarm is
        # bounded even if this fails.
        try:
            client.disarm()
        except TapClientError as e:
            print(f"warning: disarm failed: {e}", file=sys.stderr)

    # Every exit path disarms: the success path snapshots `after` first (so the
    # window bounds the health diff) then disarms; both the unavailable-mic and
    # the KeyboardInterrupt (operator Ctrl-C mid-capture) paths disarm before
    # returning/re-raising, rather than leaving the tap armed until the
    # auto-disarm deadline.
    try:
        capture = capture_mic_detections(
            args.mic,
            duration_seconds=schedule.duration_seconds,
            threshold=args.mic_threshold,
            hysteresis=args.mic_hysteresis,
            refractory_ms=args.mic_refractory_ms,
        )
    except MicSourceUnavailableError as e:
        print(f"error: {e}", file=sys.stderr)
        _disarm_quietly()
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — disarming tap.", file=sys.stderr)
        _disarm_quietly()
        raise
    after = snapshot_route_health()
    _disarm_quietly()

    detections = list(capture.detections)
    mic_path = out_dir / "mic-detections.jsonl"
    write_mic_detections_jsonl(detections, mic_path)
    health_path = out_dir / "route-health-snapshot.json"
    health_path.write_text(json.dumps({"before": before, "after": after}, indent=2), encoding="utf-8")

    if capture.stopped_early:
        # Loud: a capture that died mid-window must never read as a quiet
        # success. Downstream honesty gates (match-rate floor, artifact
        # duration/percentile certification) still protect correctness, but
        # the operator needs the when/why here — this is an evidence tool.
        log_event(
            logger,
            "route_latency_harness.mic_source_stopped_early",
            elapsed_seconds=round(capture.elapsed_seconds, 1),
            requested_seconds=round(capture.requested_seconds, 1),
            detections=len(detections),
            level=logging.WARNING,
        )
        print(
            f"WARNING: mic source went quiet at t={capture.elapsed_seconds:.1f}s "
            f"of {capture.requested_seconds:.1f}s — the capture is TRUNCATED "
            f"({len(detections)} detections). Was the AEC bridge restarted, or "
            "the XVF3800 unplugged, mid-run? The samples file (if analyze emits "
            "one) will cover only the captured window.",
            file=sys.stderr,
        )

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

    # Schedule-vs-detected sanity check (the reason the generate step writes a
    # planned impulse_count): a tap-side truncation — daemon restart, early
    # auto-disarm, or a WAV not played to completion — shrinks BOTH the paired
    # count and the match-rate denominator, so match-rate can stay high while
    # much of the run is missing. Comparing the DETECTED tap count against the
    # SCHEDULED count is the catch match-rate structurally can't provide.
    _warn_if_tap_count_far_below_schedule(
        detected_tap_events=len(tap_events),
        expected_impulse_count=args.expected_impulse_count,
        min_tap_detect_rate=args.min_tap_detect_rate,
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
        try:
            raw = json.loads(health_snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raw = None
            print(
                f"warning: could not read route-health snapshot "
                f"{health_snapshot_path} ({e}); skipping the health report. "
                "The samples file is unaffected.",
                file=sys.stderr,
            )
        if isinstance(raw, dict):
            health_report = diff_route_health(raw.get("before") or {}, raw.get("after") or {})
            _print_health_report(health_report)
        elif raw is not None:
            print(
                f"warning: route-health snapshot {health_snapshot_path} is not "
                "a JSON object; skipping the health report.",
                file=sys.stderr,
            )

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


def _warn_if_tap_count_far_below_schedule(
    *,
    detected_tap_events: int,
    expected_impulse_count: int | None,
    min_tap_detect_rate: float,
) -> bool:
    """Loudly flag a tap-side-truncated run; return True iff a warning fired.

    When `expected_impulse_count` is known (from the generate schedule) and the
    tap detected fewer than `min_tap_detect_rate` of it, emit an `event=` WARN
    log plus a stderr note naming the likely truncation causes. This is a
    warning, not a hard fail: the tap fires at ingress before pairing, so at a
    conservative playback volume some scheduled clicks legitimately miss the
    tap threshold — a LARGE deficit is the truncation signal. A no-op when the
    count is unknown or `expected_impulse_count <= 0`.
    """

    if not expected_impulse_count or expected_impulse_count <= 0:
        return False
    detect_rate = detected_tap_events / expected_impulse_count
    if detect_rate >= min_tap_detect_rate:
        return False
    log_event(
        logger,
        "route_latency_harness.tap_count_below_schedule",
        detected_tap_events=detected_tap_events,
        expected_impulse_count=expected_impulse_count,
        detect_rate=round(detect_rate, 3),
        min_tap_detect_rate=min_tap_detect_rate,
        level=logging.WARNING,
    )
    print(
        f"WARNING: the tap detected {detected_tap_events} impulses but the "
        f"schedule planned {expected_impulse_count} "
        f"({detect_rate:.1%}, below the {min_tap_detect_rate:.1%} floor). This "
        "usually means the tap window was TRUNCATED — the WAV was not played "
        "to completion, the daemon restarted, or the tap auto-disarmed mid-run. "
        "match-rate can look fine on a truncated run (missing taps shrink its "
        "denominator too), so treat any samples file from this run as covering "
        "only a partial window.",
        file=sys.stderr,
    )
    return True


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


ARTIFACT_CLI_NAME = "jasper-route-latency-artifact"


def _resolve_artifact_cli() -> str:
    """Resolve the sibling `jasper-route-latency-artifact` executable.

    Under the documented invocation (`sudo /opt/jasper/.venv/bin/
    jasper-route-latency-harness ...`), the venv's `bin/` is NOT on sudo's
    PATH/`secure_path`, so a bare `subprocess.run(["jasper-route-latency-
    artifact"])` dies with `FileNotFoundError`. The artifact CLI is a console
    entry point installed right next to this one, so we look in the same
    directory as `sys.executable` (the venv's python → its `bin/`) first, then
    the dir this script was launched from, and only then fall back to PATH.
    Returns a name/path suitable for `subprocess.run`; the bare name is the
    last resort (kept so a PATH-based dev invocation still works).
    """

    import shutil

    for base in (Path(sys.executable).parent, Path(sys.argv[0]).resolve().parent):
        candidate = base / ARTIFACT_CLI_NAME
        if candidate.is_file():
            return str(candidate)
    return shutil.which(ARTIFACT_CLI_NAME) or ARTIFACT_CLI_NAME


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
        _resolve_artifact_cli(),
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
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        print(
            f"error: could not find {ARTIFACT_CLI_NAME} next to this CLI, on "
            "PATH, or as a bare command. The samples file is already written "
            f"({samples_path}); run it by hand, e.g.\n"
            f"  sudo /opt/jasper/.venv/bin/{ARTIFACT_CLI_NAME} "
            f"--samples {samples_path} --duration-seconds {duration_seconds:g} "
            f"--harness-id {HARNESS_ID}",
            file=sys.stderr,
        )
        return 1
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
    # Feed the schedule's planned impulse count into analyze so its
    # schedule-vs-detected tap sanity check fires automatically on a `run`
    # (the standalone `analyze` path takes --expected-impulse-count explicitly).
    args.expected_impulse_count = schedule.impulse_count
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
    parser.add_argument("--expected-impulse-count", type=int, default=None, help="Scheduled impulse count (from the generate schedule). analyze warns if the tap detected far fewer — the tap-side-truncation catch (`run` sets this automatically).")
    parser.add_argument("--min-tap-detect-rate", type=float, default=MIN_TAP_DETECT_RATE_DEFAULT, help=f"Warn (not fail) if detected tap events fall below this fraction of --expected-impulse-count (default {MIN_TAP_DETECT_RATE_DEFAULT * 100:g} pct); catches a truncated tap window that match-rate alone can hide.")
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
    "ARTIFACT_CLI_NAME",
    "HARNESS_ID",
    "AnalyzeResult",
    "MicCaptureResult",
    "RouteHealthReport",
    "analyze_matches",
    "capture_mic_detections",
    "diff_route_health",
    "MIN_MATCH_RATE_DEFAULT",
    "MIN_TAP_DETECT_RATE_DEFAULT",
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
