# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator CLI for the offline emit loop — one invocation, on the speaker.

Renders a predicted linearization through the REAL pinned CamillaDSP binary and
grades what the DSP emits against what the fit claims
(:mod:`jasper.active_speaker.bench`). It plays no audio, opens no device, and
never touches the running graph: every render is a one-shot batch pass over
files under ``--out``.

It must run on the speaker, because the binary's identity is resolved from the
running ``jasper-camilla.service`` unit (R5) — there is no ``--binary`` override
to bypass that, deliberately. On a laptop, ``--dry-run`` emits and derives both
arms and writes both derived configs without resolving a binary or rendering
anything: every refusal that does not need a binary (an emitter validation
refusal, a stage outside the offline allowlist, a hard-clip limiter, a stimulus
past the FFT cap) surfaces there, on the laptop, instead of on the Pi.

**Exit codes are three-state, because the evidence is.**

* ``0`` — every branch that could be graded matched, and at least one was.
  Branches with nothing to verify are listed, not counted.
* ``1`` — at least one graded branch did not match. The finding.
* ``2`` — no verdict was reached: the run refused (bad inputs, no binary, an
  unrenderable config, a residual that could not be attributed), or it
  completed but nothing in it was gradeable. Both are "no evidence", and
  neither may read as a pass — see
  :mod:`jasper.active_speaker.delta_probe`'s ``unavailable`` doctrine.

**Memory:** the renders are bounded in their own child processes, but the
deconvolution and FFTs run in THIS process and are not. A production-length run
measures 221 MiB parent-only peak RSS (an independent measurement during review
put it at 235 MiB) — a real fraction of a 1 GB Pi. Run it through
``scripts/pi-run-diagnostic.sh`` (see ``docs/testing-tooling.md``) so the kernel
has an obvious thing to kill before it reaches a product daemon.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker.bench.compare import analysis_band_hz
from jasper.active_speaker.bench.derivation import EmitDerivationError
from jasper.active_speaker.bench.loop import (
    OUTCOME_MATCHED,
    OUTCOME_MISMATCHED,
    STIMULUS_SWEEP_SECONDS,
    EmitLoopError,
    EmitLoopPlan,
    EmitLoopReport,
    plan_emit_loop,
    run_emit_loop,
)
from jasper.active_speaker.linearization_fit import linearization_filters_by_role
from jasper.active_speaker.profile import ActiveSpeakerConfigError, ActiveSpeakerPreset
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_measurement.sweep import synchronized_sweep_metadata
from jasper.bass_extension.bench.render import RenderError, resolve_render_binary


def _load_linearization(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read a persisted ``{role: LinearizationFit.to_dict()}`` file, reduced.

    ONE accepted shape, on purpose. ``linearization_filters_by_role`` returns
    ``{}`` — not an error — when handed an already-reduced mapping, so a CLI
    that sniffed between the two shapes could silently grade nothing at all.
    Requiring the fit shape and refusing an empty reduction makes that
    impossible.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must be a JSON object keyed by driver role")
    reduced = linearization_filters_by_role(raw)
    if not any(reduced.values()):
        raise SystemExit(
            f"{path} carries no linearization filters. Expected the persisted FIT "
            'shape — {"<role>": {"filters": [{"biquad_type": …}, …]}} — not the '
            "already-reduced {role: [filters]} shape."
        )
    return reduced


def _print_plan(
    preset: ActiveSpeakerPreset,
    reduced: dict[str, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> None:
    meta = synchronized_sweep_metadata(duration_approx_s=float(args.sweep_seconds))
    lo, hi = analysis_band_hz(meta)
    print(f"preset: {preset.preset_id} ({preset.way_count}-way, {preset.name})")
    print(f"playback device: {args.playback_device}")
    for role, filters in sorted(reduced.items()):
        shapes = ", ".join(
            f"{f.get('biquad_type')}@{float(f.get('freq', 0.0)):.0f}Hz"
            f"/{float(f.get('gain', 0.0)):+.1f}dB"
            for f in filters
        )
        print(f"  {role}: {len(filters)} filter(s) {shapes or '—'}")
    print(f"stimulus: {meta.duration_s:.3f} s sweep, {meta.f1:.0f}–{meta.f2:.0f} Hz")
    print(f"analysis band: {lo:.0f}–{hi:.0f} Hz")
    print(f"work dir: {args.out}")


def _print_derivation(plan: EmitLoopPlan) -> None:
    """What the emit + derive preflight proved, without any binary."""

    print(
        f"derived both arms: capture {plan.geometry.capture_channels}ch @ "
        f"{plan.geometry.sample_rate_hz} Hz -> playback "
        f"{plan.geometry.playback_channels}ch"
    )
    for branch in plan.treated.branches:
        print(
            f"  {branch.role:<10} channels {list(branch.channels)}  "
            f"limiter {branch.limiter_clip_limit_dbfs:+.2f} dBFS  "
            f"{len(branch.names)} stage(s)"
        )
    print(f"emitter level move between arms: {plan.expected_offset_db:+.3f} dB")
    for path in plan.config_paths:
        print(f"  wrote {path}")
    print(f"  stimulus the run will write: {plan.stimulus_path} (not written here)")


def _print_report(report: EmitLoopReport) -> None:
    print()
    print(f"binary: {report.binary['binary_path']} ({report.binary['version_output']})")
    print(f"emitter level move between arms: {report.expected_offset_db:+.3f} dB")
    for branch in report.branches:
        valid = branch.valid_band_hz
        span = f"{valid[0]:.0f}–{valid[1]:.0f} Hz" if valid else "no valid bins"
        if not branch.graded:
            # Named, never counted. "Measured, nothing to verify" and "not
            # measured at all" are different news and the reader gets both.
            seen = (
                f"measured over {span} ({branch.n_bins} bins)"
                if branch.measured
                else "not measured"
            )
            print(
                f"  {branch.role:<10} {'unavailable':<14} no verdict — {seen}"
                f"{f'; {branch.verdict.reason}' if branch.verdict.reason else ''}"
            )
            continue
        print(
            f"  {branch.role:<10} {branch.verdict.verdict:<14} "
            f"max {branch.band_max_error_db:7.3f} dB @ {branch.band_worst_hz:7.0f} Hz  "
            f"rms {branch.band_rms_error_db:6.3f} dB  over {span}"
        )
        fit = branch.frame.fit
        if fit.fitted:
            print(
                f"  {'':<10} frame offset {fit.offset_db:+.3f} dB, tilt "
                f"{fit.tilt_db_per_octave:+.3f} dB/oct; frame-removed max "
                f"{branch.frame.tilt_removed_max_db:.3f} dB"
            )
        if branch.verdict.reason:
            print(f"  {'':<10} reason: {branch.verdict.reason}")
    print(f"outcome: {report.outcome}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--linearization",
        type=Path,
        required=True,
        help="JSON file of persisted per-role LinearizationFit records",
    )
    parser.add_argument(
        "--playback-device",
        required=True,
        help="the active playback PCM the emitted config targets, e.g. hw:CARD=DAC8x,DEV=0",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("emit-loop-bundle"),
        help="directory for the configs, renders, and report.json",
    )
    parser.add_argument(
        "--preset",
        type=Path,
        default=None,
        help="active-speaker preset JSON (default: the bundled worked example)",
    )
    parser.add_argument(
        "--sweep-seconds",
        type=float,
        default=STIMULUS_SWEEP_SECONDS,
        help="nominal sweep length; the generator rounds it for synchronization",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "emit and derive both arms and write both configs, then stop; "
            "resolve no binary and render nothing"
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s emit-bench %(levelname)s %(message)s",
    )

    try:
        preset = load_active_speaker_preset(args.preset)
    except ActiveSpeakerConfigError as exc:
        print(f"REFUSED — preset: {exc}", file=sys.stderr)
        return 2
    reduced = _load_linearization(args.linearization)
    _print_plan(preset, reduced, args)

    # A refusing run must not leave a previous run's verdict sitting beside
    # this run's fresh configs, where the next reader would pair them.
    report_path = Path(args.out) / "report.json"
    report_path.unlink(missing_ok=True)

    if args.dry_run:
        try:
            plan = plan_emit_loop(
                preset,
                playback_device=args.playback_device,
                linearization=reduced,
                work_dir=args.out,
                sweep_seconds=float(args.sweep_seconds),
            )
        except (EmitLoopError, EmitDerivationError, ActiveSpeakerConfigError) as exc:
            print(f"REFUSED — {exc}", file=sys.stderr)
            return 2
        _print_derivation(plan)
        print("dry run: no binary resolved, nothing rendered")
        return 0

    try:
        binary = resolve_render_binary()
    except RenderError as exc:
        print(f"REFUSED — render binary: {exc}", file=sys.stderr)
        return 2

    try:
        report = run_emit_loop(
            preset,
            playback_device=args.playback_device,
            linearization=reduced,
            work_dir=args.out,
            binary=binary,
            sweep_seconds=float(args.sweep_seconds),
        )
    except (EmitLoopError, EmitDerivationError, ActiveSpeakerConfigError) as exc:
        print(f"REFUSED — {exc}", file=sys.stderr)
        return 2

    report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    _print_report(report)
    print(f"\nreport: {report_path}")
    if report.outcome == OUTCOME_MATCHED:
        return 0
    if report.outcome == OUTCOME_MISMATCHED:
        return 1
    # Nothing reached a verdict. Not a refusal in the sense of "the run broke",
    # but the same absence of evidence, and it must not exit 0.
    print(
        "no branch could be graded — the linearization commands nothing this "
        "instrument can verify",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
