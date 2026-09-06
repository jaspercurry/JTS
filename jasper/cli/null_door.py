# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator door onto the summed reverse null: the CONFIRM half.

``jasper-round-views delay-landscape`` computes a coordinate from banked
per-driver curves; this plays it. ``--polarity both`` plays the in-phase and
inverted takes at delay 0 — the PAIR is the polarity proof, neither half means
anything alone — and ``--delays`` plays the proposal's optimum and a neighbour
either side. It banks and does not grade: every coordinate writes ONE self-contained
JSON row to ``<bundle>/null_runs/`` carrying everything needed to judge it, so
the row IS the join. A refusal is an output, printed verbatim from the module
that decided it. Applying a confirmed delay is the prescription door's job.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..logging_setup import configure_verbose_logging
from ._refusal import EXIT_OK as EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE, answered, failed

#: Where this door banks one JSON row per played coordinate, beside the
#: bundle it measured. ``jasper-round-views delay-confirm`` grades what lands
#: here.
NULL_RUNS_DIR = "null_runs"

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "measured"

#: Compose-time refusals carry the COMPOSER's reason (``program.NULL_REFUSE_*``)
#: rather than a slug spelled again here.
REFUSE_NO_SHOULDERS = "null_confirm_shoulders_unreadable"
REFUSE_UNUSABLE_CAPTURE = "null_confirm_capture_unusable"
#: The microphone half failed. One slug, two statuses: "refused" with no rows
#: when it lands before the door, "partial" with the banked ids when it lands
#: between two coordinates.
REFUSE_CAPTURE_FAILED = "null_confirm_capture_failed"

#: The three named mid-run failures (B4), spelled the way ``jasper-measure``
#: spells them. None is a programming error and all three can arrive after k
#: rows are already on disk.
REFUSE_GRAPH_LOST = "null_confirm_graph_lost"
REFUSE_ISOLATION_LOST = "null_confirm_isolation_lost"
REFUSE_VOLUME_LOST = "null_confirm_volume_lost"

#: One slug for every ``CrossoverV2Refused``: the applied profile could not
#: answer an input, and its own sentence rides in ``detail``.
REFUSE_BOX_NOT_READY = "null_confirm_box_not_ready"
#: The two UNREADABLE inputs, which are faults rather than refusals: a
#: coordinate off the grid the proposal was computed on, and an unreadable
#: state file.
REFUSE_DELAY_OFF_GRID = "null_confirm_delay_off_grid"
REFUSE_STATE_UNREADABLE = "null_confirm_state_unreadable"

POLARITY_BOTH = "both"
POLARITY_KEEP = "keep"
POLARITY_INVERT = "invert"

#: This door's identity on the mux diagnostic gate. ``mux.FANIN_TEST_OWNERS`` is
#: CLOSED: an owner missing from it is refused the gate and this door measures
#: silence with every daemon healthy. Inheriting the wizard's
#: ``correction-measurement`` would file this door's hold under the wizard.
DOOR_GATE_OWNER = "jasper-null"

#: The mic sits on the design axis at the reference distance, so the impulse
#: response is gated to its reflection-free span before the magnitude is read.
#: A near-field geometry would skip that gating and read the room into the null.
CAPTURE_GEOMETRY = "reference_axis"

#: Seconds of room tail captured after the program ends, and the slack allowed
#: between arming the recorder and the first sample. Both are the wired capture
#: path's own numbers.
POST_ROLL_S = 1.0
PRE_PLAY_ALLOWANCE_S = 20.0


def _mid_run_failures() -> dict[type[BaseException], str]:
    """The named mid-run failures, and the reason each renders as.

    Resolved ONCE per run rather than per coordinate, so the tuple `except` is
    built from the same classes the reason lookup scans.
    """
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
    from jasper.audio_measurement.wired_capture import WiredCaptureError
    from jasper.measurement_window import MeasurementWindowError

    return {
        SessionGraphError: REFUSE_GRAPH_LOST,
        MeasurementWindowError: REFUSE_ISOLATION_LOST,
        SessionVolumePlanError: REFUSE_VOLUME_LOST,
        # The mic is the fourth thing that can stop a walk between two
        # coordinates, and it left `main` as a bare traceback over k banked rows.
        WiredCaptureError: REFUSE_CAPTURE_FAILED,
    }


def _mid_run_reason(
    failures: Mapping[type[BaseException], str], exc: BaseException,
) -> str:
    """Which reason this failure renders as. Scanned, not keyed.

    ``type(exc)`` misses a SUBCLASS of any of the three, which would fall
    through to the bare traceback the partial payload exists to prevent.
    """
    for cls, reason in failures.items():
        if isinstance(exc, cls):
            return reason
    raise AssertionError("only the mapped failures reach here")


class NullRunInterrupted(RuntimeError):
    """A mid-run failure, carrying the rows that ARE on disk.

    A traceback would exit with no JSON while k rows sit in ``null_runs/``
    unnamed — evidence the operator has no id for.
    """

    def __init__(self, reason: str, detail: str, banked: Sequence[str]) -> None:
        self.reason = reason
        self.detail = detail
        self.banked = list(banked)
        super().__init__(f"{reason}: {detail}")


class NullDoorRefused(RuntimeError):
    """One coordinate cannot be measured; the row says why."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail




def _context() -> Any:
    """The applied profile's own answer to every measurement input.

    ONE owner: ``resolve_conductor_context`` derives the corner, the per-role
    bands, caps and duration limits, the session volume and the playback device
    from the applied profile. A corner typed on a command line is a claim about
    the graph, and the graph can answer for itself.
    """
    from jasper.active_speaker.crossover_v2.conductor_context import (
        conductor_status,
        resolve_conductor_context,
    )

    return resolve_conductor_context(conductor_status())


def _level_trims(context: Any) -> tuple[dict[str, float], str]:
    """The branch level match, through its single owner.

    Attenuation-only by construction at the writer, which keeps the branch-gap
    ceiling below and the per-role caps two independent legs. Empty is an
    honest answer, and the row's ``gap_ceiling_db`` says what it costs.
    """
    from jasper.active_speaker.baseline_profile import measured_level_trims
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.measurement import load_measurement_state

    trims, meta = measured_level_trims(
        context.preset,
        load_measurement_state(context.topology) or {},
        load_crossover_preview() or {},
    )
    return (
        {str(role): float(db) for role, db in trims.items()},
        str(meta.get("source") or ""),
    )


def _gap_ceiling_db(
    trims_db: Mapping[str, float], declared_sensitivities: Mapping[str, float],
) -> float:
    """The deepest null this speaker's BRANCH LEVELS allow. Disclosure, never a
    refusal.

    Two branches whose outputs differ by ``gap`` cannot cancel below
    ``-20*log10(1 - 10**(-gap/20))`` however right the delay is. The gap is what
    the level match REMOVED, not what it installed, so: trims installed ->
    unbounded (the branches were brought to equal output); no trims -> the
    declared sensitivity spread IS the bound; fewer than two declared
    sensitivities -> unbounded, with ``trims_source`` naming the absence.
    """
    from jasper.audio_measurement.interference_nulls import (
        branch_gap_null_depth_ceiling_db,
    )

    if trims_db:
        return float("inf")
    values = [float(db) for db in declared_sensitivities.values()]
    if len(values) < 2:
        return float("inf")
    return branch_gap_null_depth_ceiling_db(max(values) - min(values))


def _spec(context: Any, args: argparse.Namespace) -> Any:
    from jasper.active_speaker.delay_sweep import sweep_spec

    return sweep_spec(
        crossover_fc_hz=args.fc_hz if args.fc_hz is not None else context.fc_hz,
        upper_role=args.upper_role,
        lower_role=args.lower_role,
        signed_acoustic_path_difference_m=args.path_difference_m,
        step_us=args.step_us,
    )


def _default_delays(spec: Any, args: argparse.Namespace) -> tuple[float, ...]:
    """Zero, plus the proposal's optimum and one neighbour either side.

    §4's three takes, from the propose door's own
    ``confirmation_coordinates_us`` rather than a grid this door invents. With
    no bundle, or no banked take carrying both roles, the answer is ``(0.0,)``.
    """
    if args.bundle_dir is None:
        return (0.0,)
    from jasper.active_speaker.crossover_v2.delay_landscape import (
        DelayLandscapeError,
        compute_landscape,
    )
    from jasper.active_speaker.crossover_v2.position_cycle import (
        read_pose_curve_pair,
    )

    found = read_pose_curve_pair(
        Path(args.bundle_dir),
        phase=args.phase,
        position_deg=args.position,
        roles=(spec.negative_delay_target, spec.positive_delay_target),
    )
    if found is None:
        return (0.0,)
    try:
        landscape = compute_landscape(
            found[0], found[1], spec=spec, inverted_role=args.inverted_role,
        )
    except DelayLandscapeError:
        # The bank cannot support a proposal, but the polarity proof at zero
        # still can — and it is the take that decides whether a proposal is
        # worth computing, so this degrades rather than refusing the run.
        return (0.0,)
    return tuple(dict.fromkeys((0.0, *landscape.confirmation_coordinates_us)))


def _coordinates(
    spec: Any, args: argparse.Namespace, delays_us: Sequence[float],
) -> list[tuple[Any, bool]]:
    """Every (delay candidate, inverted?) pair this run measures.

    ``--polarity both`` pairs the two polarities at delay 0 ONLY: the in-phase
    take is a property of the speaker, not of a candidate delay, so repeating
    it at every coordinate would double a walk's audio for a fixed number.
    """
    inversions = {
        POLARITY_BOTH: (False, True),
        POLARITY_KEEP: (False,),
        POLARITY_INVERT: (True,),
    }[args.polarity]
    out: list[tuple[Any, bool]] = []
    for inverted in inversions:
        for delay_us in delays_us:
            if delay_us and not inverted and args.polarity == POLARITY_BOTH:
                continue
            out.append((spec.dsp_candidate(delay_us), inverted))
    return out




def _sweep_meta(segment: Any) -> dict[str, Any]:
    """The deconvolution reference: the PARENT sweep, ungated.

    Every branch regenerates this same waveform and differs only in which
    samples are silenced, so deconvolving against one branch's gated copy would
    model the gate as part of the speaker.
    """
    from jasper.audio_measurement.program import PROGRAM_SAMPLE_RATE_HZ
    from jasper.audio_measurement.sweep import synchronized_swept_sine

    _signal, meta = synchronized_swept_sine(
        f1=float(segment.f1_hz),
        f2=float(segment.f2_hz),
        duration_approx_s=segment.n_samples / PROGRAM_SAMPLE_RATE_HZ,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
        amplitude_dbfs=float(segment.gain_db),
    )
    return meta.to_dict()


def _compose(context: Any, fc_hz: float) -> tuple[Any, Any, float]:
    """The stimulus, its channel plan, and the ONE gain both branches carry.

    This door computes the clamp; the composer applies it. Two branches at
    different levels are not the same waveform and cannot cancel, so a per-role
    clamp inside the composer would destroy the identity the null is measured
    by. The clamp is the MOST RESTRICTIVE role cap backed off by the shared
    measurement headroom; admission still checks it per role on rendered bytes.
    """
    from jasper.active_speaker.crossover_v2.programs import back_off_gain
    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        build_null_confirm_program,
    )

    gain_db = back_off_gain(
        BASE_STIMULUS_PEAK_DBFS,
        context.session_volume_db,
        min(float(cap) for cap in context.driver_caps_dbfs.values()),
    )
    program, plan = build_null_confirm_program(
        fc_hz,
        context.roles_bands,
        gain_db=gain_db,
        sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
        downstream_gain_db=context.session_volume_db,
    )
    return program, plan, gain_db


def _publish_program(program: Any, work_dir: Path, relpath: str) -> Any:
    """Write the program WAV ONCE and name it by its bytes.

    Every coordinate plays the SAME stimulus — what changes is the graph — so
    one WAV serves the whole run and its sha256 is the hash every row carries.
    """
    from jasper.audio_measurement.evidence_identity import ArtifactIdentity
    from jasper.audio_measurement.program import write_program_wav

    path = work_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    write_program_wav(path, program)
    raw = path.read_bytes()
    return ArtifactIdentity(
        bundle_kind="jts_commissioning_bundle",
        bundle_id="jasper_null",
        relative_path=relpath,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )


def _resolve_mic() -> Any:
    """The measurement mic, resolved BEFORE the speaker is claimed.

    ``resolve_wired_mic`` fails soft to ``None``; the refusal is the caller's.
    Asking here keeps a missing mic from costing the household its volume and a
    graph swap first.
    """
    from jasper.audio_measurement.wired_capture import (
        WiredCaptureError,
        resolve_wired_mic,
    )

    mic = resolve_wired_mic()
    if mic is None:
        raise WiredCaptureError(
            "no measurement microphone is plugged into the speaker — connect a "
            "registered measurement mic (e.g. miniDSP UMIK-2) and start again"
        )
    return mic


async def _play_and_capture(
    context: Any, volume_plan: Any, program: Any, mic: Any, artifact: Any,
    work_dir: Path,
) -> bytes:
    """Admit, play through the installed graph, and capture. Returns the mic WAV.

    ``volume_plan`` must be the instance the door opened: ``play_program``
    asserts it is active AND opened in this process.
    """
    from jasper.active_speaker.crossover_v2.composition import (
        bind_program_playback_seams,
    )
    from jasper.active_speaker.program_playback import play_program
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.program import PROGRAM_SAMPLE_RATE_HZ
    from jasper.audio_measurement.wired_capture import (
        WiredRecorder,
        encode_wav_s32,
        select_capture_channel,
    )
    from jasper.camilla import primary_controller

    seams = bind_program_playback_seams(
        primary_controller(),
        bundle_dir=str(work_dir),
        artifact=artifact,
        config_dir=str(DEFAULT_CAMILLA_CONFIG_DIR),
        program=program,
        wav_path=str(work_dir / artifact.relative_path),
        topology=context.topology,
        safety_profile=context.safety_profile,
        role_targets=context.role_targets,
        session_volume_db=context.session_volume_db,
        declared_sensitivities=context.declared_sensitivities,
    )

    program_s = program.total_samples / float(PROGRAM_SAMPLE_RATE_HZ)
    recorder = WiredRecorder(
        mic.pcm,
        sample_rate_hz=PROGRAM_SAMPLE_RATE_HZ,
        channels=2,
        max_capture_s=program_s + PRE_PLAY_ALLOWANCE_S + POST_ROLL_S,
    )
    # Armed BEFORE any audio: `start` blocks until the first real chunk lands,
    # so the pre-roll is a fact rather than a hope.
    recorder.start()
    played = False
    try:
        await play_program(program, session_volume_plan=volume_plan, **seams)
        played = True
    finally:
        # Any escape must release the live ALSA device. A flag in `finally`
        # rather than a broad `except`: nothing is caught, only cleaned up.
        if not played:
            recorder.abort()
    recording = recorder.finish(tail_s=POST_ROLL_S)
    _channel, mono, _levels = select_capture_channel(recording)
    captured, _frames = encode_wav_s32(mono, sample_rate_hz=recording.sample_rate_hz)
    return captured


def _depth(
    captured_wav: Path, program: Any, plan: Any, fc_hz: float,
) -> tuple[float, Any]:
    """The null depth, read at the shoulders the propose door would have used.

    Three owners, one step each: ``summed_capture_curve`` calibrates the
    capture, ``shoulder_span`` decides where the shoulders may sit given the
    band over which BOTH branches were open at full amplitude, and
    ``crossover_null_depth_db`` subtracts. A shoulder outside that overlap
    would be read where only one driver played.
    """
    from jasper.active_speaker.driver_acoustics import summed_capture_curve
    from jasper.audio_measurement.analysis import crossover_null_depth_db, shoulder_span

    curve = summed_capture_curve(
        captured_wav,
        _sweep_meta(program.stimulus_segments()[0]),
        crossover_fc_hz=fc_hz,
        capture_geometry=CAPTURE_GEOMETRY,
        # NO ambient window, and that is a deletion rather than a smaller
        # number: any `ambient_duration_s` selects the signal-located branch,
        # whose guard needs controlled quiet reaching back PAST the whole sweep,
        # and this door records no such quiet.
    )
    if curve is None:
        raise NullDoorRefused(
            REFUSE_UNUSABLE_CAPTURE,
            f"the capture cannot decide a null at fc={fc_hz:g} Hz: it failed "
            "quality gating, or the room's low-frequency validity floor sits "
            f"above the lower shoulder {fc_hz / 2.0:g} Hz",
        )
    # The REAL analysis grid, not a stand-in: `shoulder_span` counts the bins
    # either side of Fc to decide whether a shoulder can be placed at all.
    grid = curve.freqs
    span = shoulder_span(
        grid[(grid >= plan.overlap_hz[0]) & (grid <= plan.overlap_hz[1])],
        crossover_fc_hz=fc_hz,
        overlap_hz=plan.overlap_hz,
    )
    if not span.usable:
        raise NullDoorRefused(
            REFUSE_NO_SHOULDERS,
            f"both branches are open only over [{plan.overlap_hz[0]:g},"
            f"{plan.overlap_hz[1]:g}] Hz, which cannot place a shoulder either "
            f"side of fc={fc_hz:g} Hz; there is no depth to read",
        )
    depth_db = crossover_null_depth_db(
        curve.freqs, curve.magnitude_db, fc_hz, shoulders_hz=span.used_hz,
    )
    return float(depth_db), span




def _row(
    *,
    fc_hz: float,
    candidate: Any,
    inverted: bool,
    inverted_role: str,
    position_deg: int,
    trims_db: Mapping[str, float],
    trims_source: str,
    gap_ceiling_db: float,
    graph_fingerprint: str,
    depth_db: float | None = None,
    span: Any = None,
    wav_sha256: str | None = None,
    refusal: NullDoorRefused | None = None,
) -> dict[str, Any]:
    """One self-contained coordinate. Everything a grader needs, nothing to join.

    ``inverted_role`` rides beside ``polarity`` because two speakers can both
    report an inverted take having flipped different drivers, and those depths
    are not comparable. ``None`` on a take that flipped nothing.
    """
    row: dict[str, Any] = {
        "schema_version": 1,
        "kind": "jts_null_confirm_row",
        "ts": time.time(),
        "fc_hz": fc_hz,
        "delay_us": float(candidate.relative_delay_us),
        "delayed_role": candidate.delay_target,
        "polarity": "inverted" if inverted else "in_phase",
        "inverted_role": inverted_role if inverted else None,
        "position_deg": position_deg,
        "trims_db": dict(trims_db),
        "trims_source": trims_source,
        # `inf` is not JSON, and a null here reads as "unbounded" the way the
        # float does.
        "gap_ceiling_db": (
            None if math.isinf(gap_ceiling_db) else round(gap_ceiling_db, 2)
        ),
        "graph_fingerprint": graph_fingerprint,
        "wav_sha256": wav_sha256,
        # DISCLOSED, not decided. The depth is read off an UNCALIBRATED capture
        # and the mic's own response does not cancel here: the shoulders sit an
        # octave either side of Fc, so any tilt across that span biases the
        # subtraction. Every row says which it was, so a later calibrated run is
        # distinguishable rather than silently comparable.
        "calibrated": False,
    }
    if refusal is not None:
        row["status"] = "refused"
        row["reason"] = refusal.reason
        row["detail"] = refusal.detail
        row["depth_db"] = None
        row["shoulders_used"] = None
        row["clamped_lo"] = None
        row["clamped_hi"] = None
        return row
    row["status"] = "measured"
    # Never `depth_db or 0.0`: 0.0 dB is a legal, plausible reading ("no null
    # formed") a grader could not tell from a real one. Unreachable today; kept
    # because the signature still admits ``None`` for the refusal arm.
    if depth_db is None or span is None:
        raise ValueError(
            "a measured null row needs both a depth and the shoulders it was "
            "read at; pass refusal= instead"
        )
    row["depth_db"] = round(float(depth_db), 2)
    row["shoulders_used"] = [span.used_hz[0], span.used_hz[1]]
    row["shoulders_canonical"] = [span.canonical_hz[0], span.canonical_hz[1]]
    row["clamped_lo"] = span.lower_clamped
    row["clamped_hi"] = span.upper_clamped
    return row


def _write_row(rows_dir: Path, row: Mapping[str, Any]) -> Path:
    rows_dir.mkdir(parents=True, exist_ok=True)
    name = (
        f"{int(row['ts'])}_{row['position_deg']}deg_"
        f"{row['fc_hz']:.0f}hz_{row['delay_us']:+.0f}us_{row['polarity']}.json"
    )
    path = rows_dir / name
    path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    return path




async def _run(args: argparse.Namespace) -> int:
    from jasper.active_speaker.crossover_v2.door import measurement_door
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.audio_measurement.program import NullConfirmUnavailable
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
    from jasper.camilla import primary_controller

    context = _context()
    fc_hz = float(args.fc_hz if args.fc_hz is not None else context.fc_hz)
    spec = _spec(context, args)
    delays_us = (
        tuple(args.delays) if args.delays is not None else _default_delays(spec, args)
    )
    trims_db, trims_source = _level_trims(context)
    gap_ceiling_db = _gap_ceiling_db(trims_db, context.declared_sensitivities)

    work_dir = Path(args.bundle_dir) if args.bundle_dir else Path.cwd()
    rows_dir = work_dir / NULL_RUNS_DIR

    written: list[tuple[str, dict[str, Any]]] = []
    mid_run = _mid_run_failures()

    def _bank(candidate: Any, inverted: bool, fingerprint: str, **outcome) -> None:
        """Every row this run writes, built ONE way.

        A refused coordinate carries the same identity fields as a measured
        one, or a reader cannot tell WHICH coordinate could not be measured.
        """
        row = _row(
            fc_hz=fc_hz,
            candidate=candidate,
            inverted=inverted,
            inverted_role=args.inverted_role,
            position_deg=args.position,
            trims_db=trims_db,
            trims_source=trims_source,
            gap_ceiling_db=gap_ceiling_db,
            graph_fingerprint=fingerprint,
            **outcome,
        )
        written.append((_write_row(rows_dir, row).name, row))
        print(_line(row), file=sys.stderr)

    def _answer() -> dict[str, Any]:
        """The ask and one depth per coordinate, however the run ended.

        The banked rows under ``out`` are the depth on demand: repeating one
        here would put a whole grid's shoulders and trims on stdout.
        """
        return {
            "fc_hz": fc_hz,
            "position_deg": args.position,
            "delays_us": list(delays_us),
            "out": str(rows_dir),
            "rows": [
                {
                    "row": name,
                    "delay_us": row["delay_us"],
                    "polarity": row["polarity"],
                    "status": row["status"],
                    "depth_db": row["depth_db"],
                    **({"reason": row["reason"]}
                       if row["status"] == "refused" else {}),
                }
                for name, row in written
            ],
        }

    try:
        program, plan, gain_db = _compose(context, fc_hz)
    except NullConfirmUnavailable as exc:
        # The composer's OWN reason, not one slug for all of them: four distinct
        # facts refuse a compose and a grader keys on the reason. The sentence is
        # verbatim too — the module that decided owns the words.
        _bank(
            spec.dsp_candidate(0.0), False, "",
            refusal=NullDoorRefused(exc.reason, str(exc)),
        )
        return failed(EXIT_REFUSED, exc.reason, _answer())

    coordinates = _coordinates(spec, args, delays_us)
    print(
        f"{len(coordinates)} coordinate(s) at {args.position} deg, "
        f"fc={fc_hz:g} Hz, stimulus {gain_db:g} dBFS, "
        f"sweep {plan.band_hz[0]:g}-{plan.band_hz[1]:g} Hz",
        file=sys.stderr,
    )

    # OUTSIDE, deliberately: `resolve_wired_mic` is a pure READ, so asking here
    # keeps a missing mic from costing the household a fader claim and a graph
    # swap first.
    mic = _resolve_mic()

    try:
        async with measurement_door(
            profile=MeasurementGraphProfile(
                preset=context.preset,
                topology=context.topology,
                role_channels=context.role_channels,
                playback_device=context.playback_device,
                protection_sections_by_role=_protection_sections(context),
            ),
            measurement_volume_db=context.session_volume_db,
            camilla_factory=primary_controller,
            action="confirming a reverse null",
            gate_owner=DOOR_GATE_OWNER,
        ) as door:
            # INSIDE: a write that runs before the interlock runs even when the
            # door refuses. This publishes `null_programs/stimulus.wav` under a
            # fixed name, so a refused second run would overwrite the bytes a
            # live run's artifact sha256 is bound to (#3393 B2).
            artifact = _publish_program(
                program, work_dir, "null_programs/stimulus.wav",
            )
            try:
                for index, (candidate, inverted) in enumerate(coordinates):
                    delays = (
                        {candidate.delay_target: candidate.delay_us}
                        if candidate.delay_target
                        else {}
                    )
                    # Per COORDINATE, not per session: each is a different
                    # graph. `install` is contracted idempotent, so the door's
                    # own open-time install costs one liveness read here.
                    fingerprint = await door.graph.install(
                        (args.inverted_role,) if inverted else (),
                        delays,
                        trims_db,
                    )
                    captured = await _play_and_capture(
                        context, door.plan, program, mic, artifact, work_dir,
                    )
                    mic_wav = (
                        work_dir / "null_programs" / f"capture_{index:02d}.wav"
                    )
                    mic_wav.write_bytes(captured)
                    try:
                        depth_db, span = _depth(mic_wav, program, plan, fc_hz)
                        outcome: dict[str, Any] = {
                            "depth_db": depth_db, "span": span,
                        }
                    except NullDoorRefused as exc:
                        outcome = {"refusal": exc}
                    _bank(
                        candidate, inverted, fingerprint,
                        wav_sha256=artifact.sha256, **outcome,
                    )
            except tuple(mid_run) as exc:
                # Any of the three can land BETWEEN two coordinates with rows
                # already on disk; a traceback would exit with no JSON while
                # those rows sit in null_runs/ unnamed (#3393 B4).
                raise NullRunInterrupted(
                    _mid_run_reason(mid_run, exc), str(exc),
                    [name for name, _row in written],
                ) from exc
    except SessionGraphError as exc:
        # `_give_back` can raise this OUTSIDE the loop's own catch: a clean
        # walk whose door exit failed to put the entry graph back. `written`
        # already holds every row this walk earned.
        raise NullRunInterrupted(
            REFUSE_GRAPH_LOST, str(exc), [name for name, _row in written],
        ) from exc

    unmeasured = [row for _name, row in written if row["status"] != "measured"]
    if unmeasured:
        # The row's OWN reason: the coordinate that could not be read decided
        # it, and a slug spelled here would be a second opinion about which.
        return failed(EXIT_REFUSED, unmeasured[0]["reason"], _answer())
    return answered({
        **_answer(),
        "next": (
            f"jasper-round-views delay-confirm {work_dir} --fc-hz {fc_hz:g}"
        ),
    })


def _protection_sections(context: Any) -> Mapping[str, Any] | None:
    """The confirmed per-role protection filters, from the applied profile.

    Derived on-box from the safety profile and the role targets, never from a
    flag: a door that let an operator name them could name them away.
    """
    from jasper.active_speaker.branch_chain import confirmed_protection_sections

    return confirmed_protection_sections(context.safety_profile, context.role_targets)


def _line(row: Mapping[str, Any]) -> str:
    """One operator line: the answer beside the basis it was read on."""
    if row["status"] == "refused":
        # The detail, not just the slug: the sentence comes verbatim from the
        # module that decided, and it is the only part an operator can act on.
        return (
            f"  {row['delay_us']:+.0f} us {row['polarity']}: "
            f"refused ({row['reason']}) — {row['detail']}"
        )
    clamped = [
        side for side, flag in
        (("lower", row["clamped_lo"]), ("upper", row["clamped_hi"])) if flag
    ]
    basis = (
        f"shoulders {row['shoulders_used'][0]:g}-{row['shoulders_used'][1]:g} Hz"
        + (f", {'+'.join(clamped)} clamped" if clamped else ", canonical")
    )
    ceiling = row["gap_ceiling_db"]
    cap = "" if ceiling is None else f", branch-gap ceiling {ceiling:g} dB"
    return (
        f"  {row['delay_us']:+.0f} us {row['polarity']}: "
        f"null {row['depth_db']:.1f} dB — {basis}{cap}"
    )


def build_parser() -> argparse.ArgumentParser:
    from jasper.active_speaker.crossover_v2.contracts import (
        DESIGN_AXIS_DEG,
        DRIVER_ROLES,
    )

    parser = argparse.ArgumentParser(
        prog="jasper-null",
        description=(
            "Play the summed reverse null and bank one row per coordinate. "
            "Measures only; grades nothing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PURPOSE\n"
            "  Play the reverse null and bank one self-contained JSON row per\n"
            "  coordinate under <bundle>/null_runs/, usually reached for the\n"
            "  polarity proof and the acoustic confirm after\n"
            "  jasper-round-views delay-landscape has printed where the null\n"
            "  should sit -- that verb computes the delay landscape, this\n"
            "  plays it and reports what the room actually did. Comparing\n"
            "  rows across a run IS the grading step; this tool grades\n"
            "  nothing itself.\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  - before jasper-round-views delay-landscape has printed a\n"
            "    coordinate grid -- a --delays value off that grid is\n"
            "    refused (a coordinate nobody proposed names a graph nobody\n"
            "    modelled)\n"
            "  - branches at very different sensitivities with no level\n"
            "    match applied -- an un-level-matched pair caps its own\n"
            "    null depth, and the depth is the whole reading\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-null --bundle-dir captures/xover-2026-08-30/session-1 \\\n"
            "      --fc-hz 1800\n"
            "\n"
            "EXIT CODES\n"
            "  Every exit -- ok or refused -- prints one JSON document on\n"
            "  stdout first, then a one-line human gloss on stderr; a\n"
            "  refusal is never silent on either channel.\n"
            "  0  EXIT_OK -- every coordinate played and banked\n"
            "  1  EXIT_REFUSED -- a coordinate could not be read, the walk\n"
            "     was interrupted (the rows it banked ride in detail), the\n"
            "     measurement door refused, or the capture/mic failed\n"
            "  2  EXIT_UNREADABLE -- a --delays coordinate off the proposed\n"
            "     grid, or the state file could not be read"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--bundle-dir",
        metavar="<round-dir>",
        help="a commissioning bundle directory; rows land in <bundle>/null_runs/ "
             "and the default --delays are read from its banked curves",
    )
    parser.add_argument(
        "--fc-hz", type=float, default=None,
        help="the crossover corner; read from the applied graph when omitted",
    )
    parser.add_argument(
        "--delays", type=_delay_list, default=None,
        help="signed microsecond coordinates, comma separated; the propose "
             "door's optimum and one neighbour either side when omitted. A "
             "value starting with '-' must use the --delays=-200,-100,0 form: "
             "argparse reads a leading dash as the next flag",
    )
    parser.add_argument(
        "--polarity", default=POLARITY_BOTH,
        choices=(POLARITY_BOTH, POLARITY_KEEP, POLARITY_INVERT),
        help="both plays the in-phase/inverted pair at delay 0 — the polarity "
             "proof — and the delayed coordinates inverted",
    )
    parser.add_argument(
        "--position", type=int, default=DESIGN_AXIS_DEG,
        help="the bearing being measured; the reverse null is a design-axis act",
    )
    parser.add_argument(
        "--inverted-role", default="tweeter", choices=sorted(DRIVER_ROLES),
        help="which branch the confirmation flips",
    )
    parser.add_argument("--upper-role", default="tweeter")
    parser.add_argument("--lower-role", default="woofer")
    parser.add_argument(
        "--path-difference-m", type=float, default=0.0,
        help="lower-driver path minus upper-driver path; 0.0 centres the "
             "half-period window on zero when geometry is undeclared",
    )
    parser.add_argument(
        "--step-us", type=float, default=None,
        help="grid step in microseconds; the shared walk's own default when "
             "omitted",
    )
    from jasper.active_speaker.crossover_v2.journey import (
        PHASE_LATERAL,
        PHASE_MEASURE,
    )

    parser.add_argument(
        "--phase", default=PHASE_MEASURE, choices=(PHASE_MEASURE, PHASE_LATERAL),
        help="which banked phase carries the curves the default delays read",
    )
    return parser


def _delay_list(value: str) -> list[float]:
    try:
        return [float(part) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--delays must be comma-separated microseconds, got {value!r}"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    from jasper.active_speaker.crossover_v2.door import MeasurementDoorRefused
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.audio_measurement.null_walk import NullWalkError
    from jasper.audio_measurement.wired_capture import WiredCaptureError

    from jasper.env_load import load_env_files
    from jasper.volume_coordinator import install_env_canonical_target_provider

    args = build_parser().parse_args(argv)
    configure_verbose_logging(verbose=args.verbose)
    # Installs this process's VolumeOwner and the canonical target the duck
    # release reads. Without it there is no SESSION_MEASUREMENT rank to claim
    # the fader through and the door REFUSES.
    load_env_files()
    install_env_canonical_target_provider()
    try:
        return asyncio.run(_run(args))
    except NullRunInterrupted as exc:
        # k rows are on disk and named, so the operator gets both halves: why
        # the run stopped, and which coordinates it did bank.
        return failed(EXIT_REFUSED, "interrupted", {
            "reason": exc.reason,
            "detail": exc.detail,
            "banked_row_ids": exc.banked,
        })
    except MeasurementDoorRefused as exc:
        return failed(EXIT_REFUSED, exc.reason, exc.detail)
    except CrossoverV2Refused as exc:
        # The applied profile could not answer an input, which lands before
        # anything is composed or held.
        return failed(EXIT_REFUSED, REFUSE_BOX_NOT_READY, str(exc))
    except WiredCaptureError as exc:
        # The mic half, landing BEFORE the door — no rows exist, so this is a
        # refusal rather than an interrupted walk.
        return failed(EXIT_REFUSED, REFUSE_CAPTURE_FAILED, str(exc))
    except NullWalkError as exc:
        # A coordinate off the shared walk's grid, which is the grid the
        # proposal was computed on: an input fault, not a refusal.
        return failed(EXIT_UNREADABLE, REFUSE_DELAY_OFF_GRID, str(exc))
    except OSError as exc:
        return failed(EXIT_UNREADABLE, REFUSE_STATE_UNREADABLE, str(exc))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
