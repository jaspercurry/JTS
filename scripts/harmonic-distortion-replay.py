#!/usr/bin/env python3
"""Harmonic-distortion replay: read H2/H3 out of banked MEASURE captures.

**The question it answers, and the one it cannot.** Every JTS measurement sweep
is Novak-synchronized, so each capture has always carried the speaker's harmonic
distortion at exact pre-arrival offsets — nobody had ever looked. This tool
looks, using :mod:`jasper.audio_measurement.distortion`. What it CANNOT give is
an absolute level: the corpus records no SPL anywhere, so every number here is
"dB below the fundamental, at the drive this capture used", and the drive is
reported beside it in dBFS. A distortion figure without its level is unreadable,
so the two are never printed apart.

**Why the production window cannot be used.**
``program_analysis.DECONV_PRE_GUARD_S`` is 0.25 s; the H3 image leads the linear
IR by ``L·ln 3`` ≈ 1.34 s on a MEASURE woofer sweep. Deconvolution is circular,
so at the production window every image is wrapped off the front. The product
module re-deconvolves at :func:`~jasper.audio_measurement.distortion.required_pre_guard_s`
— an analysis-side value for a parameter ``_deconvolve_window`` already exposes.
Nothing production does changes.

**Why the replay validates itself.** Two independent gates, both refusing rather
than reporting:

1. **Program identity.** The program is rebuilt from the round's own banked
   ``gain_plan_db`` and the driver bands, and its ``program_id`` must equal the
   id the session recorded. That is a SHA-256 over the full segment schedule, so
   a match means the reconstructed stimulus — hence every ``L`` the harmonic
   offsets are derived from — is exactly the one that played. The session
   volume is not banked anywhere, so it is SOLVED against that same id over a
   bounded grid (:data:`DOWNSTREAM_GRID_DB`); a solve verified by a hash is
   worth more than an operator's assertion.
2. **Analysis fidelity.** The shipped ``analyze_program_capture`` is re-run and
   its :data:`FIDELITY_FIELDS` compared against the sidecar's own ``diagnostic``
   block — the analysis AS PERFORMED. Only then is the distortion read trusted,
   because it rides that analysis's located anchors and clock-drift estimate.
   Fail-closed: a sidecar carrying NONE of the gate fields is refused (zero
   comparisons is not a passed gate), a partial block is compared on exactly
   the fields it has, and the summary prints the count actually compared.

Captures are bound to sidecars by ``wav_sha256``, never by filename.

Usage (laptop, from the repo root)::

    .venv/bin/python scripts/harmonic-distortion-replay.py \\
        --state captures/xover-series2-2026-08-17/series2-state-r1b-preapply.json \\
        --captures captures/xover-series2-2026-08-17/e0-r1b \\
        --dumps captures/xover-series2-2026-08-17/dumps-r1b \\
        --calibration captures/flat-linearization-20260725/umik2-cal/umik2-b7343c0c625b.txt

Exit code is 1 if any capture fails a gate, so a drifted replay cannot be
mistaken for a finding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# Session volume is not recorded in any banked artifact, so it is solved
# against the program id. -40..0 dB in 0.5 dB steps covers every session volume
# the flow can compose (the composer clamps to non-positive), and the solve is
# ~80 program builds, well under a second.
DOWNSTREAM_GRID_DB = np.arange(-40.0, 0.01, 0.5)

# Sidecar diagnostics compared against the replay. Each is a value the banked
# analysis recorded about ITSELF, so a mismatch means the reconstruction reads
# different bytes than the session did. Scoped to what a DISTORTION read
# actually rides on — the clock-drift estimate divided out of the reference, the
# repeat agreement that says the schedule was located consistently, and the
# integrity flags — rather than copying a longer list whose extra fields this
# tool never consumes.
#
# `max_residual_samples` and `glitch_detected` are deliberately ABSENT, both for
# the same reason. D7 (`b98e9380f`, 2026-08-18) replaced the estimator behind
# them: the old one took `int(np.argmax(...))` of a dry stimulus against a
# room-convolved capture, whose argmax hops between early lobes, so the desync
# threshold sat below the resolution of its own estimator. Every capture banked
# before that commit records a value from the blunter instrument (0.333 on
# series-2 r1b) than a current replay produces (0.061 — inside the 0.022-0.110
# range D7's own re-derivation reports for physically-clean captures), and the
# eight captures that guard wrongly rejected still carry `glitch_detected: true`.
# Comparing either would report a deliberate product improvement as a broken
# reconstruction, which is the misattribution these gates exist to prevent.
# A banked-vs-replay disagreement on `glitch_detected` is DISCLOSED per capture
# (:func:`glitch_disclosure`) rather than silently dropped, because reading a
# capture the session rejected is a fact the reader is owed.
#
# `alignment_confidence` / `anchor_delay_us` are absent for a different reason:
# they are not deterministic across re-analyses of ONE capture (the six
# `*_measure_*.json` sidecars in dumps-r1b share a wav_sha256 and disagree on
# both), so they cannot be a fidelity signal for anything. This read uses
# neither.
FIDELITY_FIELDS = (
    "epsilon_ppm",
    "woofer_repeat_epsilon_ppm",
    "tweeter_repeat_epsilon_ppm",
    "repeat_level_delta_db",
    "linearity_ok",
    "pilot_snr_ok",
    "woofer_validity_floor_hz",
    "tweeter_validity_floor_hz",
    "woofer_gate_window_ms",
    "tweeter_gate_window_ms",
)
FIDELITY_TOLERANCE = 5e-3

# Where the read is reported. A fixed ladder rather than a per-role grid so the
# two drivers' rows line up in the overlap; probes outside a role's own band are
# skipped, and probes past an ORDER's band edge print as a dash.
PROBE_FREQUENCIES_HZ = (
    150.0, 200.0, 300.0, 400.0, 600.0, 800.0, 1000.0,
    1500.0, 2000.0, 3000.0, 4000.0, 6000.0, 8000.0,
)


@dataclass(frozen=True)
class Capture:
    """One banked WAV and the sidecar bound to it by content."""

    wav: Path
    sidecar: dict[str, Any]
    phase: str


def load_mono(path: Path) -> np.ndarray:
    with wave.open(str(path)) as handle:
        channels, frames = handle.getnchannels(), handle.getnframes()
        width = handle.getsampwidth()
        raw = handle.readframes(frames)
    # Width from the container's own fmt chunk, never assumed: the dump ring
    # holds 16-bit phone captures AND 32-bit wired captures (#2662 W2b), and
    # decoding 32-bit frames as int16 re-strides every sample — the fidelity
    # gate then misreports the garbage as drift, the exact misattribution
    # this replay exists to prevent. Unsupported widths fail loudly
    # (scripts/_wav_stats.py precedent), never as a wrong analysis.
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 2**15
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2**31
    else:
        raise ValueError(
            f"{path.name}: unsupported {width * 8}-bit WAV — this replay reads "
            "the 16-bit (phone) and 32-bit (wired) captures the dump ring holds"
        )
    return samples[::channels] if channels > 1 else samples


def sign_convention(calibration_id: str) -> str:
    """How to read this session's calibration file — asked of the product's own
    registry rather than pinned here.

    A vendor file states either the mic's RESPONSE or a CORRECTION; the two
    differ by a sign, and getting it backwards moves every magnitude without
    moving one timing diagnostic. Same recovery
    ``scripts/severed-twin-replay.py`` performs, for the same reason.
    """
    from jasper.audio_measurement.calibration import (
        DEFAULT_SIGN_CONVENTION,
        SUPPORTED_MODELS,
    )

    parts = set(str(calibration_id).split("-"))
    for key, spec in SUPPORTED_MODELS.items():
        if key in parts:
            return str(spec["sign_convention"])
    return DEFAULT_SIGN_CONVENTION


def rebuild_program(state: dict[str, Any], bands: dict[str, tuple[float, float]]):
    """The round's MEASURE program, verified against its banked ``program_id``.

    Returns ``(program, downstream_gain_db)``. Raises ``SystemExit`` when no
    grid point reproduces the id — a reconstruction that cannot prove itself
    must not be read, because every harmonic offset derives from the sweep
    ``L`` this program carries.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        COURTESY_PRELUDE_ENABLED,
        PILOT_LEVEL_DELTA_DB,
    )
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
    )

    gains = state.get("gain_plan_db") or {}
    want = ((state.get("candidate") or {}) or {}).get("program_id")
    if not want:
        raise SystemExit(
            "state carries no candidate.program_id — point --state at the "
            "round's *-preapply.json, which records it"
        )
    if not {"woofer", "tweeter"} <= set(gains):
        raise SystemExit("state carries no woofer/tweeter gain_plan_db")
    roles = (
        RoleBand("woofer", 0, FrequencyBand(*bands["woofer"])),
        RoleBand("tweeter", 1, FrequencyBand(*bands["tweeter"])),
    )
    for downstream in DOWNSTREAM_GRID_DB:
        program = build_measure_program(
            {role: float(gains[role]) for role in ("woofer", "tweeter")},
            roles,
            downstream_gain_db=float(downstream),
            leading_pilot_gains_db=(
                float(gains["woofer"]) - PILOT_LEVEL_DELTA_DB,
                float(gains["woofer"]),
            ),
            leading_pilot_role="woofer",
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )
        if program.program_id == want:
            return program, float(downstream)
    raise SystemExit(
        f"no session volume in [{DOWNSTREAM_GRID_DB[0]}, {DOWNSTREAM_GRID_DB[-1]}] dB "
        f"reproduces program_id {want[:12]} at bands {bands}. Either the driver "
        f"bands are wrong or this state does not describe a MEASURE round."
    )


def bind_captures(captures: Path, dumps: Path, phases: set[str]) -> list[Capture]:
    """Every WAV under ``captures`` whose sha256 a sidecar under ``dumps`` claims.

    A content binding: the filename is read for nothing but globbing. Sidecars
    are deduplicated by ``wav_sha256`` — a corpus may hold several re-analyses of
    one capture, and they are one capture, not several.
    """
    by_sha: dict[str, dict[str, Any]] = {}
    for path in sorted(dumps.glob("*.json")):
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        sha = meta.get("wav_sha256")
        if isinstance(sha, str) and sha not in by_sha:
            by_sha[sha] = meta
    found: list[Capture] = []
    for wav in sorted(captures.glob("*.wav")):
        sha = hashlib.sha256(wav.read_bytes()).hexdigest()
        meta = by_sha.get(sha)
        if meta is None:
            continue
        phase = str(meta.get("phase") or "")
        if phase in phases:
            found.append(Capture(wav=wav, sidecar=meta, phase=phase))
    return found


def fidelity_failures(summary: dict[str, Any], want: dict[str, Any]) -> list[str]:
    """Which :data:`FIDELITY_FIELDS` the replay failed to reproduce.

    A field the sidecar does not record is not compared — the bank predates some
    of them — but a field it records and the replay omits IS a failure, because
    that is the reconstruction losing something the session had.
    """
    failures = []
    for field in FIDELITY_FIELDS:
        if field not in want or want[field] is None:
            continue
        mine, theirs = summary.get(field), want[field]
        if mine is None:
            failures.append(f"{field}: replay produced nothing, banked {theirs!r}")
        elif isinstance(theirs, bool) or isinstance(mine, bool):
            if bool(mine) != bool(theirs):
                failures.append(f"{field}: replay {mine!r}, banked {theirs!r}")
        elif isinstance(theirs, (int, float)) and isinstance(mine, (int, float)):
            if abs(float(mine) - float(theirs)) > FIDELITY_TOLERANCE:
                failures.append(f"{field}: replay {mine!r}, banked {theirs!r}")
        elif mine != theirs:
            failures.append(f"{field}: replay {mine!r}, banked {theirs!r}")
    return failures


def glitch_disclosure(summary: dict[str, Any], want: dict[str, Any]) -> str | None:
    """A note when the bank and the replay disagree about capture integrity.

    Not a gate (see :data:`FIDELITY_FIELDS`), but not nothing either: a capture
    the session REJECTED and this replay reads as clean is being included on the
    strength of D7's re-derivation, and the reader should be told which ones
    those are rather than discovering it from a capture count.
    """
    banked, replayed = want.get("glitch_detected"), summary.get("glitch_detected")
    if banked is None or replayed is None or bool(banked) == bool(replayed):
        return None
    if bool(banked) and not bool(replayed):
        return ("banked glitch_detected=true, replay says false — rejected by "
                "the pre-D7 desync guard, read here on D7's re-derivation")
    return "banked glitch_detected=false, replay says TRUE — read with suspicion"


def read_capture(program, capture_samples, calibration, orders, fc_hz, sidecar):
    """Gate one capture, then read every sweep segment's distortion.

    Returns ``(readings, failures, disclosure, compared)`` where ``compared``
    is how many of :data:`FIDELITY_FIELDS` the sidecar actually carried for
    comparison. ``readings`` is empty when the gate failed — a capture whose
    analysis does not reproduce is not evidence about a speaker — and a
    sidecar carrying NONE of the gate fields is refused outright rather than
    read ungated: zero comparisons is not a passed gate.
    """
    from jasper.audio_measurement import deconv
    from jasper.audio_measurement.distortion import read_segment_distortion
    from jasper.audio_measurement.program import KIND_SWEEP
    from jasper.audio_measurement.program_analysis import (
        CAPTURE_BOUND_MARGIN_S,
        MeasurementGeometry,
        MeasurementPriors,
        _estimate_drift,
        _global_offset,
        _locate_segments,
        analysis_diagnostic_summary,
        analyze_program_capture,
    )

    banked = sidecar.get("diagnostic") or {}
    compared = sum(
        1 for field in FIDELITY_FIELDS
        if field in banked and banked[field] is not None
    )
    if compared == 0:
        return [], [
            "sidecar carries none of the gate's diagnostic fields — nothing "
            "to compare means nothing was validated, so the capture is "
            "refused rather than read ungated"
        ], None, 0

    rate = program.sample_rate_hz
    analysis = analyze_program_capture(
        program, capture_samples, rate,
        calibration=calibration,
        geometry=MeasurementGeometry(),
        priors=MeasurementPriors(crossover_fc_hz=fc_hz),
    )
    summary = analysis_diagnostic_summary(analysis)
    failures = fidelity_failures(summary, banked)
    if failures:
        return [], failures, None, compared
    disclosure = glitch_disclosure(summary, banked)

    # The same bounding `analyze_program_capture` applies before locating, so
    # the anchors below are the anchors it used.
    bounded = deconv.cap_capture_length(
        capture_samples,
        sweep_len=program.total_samples,
        sample_rate=rate,
        max_capture_seconds=program.total_samples / rate + CAPTURE_BOUND_MARGIN_S,
    )
    global_offset, _first, stimuli, _ambiguous = _global_offset(program, bounded, rate)
    locations = _locate_segments(program, bounded, rate, global_offset, stimuli)
    epsilon = _estimate_drift(program, bounded, rate, locations).epsilon_ppm / 1e6

    readings = []
    for segment in program.stimulus_segments():
        if segment.kind != KIND_SWEEP:
            continue
        readings.append(
            read_segment_distortion(
                program, bounded, segment.segment_id,
                global_offset + segment.start_sample,
                orders=orders, calibration=calibration, epsilon=epsilon,
                level_notes={
                    "wav_sha256_12": str(sidecar.get("wav_sha256", ""))[:12],
                    "phase": sidecar.get("phase"),
                    "session_volume_note": "session volume folded into "
                                           "effective_peak_dbfs; no SPL banked",
                },
            )
        )
    return readings, [], disclosure, compared


def _pooled(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def report(all_readings, orders) -> dict[str, Any]:
    """Print the per-role read and return it as a JSON-able structure."""
    from jasper.audio_measurement.distortion import worst_clear_of_floor

    by_role: dict[str, list] = {}
    for reading in all_readings:
        by_role.setdefault(reading.role or "?", []).append(reading)

    print("\nEvery H-N value is dB BELOW THE FUNDAMENTAL at the same "
          "excitation frequency (more negative = cleaner); floors are in the "
          "same units. fundΔ is the pooled fundamental re its own band "
          "median: a dip there inflates the ratios on that row with no "
          "change in harmonic energy, so read ratio peaks against it.")
    out: dict[str, Any] = {
        "reference": {
            "harmonic_db": "dB below the fundamental at the same excitation "
                           "frequency; floors identical",
            "fundamental_re_band_median_db": "pooled fundamental minus its "
                                             "own band median (a notch here "
                                             "inflates the row's ratios)",
            "drive_dbfs": "stimulus/effective re digital full scale; capture "
                          "re the capture device's full scale; no SPL exists "
                          "in this corpus",
        },
        "roles": {},
    }
    for role, readings in sorted(by_role.items()):
        first = readings[0]
        drives = [r.drive for r in readings]
        # Pooling below is BY GRID INDEX, valid because every sweep of one
        # role shares one SweepMeta, hence one FFT length and one masked grid.
        # Checked, because pooling by index silently lies otherwise.
        if not all(
            np.array_equal(r.freqs_hz, first.freqs_hz) for r in readings
        ):
            raise SystemExit(f"role {role}: sweep grids disagree; cannot pool")
        # np.median partitions NaN to the end of each slice and inspects the
        # last slot, so a bin where ANY sweep is NaN pools to NaN. Here a bin
        # is NaN for every sweep or for none (the mask is a pure function of
        # the shared meta), so pooled bins are either fully real or NaN.
        fund_pool = np.median(
            np.stack([r.fundamental_db for r in readings]), axis=0
        )
        fund_delta = fund_pool - float(np.median(fund_pool))
        print(f"\n=== {role}  ({len(readings)} sweeps from "
              f"{len({r.drive.notes['wav_sha256_12'] for r in readings if r.drive.notes})} captures)")
        print(f"    sweep {first.sweep.f1:.0f}-{first.sweep.f2:.0f} Hz, L={first.sweep.L:.4f} s; "
              f"read over {first.band_hz[0]:.0f}-{first.band_hz[1]:.0f} Hz "
              f"(band-edge trim up to H{min(orders)}'s f2/{min(orders)} limit; "
              f"H{max(orders)} ends at {first.sweep.f2 / max(orders):.0f} Hz)")
        print(f"    drive: stimulus {_pooled([d.stimulus_peak_dbfs for d in drives]):.2f} dBFS, "
              f"effective {_pooled([d.effective_peak_dbfs for d in drives]):.2f} dBFS, "
              f"capture peak {_pooled([d.capture_peak_dbfs for d in drives]):.2f} dBFS")
        print(f"    harmonic-window clearance: "
              f"{min(r.clearance_s for r in readings):+.3f} s worst "
              f"({'all clean' if all(r.images_clean for r in readings) else 'CONTAMINATED'})")

        header = ("      Hz   fundΔ "
                  + "".join(f"   H{o} dB  floor" for o in orders) + "    THD%")
        print(header)
        rows = []
        for probe in PROBE_FREQUENCIES_HZ:
            if not (first.band_hz[0] <= probe <= first.band_hz[1]):
                continue
            i0 = int(np.argmin(np.abs(first.freqs_hz - probe)))
            row: dict[str, Any] = {
                "hz": probe,
                "fundamental_re_band_median_db": round(float(fund_delta[i0]), 1),
            }
            cells = [f" {float(fund_delta[i0]):+6.1f} "]
            for order in orders:
                vals, floors, limited = [], [], []
                for r in readings:
                    i = int(np.argmin(np.abs(r.freqs_hz - probe)))
                    vals.append(float(r.relative_db[order][i]))
                    floors.append(float(r.floor_relative_db[order][i]))
                    limited.append(bool(r.floor_limited(order)[i]))
                value, floor = _pooled(vals), _pooled(floors)
                at_floor = sum(limited) > len(limited) / 2
                # NaN means "past this order's own band edge" — printed as a
                # dash so it can never be read as a very clean measurement.
                beyond = not math.isfinite(value)
                row[f"h{order}_below_fundamental_db"] = (
                    None if beyond else round(value, 1)
                )
                row[f"h{order}_floor_below_fundamental_db"] = (
                    None if beyond else round(floor, 1)
                )
                row[f"h{order}_floor_limited"] = None if beyond else at_floor
                cells.append(
                    f" {'—':>7} {'—':>6}" if beyond
                    else f" {value:+7.1f}{'*' if at_floor else ' '}{floor:+6.1f}"
                )
            thd = _pooled([
                float(r.thd_percent[int(np.argmin(np.abs(r.freqs_hz - probe)))])
                for r in readings
            ])
            row["thd_percent"] = None if not math.isfinite(thd) else round(thd, 3)
            cells.append(f"  {'—':>6}" if not math.isfinite(thd) else f"  {thd:6.3f}")
            print(f"    {probe:6.0f}  " + "".join(cells))
            rows.append(row)

        worst, floor_fraction = {}, {}
        # The summary pools per grid point exactly as the table rows do —
        # median value, majority floor-limited vote — so the headline cannot
        # contradict its own table, and a per-sweep worst() cannot headline
        # an isolated excursion the other sweeps vote down as floor noise.
        # Eligibility is `worst_clear_of_floor`'s — the module owns the
        # policy; this report only supplies pooled arrays.
        for order in orders:
            values = np.median(
                np.stack([r.relative_db[order] for r in readings]), axis=0
            )
            limited = np.stack(
                [r.floor_limited(order) for r in readings]
            ).sum(axis=0) > len(readings) / 2
            share = float(np.mean(limited))
            floor_fraction[f"h{order}"] = round(share, 3)
            f, v = worst_clear_of_floor(first.freqs_hz, values, limited)
            if math.isfinite(v):
                index = int(np.argmin(np.abs(first.freqs_hz - f)))
                fd = float(fund_delta[index])
                worst[f"h{order}"] = {
                    "hz": round(f, 1),
                    "below_fundamental_db": round(v, 1),
                    "fundamental_re_band_median_db": round(fd, 1),
                }
                print(f"    worst H{order} ratio clear of the floor: {v:+.1f} dB "
                      f"at {f:.0f} Hz, where fundΔ is {fd:+.1f} dB  "
                      f"({100 * share:.1f}% of the band is floor-limited)")
                # The ratio's worst bin can be the DENOMINATOR's doing (a
                # fundamental notch), so the bin where the harmonic's own
                # energy peaks is named beside it — same eligibility owner,
                # argmax over the absolute curve fundamental + ratio.
                fa, _level = worst_clear_of_floor(
                    first.freqs_hz, fund_pool + values, limited
                )
                ia = int(np.argmin(np.abs(first.freqs_hz - fa)))
                worst[f"h{order}"]["absolute_energy_peak"] = {
                    "hz": round(fa, 1),
                    "below_fundamental_db": round(float(values[ia]), 1),
                    "fundamental_re_band_median_db": round(
                        float(fund_delta[ia]), 1
                    ),
                }
                print(f"      highest absolute H{order} energy clear of the "
                      f"floor: {fa:.0f} Hz (ratio {float(values[ia]):+.1f} dB, "
                      f"fundΔ {float(fund_delta[ia]):+.1f} dB)")
            else:
                worst[f"h{order}"] = None
                print(f"    worst H{order}: NOT MEASURABLE — {100 * share:.1f}% of "
                      f"the band is at the measurement floor")
        out["roles"][role] = {
            "sweeps": len(readings),
            "floor_limited_fraction": floor_fraction,
            "band_hz": [round(x, 1) for x in first.band_hz],
            "sweep_hz": [first.sweep.f1, first.sweep.f2],
            "stimulus_peak_dbfs": round(_pooled([d.stimulus_peak_dbfs for d in drives]), 2),
            "effective_peak_dbfs": round(_pooled([d.effective_peak_dbfs for d in drives]), 2),
            "capture_peak_dbfs": round(_pooled([d.capture_peak_dbfs for d in drives]), 2),
            "worst_clear_of_floor": worst,
            "rows": rows,
        }
    print("\n  * = a majority of the sweeps read this point within "
          "FLOOR_LIMITED_MARGIN_DB (6 dB) of their own per-sweep floors — an "
          "upper bound on the driver, not a reading of it.")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", type=Path, required=True,
                        help="round state JSON carrying gain_plan_db + candidate.program_id")
    parser.add_argument("--captures", type=Path, required=True, help="dir of capture WAVs")
    parser.add_argument("--dumps", type=Path, required=True, help="dir of sidecar JSON")
    parser.add_argument("--calibration", type=Path, default=None, help="mic calibration text")
    parser.add_argument("--orders", default="2,3", help="harmonic orders (default 2,3)")
    parser.add_argument("--phases", default="measure,lateral",
                        help="sidecar phases to read (default measure,lateral)")
    parser.add_argument("--woofer-band", default="150:4000", help="woofer band lo:hi Hz")
    parser.add_argument("--tweeter-band", default="1600:20000", help="tweeter band lo:hi Hz")
    parser.add_argument("--limit", type=int, default=0, help="stop after N captures")
    parser.add_argument("--json", type=Path, default=None, help="write the report as JSON")
    args = parser.parse_args()

    orders = tuple(int(x) for x in args.orders.split(","))
    bands = {
        "woofer": tuple(float(x) for x in args.woofer_band.split(":")),
        "tweeter": tuple(float(x) for x in args.tweeter_band.split(":")),
    }
    state = json.loads(args.state.read_text())
    program, downstream = rebuild_program(state, bands)
    print(f"program_id {program.program_id[:12]} reproduced "
          f"(session volume solved: {downstream:+.1f} dB)")

    preset = ((state.get("pre_apply_profile") or {}).get("recomposition_snapshot")
              or {}).get("preset") or {}
    regions = preset.get("crossover_regions") or [{}]
    fc_hz = float(regions[0].get("fc_hz") or 0.0) or None
    if fc_hz is None:
        raise SystemExit("state carries no crossover fc_hz; MEASURE analysis needs it")

    captures = bind_captures(args.captures, args.dumps, set(args.phases.split(",")))
    if args.limit:
        captures = captures[: args.limit]
    if not captures:
        raise SystemExit(
            f"no WAV under {args.captures} was claimed by a sidecar under "
            f"{args.dumps} for phases {args.phases}"
        )
    print(f"{len(captures)} capture(s) bound by wav_sha256, fc={fc_hz:.1f} Hz")

    calibration = None
    if args.calibration is not None:
        from jasper.audio_measurement.calibration import parse_calibration_text

        convention = sign_convention(
            str(captures[0].sidecar.get("setup_calibration_id") or "")
        )
        calibration = parse_calibration_text(
            args.calibration.read_text(), sign_convention=convention
        )
        print(f"calibration {args.calibration.name} read as '{convention}' "
              f"({len(calibration.freqs_hz)} points), applied to the fundamental "
              f"at f and to H-N at N·f")
    else:
        print("NO calibration supplied: harmonic-to-fundamental ratios carry the "
              "mic's own C(N·f) - C(f) tilt")

    all_readings, gate_failures, disclosures, compared_ok = [], 0, [], []
    for capture in captures:
        readings, failures, disclosure, compared = read_capture(
            program, load_mono(capture.wav), calibration, orders, fc_hz,
            capture.sidecar,
        )
        if failures:
            gate_failures += 1
            print(f"\n{capture.wav.name}: FIDELITY FAILED — not reporting a read:")
            for line in failures:
                print(f"    {line}")
            continue
        if disclosure:
            disclosures.append(f"{capture.wav.name}: {disclosure}")
        compared_ok.append(compared)
        all_readings.extend(readings)

    if not all_readings:
        print("\nno capture passed its gate")
        return 1
    # The count printed is what was actually compared, never the field-list
    # length: a sidecar carrying zero gate fields is refused above, and a
    # partial one passes on exactly the fields it has.
    spread = (
        f"{min(compared_ok)}" if min(compared_ok) == max(compared_ok)
        else f"{min(compared_ok)}-{max(compared_ok)}"
    )
    print(f"\n{len(compared_ok)}/{len(captures)} captures reproduced every "
          f"banked diagnostic they carry ({spread} of {len(FIDELITY_FIELDS)} "
          f"gate fields per capture); {len(all_readings)} sweeps read")
    if disclosures:
        print(f"\n{len(disclosures)} capture(s) disagree with the bank on integrity:")
        for line in disclosures:
            print(f"    {line}")
    payload = report(all_readings, orders)
    if args.json is not None:
        payload["program_id"] = program.program_id
        payload["session_volume_db"] = downstream
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"\nwrote {args.json}")
    return 1 if gate_failures else 0


if __name__ == "__main__":
    sys.exit(main())
