#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render the honest metric views for every banked crossover-v2 receipt in a
capture bundle, beside the pooled number the product actually shipped.

LAB TOOLING. Reads gitignored `captures/` evidence, prints a report, writes
nothing back into the bundle. Every MEASUREMENT here comes from
`jasper.active_speaker.flat_spec_views`, which is product code, so this tool
cannot compute a residual, offset, or weight the product will not later
compute the same way. What lives here is the part that IS lab-only — walking a
receipts tree, rehydrating a persisted report, joining an arm's positions to
the walk log that drove them, and the two subtractions this table wants and no
consumer does: the `gap` column and the bins-per-octave `ratio`. Both are
differences of numbers the views already publish, and both carry a display
choice — which "other" role to put in a single column when a cloud sampled
several — that would turn into product policy if it moved onto a result type.

Usage:

    PYTHONPATH=. .venv/bin/python scripts/render-metric-views.py \\
        captures/xover-armrun-2026-08-18/receipts

    # ...with microphone angles recovered from the run's own walk logs:
    PYTHONPATH=. .venv/bin/python scripts/render-metric-views.py \\
        captures/xover-armrun-2026-08-18/receipts \\
        --walk-logs captures/xover-armrun-2026-08-18/logs \\
        --json /tmp/metric-views.json

The question it answers, in one line: the shipped pooled residual grades on a
LINEAR frequency axis and pools every microphone position equally, so it
overweights the top octave about 12:1 per octave against the midrange and
lets a coverage-edge position drag the number a listening seat never sees.
This prints the same evaluation re-read without those two properties, side by
side with the shipped one, so the difference is a number rather than an
argument.

The angle join, and why it is best-effort: the cloud does not bank a numeric
angle on a position record — only its role. For an already-captured corpus the
angle is recoverable from the walk driver's own log, which prints
``released index=N -> ... "attempt": M, "degrees": D`` for every position it
released. This tool joins on ``(index, attempt)``, which is unique within one
walk, and picks a receipt's walk log by requiring that log's released pairs to
cover every pair the receipt carries. When zero or more than one log qualifies
it declines and says so: an angle guessed from the wrong walk is worse than no
angle, and every view already degrades to role-only without one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.crossover_v2_flow import POSITION_ROLE_ONAX
from jasper.active_speaker.flat_spec import BandResult, FlatSpecReport
from jasper.active_speaker.flat_spec_views import (
    DirectivityTable,
    LogPooledResidual,
    PositionCurve,
    RoleFlatness,
    RoleSplitFlatness,
    directivity_table,
    log_pooled_residual,
    role_split_flatness,
)

# `released index=2 -> http 200 {"ok": true, "released": {"index": 2,
# "attempt": 2, "degrees": -7, "role": "onax", ...` -- matched by field rather
# than parsed as JSON because the walk driver truncates the response body
# mid-object in its log line. All four fields it needs land before the cut.
_RELEASED_RE = re.compile(
    r'"index":\s*(?P<index>\d+)\s*,\s*"attempt":\s*(?P<attempt>\d+)\s*,'
    r'\s*"degrees":\s*(?P<degrees>-?\d+(?:\.\d+)?)\s*,\s*"role":\s*"(?P<role>[^"]*)"',
)

#: The join key. ``(index, attempt)`` alone is NOT unique across a run's logs:
#: on the 2026-08-18 arm-run every arm's receipt carries the same four pairs
#: and eleven of the thirteen walk logs contain all four -- at two different
#: angle assignments, because a measure walk and a verify walk number their
#: positions the same way while visiting different angles. Adding the role the
#: receipt already records narrows that to the walks whose position at each
#: pair answered the same question, which on that corpus is a set that agrees.
JoinKey = tuple[int, int, str]


def _fmt(value: float | None, width: int = 8, places: int = 3) -> str:
    """A number, or a visible dash. Never a zero standing in for a missing one."""
    if value is None:
        return "—".rjust(width)
    return f"{value:>{width}.{places}f}"


def _band_result_from_dict(raw: dict[str, Any]) -> BandResult:
    """One persisted band back into its dataclass, field for field.

    `BandResult.to_dict` is the only writer of this shape; this is its inverse
    and nothing here re-derives a value the writer did not store.
    """
    return BandResult(
        f_lo_hz=float(raw["f_lo_hz"]),
        f_hi_hz=float(raw["f_hi_hz"]),
        tolerance_db=float(raw["tolerance_db"]),
        max_deviation_db=raw.get("max_deviation_db"),
        max_deviation_hz=raw.get("max_deviation_hz"),
        rms_deviation_db=raw.get("rms_deviation_db"),
        n_bins=int(raw["n_bins"]),
        n_excluded=int(raw["n_excluded"]),
        evaluable=bool(raw["evaluable"]),
        passed=raw.get("passed"),
        level_deviation_db=raw.get("level_deviation_db"),
        max_ripple_db=raw.get("max_ripple_db"),
        max_ripple_hz=raw.get("max_ripple_hz"),
        graded_lo_hz=raw.get("graded_lo_hz"),
        max_at_graded_edge=raw.get("max_at_graded_edge"),
    )


def _report_from_dict(raw: dict[str, Any]) -> FlatSpecReport:
    """The banked `spec` block back into a `FlatSpecReport`.

    The receipt stores `FlatSpecReport.to_dict()` verbatim, so this is a
    rehydration and not a reconstruction: no band edge, floor, or reference is
    recomputed here. The dataclass defaults cover a report written before a
    field existed, which is exactly what they are documented to be for.
    """
    reference_band = raw.get("reference_band_hz")
    kwargs: dict[str, Any] = {}
    if reference_band is not None:
        kwargs["reference_band_hz"] = (float(reference_band[0]), float(reference_band[1]))
    return FlatSpecReport(
        reference_db=float(raw["reference_db"]),
        bands=tuple(_band_result_from_dict(b) for b in raw["bands"]),
        overall_passed=bool(raw["overall_passed"]),
        excluded_intervals=tuple(
            (float(lo), float(hi)) for lo, hi in raw.get("excluded_intervals", ())
        ),
        best_effort_above_hz=float(raw["best_effort_above_hz"]),
        smoothing_fraction=int(raw["smoothing_fraction"]),
        trusted_floor_hz=raw.get("trusted_floor_hz"),
        **kwargs,
    )


def _walk_log_angles(path: Path) -> dict[JoinKey, float]:
    """``(index, attempt, role) -> degrees`` for every position one walk released."""
    angles: dict[JoinKey, float] = {}
    for line in path.read_text(errors="replace").splitlines():
        if "released index=" not in line:
            continue
        found = _RELEASED_RE.search(line)
        if found is None:
            continue
        key = (int(found["index"]), int(found["attempt"]), found["role"])
        angles[key] = float(found["degrees"])
    return angles


def _resolve_angles(
    wanted: list[JoinKey], walk_logs: dict[Path, dict[JoinKey, float]],
) -> tuple[dict[JoinKey, float], str]:
    """Recover this receipt's microphone angles from the walks that could have
    produced it.

    Every walk log covering all of ``wanted`` is a candidate. When they agree
    on every angle the join succeeds and the note says how many agreed — which
    is a stronger statement than naming one file, because it is what actually
    licenses the numbers. When they disagree the join is DECLINED: the views
    degrade to role-only, which is honest, where an angle taken from the wrong
    walk would be a plausible-looking lie on every row.

    Returns the angle map and a note, or an empty map and the reason.
    """
    if not walk_logs:
        return {}, "no walk logs supplied — role-only"
    if not wanted:
        return {}, "receipt carries no index/attempt/role keys — role-only"
    covering = [
        (path, angles)
        for path, angles in sorted(walk_logs.items())
        if all(key in angles for key in wanted)
    ]
    if not covering:
        return {}, "no walk log covers every position of this receipt — role-only"
    distinct = {
        tuple(sorted((key, angles[key]) for key in wanted)) for _path, angles in covering
    }
    if len(distinct) > 1:
        names = ", ".join(path.name for path, _a in covering)
        return {}, f"DECLINED — {len(covering)} covering walk logs disagree ({names})"
    _path, angles = covering[0]
    names = ", ".join(path.name for path, _a in covering)
    return (
        {key: angles[key] for key in wanted},
        f"{len(covering)} covering walk log(s) agree ({names})",
    )


@dataclass(frozen=True)
class ArmViews:
    """One receipt's three views, plus what it took to build them."""

    arm: str
    path: Path
    phase: str
    report: FlatSpecReport
    pooled: LogPooledResidual
    split: RoleSplitFlatness
    directivity: DirectivityTable
    angle_note: str
    position_smoothing: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "receipt": str(self.path),
            "phase": self.phase,
            "shipped": {
                "overall_passed": self.report.overall_passed,
                "smoothing_fraction": self.report.smoothing_fraction,
                "trusted_floor_hz": self.report.trusted_floor_hz,
                "reference_band_hz": list(self.report.reference_band_hz),
            },
            "position_smoothing_fraction": self.position_smoothing,
            "angle_join": self.angle_note,
            "log_pooled_residual": self.pooled.to_dict(),
            "role_split_flatness": self.split.to_dict(),
            "directivity_table": self.directivity.to_dict(),
        }


def _load_arm(
    path: Path,
    root: Path,
    walk_logs: dict[Path, dict[JoinKey, float]],
    reference_role: str,
) -> ArmViews | None:
    """Build one receipt's views, or ``None`` when it banks no evaluation."""
    payload = json.loads(path.read_text())
    spec = payload.get("spec")
    if not isinstance(spec, dict) or not spec.get("bands"):
        return None
    report = _report_from_dict(spec)
    block = payload.get("positions") or {}
    rows = block.get("positions") or []
    grid = np.asarray((block.get("curve_grid") or {}).get("freqs_hz") or [], dtype=float)
    smoothing = int((block.get("curve_grid") or {}).get("smoothing_fraction") or 0)
    def _key(row: dict[str, Any]) -> JoinKey | None:
        if row.get("index") is None or row.get("attempt") is None:
            return None
        return (int(row["index"]), int(row["attempt"]), str(row.get("role") or ""))

    keys = [k for k in (_key(row) for row in rows) if k is not None]
    angles, angle_note = _resolve_angles(keys, walk_logs)
    positions = tuple(
        PositionCurve(
            position_id=str(row.get("position_id") or ""),
            role=str(row.get("role") or ""),
            freqs_hz=grid,
            magnitude_db=np.asarray(row.get("magnitude_db") or [], dtype=float),
            smoothing_fraction=smoothing,
            degrees=angles.get(_key(row)) if _key(row) is not None else None,
            take_id=str(row.get("take_id") or ""),
        )
        for row in rows
    )
    # The receipt lives at <arm>/evidence/v1/artifacts/crossover_v2/<cap>/…, so
    # the arm is the first path component under the receipts root the caller
    # named. Taken relative to that root rather than by counting `.parent`s,
    # which silently names the wrong directory if the depth ever changes.
    relative = path.relative_to(root).parts
    return ArmViews(
        arm=relative[0] if len(relative) > 1 else path.stem,
        path=path,
        phase=str(payload.get("phase") or ""),
        report=report,
        pooled=log_pooled_residual(report),
        split=role_split_flatness(report, positions, primary_role=reference_role),
        directivity=directivity_table(report, positions, reference_role=reference_role),
        angle_note=angle_note,
        position_smoothing=smoothing,
    )


def _render_headline(arms: list[ArmViews], reference_role: str) -> None:
    print("=" * 96)
    print("HEADLINE — the shipped pooled number, and the same evaluation read honestly")
    print("=" * 96)
    print(
        f"{'arm':<12}{'shipped':>9}{'per-oct':>9}{'  |':>3}"
        f"{reference_role:>9}{'off-axis':>10}{'  |':>3}{'gap':>8}  positions",
    )
    print(
        f"{'':<12}{'lin.pool':>9}{'log.pool':>9}{'  |':>3}"
        f"{'pooled':>9}{'pooled':>10}{'  |':>3}{'off-on':>8}",
    )
    print("-" * 96)
    for arm in arms:
        primary = arm.split.primary
        offax = [r for r in arm.split.others if r.evaluable and r.log_rms_db is not None]
        # The single worst other role, not a mean of them: averaging roles is
        # the thing these views exist to stop doing.
        worst_other = max(offax, key=lambda r: r.log_rms_db) if offax else None
        gap = (
            worst_other.log_rms_db - primary.log_rms_db
            if worst_other is not None
            and worst_other.log_rms_db is not None
            and primary is not None
            and primary.log_rms_db is not None
            else None
        )
        n_pos = sum(
            role.n_positions
            for role in ((primary,) if primary is not None else ()) + arm.split.others
        )
        print(
            f"{arm.arm:<12}{_fmt(arm.pooled.linear_rms_db, 9)}{_fmt(arm.pooled.rms_db, 9)}"
            f"{'  |':>3}{_fmt(primary.log_rms_db if primary else None, 9)}"
            f"{_fmt(worst_other.log_rms_db if worst_other else None, 10)}"
            f"{'  |':>3}{_fmt(gap, 8)}  {n_pos}",
        )
    print()
    print("  shipped lin.pool : every graded BIN weighted equally (the linear rfft axis)")
    print("  per-oct log.pool : every graded OCTAVE weighted equally, same per-band figures")
    print(f"  {reference_role:<17}: only {reference_role} positions, pooled per octave")
    print("  off-axis         : the WORST other role, never averaged with the primary one")
    print("  NOTE: the two left columns come from the COMBINED curve; the two right ones")
    print("        come from individual member curves at a finer smoothing fraction, so a")
    print("        per-role figure reads high against a pooled one for that reason alone.")
    print("        Read the gap between roles, not a difference across the '|'.")


def _render_weights(arm: ArmViews) -> None:
    print(f"  band weights — where the linear grid puts its attention ({arm.arm})")
    print(
        f"    {'band':<16}{'graded lo':>10}{'octaves':>9}{'bins':>7}"
        f"{'bins/oct':>10}{'lin share':>11}{'log share':>11}{'rms dB':>9}",
    )
    for band in arm.pooled.bands:
        label = f"{band.f_lo_hz:.0f}-{band.f_hi_hz:.0f} Hz"
        print(
            f"    {label:<16}{band.graded_lo_hz:>10.1f}{band.octaves:>9.3f}"
            f"{band.n_bins:>7d}{band.bins_per_octave:>10.1f}"
            f"{band.linear_share:>10.1%}{band.log_share:>11.1%}"
            f"{band.rms_deviation_db:>9.4f}",
        )
    if len(arm.pooled.bands) >= 2:
        top = max(arm.pooled.bands, key=lambda b: b.bins_per_octave)
        bottom = min(arm.pooled.bands, key=lambda b: b.bins_per_octave)
        ratio = top.bins_per_octave / bottom.bins_per_octave
        print(
            f"    per-octave overweight, {top.f_lo_hz:.0f}-{top.f_hi_hz:.0f} Hz over "
            f"{bottom.f_lo_hz:.0f}-{bottom.f_hi_hz:.0f} Hz: {ratio:.1f}:1 under the "
            f"linear grid, 1.0:1 per octave",
        )
    if arm.pooled.n_bands_not_evaluated:
        print(f"    NOT EVALUATED: {arm.pooled.n_bands_not_evaluated} band(s) carried no residual")


def _render_role(role: RoleFlatness) -> None:
    print(
        f"    {role.role:<8}n={role.n_positions} evaluated={role.n_evaluated}  "
        f"lin.pool={_fmt(role.rms_db, 7)}  log.pool={_fmt(role.log_rms_db, 7)}",
    )
    for position in role.positions:
        angle = "  role-only" if position.degrees is None else f"{position.degrees:>+8.0f}°"
        if not position.evaluable:
            print(
                f"      {position.position_id:<20}{angle}  NOT EVALUATED — "
                f"{position.not_evaluated_reason}",
            )
            continue
        print(
            f"      {position.position_id:<20}{angle}  lin={_fmt(position.rms_db, 7)}"
            f"  log={_fmt(position.log_rms_db, 7)}  bins={position.n_bins}",
        )


def _render_directivity(table: DirectivityTable) -> None:
    if not table.evaluable:
        print(f"    NOT EVALUATED — {table.not_evaluated_reason}")
        return
    print(
        f"    reference: {table.reference_role} "
        f"({', '.join(table.reference_position_ids) or 'none'}), "
        f"level pooled over {table.reference_band_hz[0]:.0f}-{table.reference_band_hz[1]:.0f} Hz",
    )
    if not table.angles_recorded:
        print("    angles: NOT RECORDED for every row — table is role-labelled only")
    print(
        f"      {'position':<20}{'angle':>8}{'level':>9}   per-band level offset / shape RMS, dB",
    )
    for row in table.rows:
        if not row.evaluable:
            print(f"      {row.position_id:<20}  NOT EVALUATED — {row.not_evaluated_reason}")
            continue
        angle = "       —" if row.degrees is None else f"{row.degrees:>+7.0f}°"
        bands = "  ".join(
            f"{b.f_lo_hz / 1000:.2g}k:{_fmt(b.level_offset_db, 6, 2)}/"
            f"{_fmt(b.shape_rms_db, 5, 2)}"
            if b.evaluable
            else f"{b.f_lo_hz / 1000:.2g}k:NOT-EVAL"
            for b in row.bands
        )
        marker = "*" if row.in_reference else " "
        print(
            f"      {row.position_id:<20}{angle}{_fmt(row.level_offset_db, 9, 2)}{marker}  {bands}",
        )
    print("      * = a member of the reference itself; its departure is cloud spread, not")
    print("        off-axis behaviour. level = broadband offset from on-axis over the")
    print("        reference band; per band, level offset / shape RMS after removing it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "receipts", type=Path, help="a receipts/ directory holding banked cloud evidence",
    )
    parser.add_argument(
        "--walk-logs",
        type=Path,
        default=None,
        help="directory of walk-*.log files, to recover per-position microphone angles",
    )
    parser.add_argument(
        "--role",
        default=POSITION_ROLE_ONAX,
        help=f"the reference role for the split and the directivity table (default {POSITION_ROLE_ONAX})",
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="also write every view to this path as JSON",
    )
    args = parser.parse_args(argv)

    if not args.receipts.is_dir():
        print(f"not a directory: {args.receipts}", file=sys.stderr)
        return 2
    walk_logs: dict[Path, dict[JoinKey, float]] = {}
    if args.walk_logs is not None:
        walk_logs = {p: _walk_log_angles(p) for p in sorted(args.walk_logs.glob("walk-*.log"))}

    arms: list[ArmViews] = []
    for path in sorted(args.receipts.glob("**/cloud_verify.json")):
        loaded = _load_arm(path, args.receipts, walk_logs, args.role)
        if loaded is None:
            print(f"skipped (no banked spec block): {path}", file=sys.stderr)
            continue
        arms.append(loaded)
    if not arms:
        print(f"no banked cloud evidence under {args.receipts}", file=sys.stderr)
        return 1

    _render_headline(arms, args.role)
    for arm in arms:
        print()
        print("=" * 96)
        floor = (
            "not stated"
            if arm.report.trusted_floor_hz is None
            else f"{arm.report.trusted_floor_hz:.1f} Hz"
        )
        print(
            f"{arm.arm}  —  phase={arm.phase}  shipped verdict "
            f"passed={arm.report.overall_passed}  trusted floor={floor}",
        )
        print(
            f"  smoothing: combined curve 1/{arm.report.smoothing_fraction} oct, "
            f"member curves 1/{arm.position_smoothing} oct   |   angles: {arm.angle_note}",
        )
        print("=" * 96)
        _render_weights(arm)
        print()
        print("  role split — the primary role is never averaged with the others")
        if arm.split.primary is not None:
            _render_role(arm.split.primary)
        else:
            print(f"    {arm.split.primary_role}: UNSAMPLED — no position carried this role")
        for role in arm.split.others:
            _render_role(role)
        if arm.split.n_not_evaluated:
            print(f"    NOT EVALUATED: {arm.split.n_not_evaluated} position(s)")
        print()
        print("  directivity — every position normalised to the on-axis reference")
        _render_directivity(arm.directivity)

    if args.json is not None:
        args.json.write_text(json.dumps([arm.to_dict() for arm in arms], indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
