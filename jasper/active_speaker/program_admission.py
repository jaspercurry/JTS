# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Re-admit actual measurement WAVs against declared driver caps.

Driver programs retain per-segment isolated-driver admission. Summed programs
require a complete protected graph: runtime_contract proves topology, branch
headroom and wiring; declared protection filters justify only the frequencies
outside each driver's input band. Both paths attest actual PCM peak, schedule
silence and manifest agreement before verified playback.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.program import (
    PROGRAM_PHASE_CHECK,
    PROGRAM_PHASE_MEASURE,
    PROGRAM_PHASE_VERIFY,
    KIND_PILOT,
    KIND_SUMMED_SWEEP,
    PROGRAM_SAMPLE_RATE_HZ,
    ExcitationProgram,
    ProgramSegment,
    segment_emitted_band_hz,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from .driver_safety import evaluate_driver_safety_profile
from .driver_protection import PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
from .graph_safety import protection_requirement_present, view_from_emitted_text
from .measurement import active_driver_targets
from .runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph,
)
from .excitation_safety_plan import (
    DriverSweepGeneratorPlan,
    ExcitationSafetyPlanError,
    ExcitationSafetyPlanRefusal,
    PreparedDriverExcitationPlan,
    RequestedDriverExcitationPlan,
    prepare_driver_excitation_plan,
    resolve_driver_excitation_ceilings,
    effective_sweep_duration_limit_s,
)

logger = logging.getLogger(__name__)

# Out-of-segment energy on a program channel must sit below this RMS floor: a
# clean render is exact silence between stimuli, so any measurable energy here is
# a leak/tamper (contract: the attestation is about the artifact).
OUT_OF_SEGMENT_RMS_FLOOR_DBFS = -60.0

# The rendered per-channel true peak must match the manifest's declared peak
# (the loudest scheduled segment on that channel) within this tolerance. 0.5 dB
# absorbs int16 quantization while still catching gross composer/render drift.
CHANNEL_PEAK_TOLERANCE_DB = 0.5

_DBFS_FLOOR = 1e-12


class ProgramAdmissionRefusal(str, Enum):
    """Closed refusal vocabulary for one program admission."""

    PROFILE_NOT_CONFIRMED = "program_profile_not_confirmed"
    TARGET_NOT_MAPPED = "program_target_not_mapped"
    CHANNEL_ROLE_INCONSISTENT = "program_channel_role_inconsistent"
    SEGMENT_OUTSIDE_LIMITS = "program_segment_outside_limits"
    CHANNEL_PEAK_OVER_CAP = "program_channel_peak_over_cap"
    OUT_OF_SEGMENT_ENERGY = "program_out_of_segment_energy"
    MANIFEST_PEAK_MISMATCH = "program_manifest_peak_mismatch"
    RENDER_SHAPE_MISMATCH = "program_render_shape_mismatch"
    GATE_NOT_APPLIED = "program_gate_not_applied"
    GRAPH_NOT_PROVEN = "program_graph_not_proven"


class ProgramAdmissionError(ValueError):
    """The program or its admission inputs are structurally invalid."""


@dataclass(frozen=True)
class SegmentAdmission:
    """One non-silence segment's prepared-plan verdict."""

    segment_id: str
    role: str
    channel: int
    band: tuple[float, float]
    effective_peak_dbfs: float
    execution_allowed: bool
    refusals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "role": self.role,
            "channel": self.channel,
            "band_hz": [self.band[0], self.band[1]],
            "effective_peak_dbfs": self.effective_peak_dbfs,
            "execution_allowed": self.execution_allowed,
            "refusals": list(self.refusals),
        }


@dataclass(frozen=True)
class ChannelFacts:
    """One channel's whole-file attestation, recomputed from the PCM bytes."""

    channel: int
    role: str
    cap_dbfs: float
    session_volume_db: float
    declared_peak_dbfs: float
    true_peak_dbfs: float
    effective_true_peak_dbfs: float
    out_of_segment_rms_dbfs: float
    peak_within_cap: bool
    quiet_out_of_segment: bool
    peak_matches_manifest: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "role": self.role,
            "cap_dbfs": self.cap_dbfs,
            "session_volume_db": self.session_volume_db,
            "declared_peak_dbfs": self.declared_peak_dbfs,
            "true_peak_dbfs": self.true_peak_dbfs,
            "effective_true_peak_dbfs": self.effective_true_peak_dbfs,
            "out_of_segment_rms_dbfs": self.out_of_segment_rms_dbfs,
            "peak_within_cap": self.peak_within_cap,
            "quiet_out_of_segment": self.quiet_out_of_segment,
            "peak_matches_manifest": self.peak_matches_manifest,
        }


@dataclass(frozen=True)
class ProgramAdmission:
    """Aggregated admission for one excitation program (N segments + M channels)."""

    program_id: str
    phase: str
    session_volume_db: float
    segments: tuple[SegmentAdmission, ...]
    channels: tuple[ChannelFacts, ...]
    refusals: tuple[ProgramAdmissionRefusal, ...]

    @property
    def allowed(self) -> bool:
        return not self.refusals

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "jts_active_program_admission",
            "program_id": self.program_id,
            "phase": self.phase,
            "session_volume_db": self.session_volume_db,
            "segments": [segment.to_dict() for segment in self.segments],
            "channels": [channel.to_dict() for channel in self.channels],
            "refusals": [reason.value for reason in self.refusals],
            "allowed": self.allowed,
        }


def _dbfs(amplitude: float) -> float:
    return 20.0 * math.log10(max(float(amplitude), _DBFS_FLOOR))


def _channel_roles(program: ExcitationProgram) -> dict[int, str]:
    """Map each channel carrying a stimulus to its single role (fail-closed)."""

    roles: dict[int, str] = {}
    for segment in program.stimulus_segments():
        assert segment.channel is not None and segment.role is not None
        existing = roles.setdefault(segment.channel, segment.role)
        if existing != segment.role:
            raise _ChannelRoleInconsistent(segment.channel)
    return roles


class _ChannelRoleInconsistent(Exception):
    def __init__(self, channel: int) -> None:
        super().__init__(f"channel {channel} carries more than one role")
        self.channel = channel


def _requested_segment_plan(
    segment: ProgramSegment,
    *,
    target_fingerprint: str,
    session_volume_db: float,
    program_id: str,
) -> RequestedDriverExcitationPlan:
    assert segment.f1_hz is not None and segment.f2_hz is not None
    # The band this segment ACTUALLY emits, gate included. `f1_hz`/`f2_hz` are
    # the PARENT sweep's, which a gated channel keeps so the branches stay
    # sample-identical where both play; judging a gated segment by them refuses
    # a driver for frequencies the gate silences, and vice versa.
    emitted_f1, emitted_f2 = segment_emitted_band_hz(segment)
    amplitude = 10.0 ** (float(segment.gain_db) / 20.0)
    duration_s = segment.n_samples / PROGRAM_SAMPLE_RATE_HZ
    context = json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_program_segment_context",
            "program_id": program_id,
            "segment_id": segment.segment_id,
            "session_volume_db": session_volume_db,
        }
    )
    return RequestedDriverExcitationPlan(
        target_fingerprint=target_fingerprint,
        commissioning_context_fingerprint=context,
        generator=DriverSweepGeneratorPlan(
            f1_hz=emitted_f1,
            f2_hz=emitted_f2,
            amplitude=amplitude,
            duration_s=duration_s,
            repeat_count=1,
            commissioning_gain_db=0.0,
            main_volume_db=float(session_volume_db),
        ),
    )


def _gate_leak_ratio_ceiling(segment: ProgramSegment) -> float:
    """Largest out-of-gate RMS the gate's OWN SHAPE can account for, as a
    fraction of the segment's amplitude.

    Derived, not chosen: the quietest sound the gate may make anywhere is the
    first non-zero sample of its raised-cosine ramp, ``0.5*(1 - cos(pi/fade))``,
    so energy at or above that outside the window cannot be the gate's doing.

    A RATIO, because these programs are composed at the most restrictive driver
    cap — tens of dB down — so an ungated parent sweep leaking at full segment
    amplitude still passes a fixed -60 dBFS residual floor. Against the segment's
    own amplitude an unapplied gate reads about -3 dB versus a bound near -93 dB.

    ``fade == 0`` has no ramp to excuse anything, so only the sample format's
    own quantization step is allowed.
    """
    fade = segment.gate_fade_samples
    if fade <= 0:
        return 2.0 ** -31
    return 0.5 * (1.0 - math.cos(math.pi / float(fade)))


def _gate_leak_refusals(program: ExcitationProgram, pcm) -> list[str]:
    """Verify the RENDERED BYTES obey every gate their metadata claims.

    The second, independent leg of the gate contract. The first
    (:func:`segment_emitted_band_hz`) reads the SCHEDULE, so a renderer that
    silently stopped applying gates would leave the narrow claim intact while
    the full parent sweep reached the driver. Metadata cannot verify itself, so
    this measures the samples. Vacuous by construction for an ungated segment.
    """
    import numpy as np

    refusals: list[str] = []
    for segment in program.stimulus_segments():
        if not segment.is_gated or segment.channel is None:
            continue
        amplitude = 10.0 ** (float(segment.gain_db) / 20.0)
        if not amplitude > 0.0:
            continue
        start = segment.start_sample
        stop = start + segment.n_samples
        if stop > pcm.shape[0]:
            continue  # RENDER_SHAPE_MISMATCH already speaks for this program
        window = np.asarray(pcm[start:stop, segment.channel], dtype=np.float32)
        gate_end = (
            segment.n_samples if segment.gate_end_sample is None
            else segment.gate_end_sample
        )
        outside = np.concatenate(
            (window[: segment.gate_start_sample], window[gate_end:])
        )
        if outside.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(outside), dtype=np.float64)))
        if rms > amplitude * _gate_leak_ratio_ceiling(segment):
            refusals.append(segment.segment_id)
    return refusals


def _out_of_segment_mask(program: ExcitationProgram, channel: int, length: int) -> Any:
    """True where a channel is expected to be silent.

    Scoped to ``known_audible_segments()`` rather than ``stimulus_segments()``:
    the courtesy-tone prelude (#1677) is real audio outside any analyzed
    stimulus window, so it must be excluded here too or every prelude-bearing
    program is refused as if the tone were leaked energy.
    ``_channel_declared_peak_dbfs`` stays scoped to ``stimulus_segments()``.
    """
    import numpy as np

    mask = np.ones(length, dtype=bool)
    for segment in program.known_audible_segments():
        if segment.channel != channel:
            continue
        start = segment.start_sample
        end = min(length, segment.start_sample + segment.n_samples)
        if start < end:
            mask[start:end] = False
    return mask


def _channel_declared_peak_dbfs(program: ExcitationProgram, channel: int) -> float:
    """The manifest's expected true peak for one channel.

    Deliberately scoped to ``stimulus_segments()``, NOT
    ``known_audible_segments()``: the courtesy-tone prelude never exceeds its
    channel's loudest stimulus, so leaving it out keeps MANIFEST_PEAK_MISMATCH
    able to catch a tone rendered louder than intended, instead of the
    expectation rising to match whatever the tone did.
    """
    peaks = [
        float(segment.gain_db)
        for segment in program.stimulus_segments()
        if segment.channel == channel
    ]
    return max(peaks) if peaks else _dbfs(0.0)


def _channel_facts(
    program: ExcitationProgram, pcm: Any, *, channel: int, role: str,
    cap_dbfs: float, session_volume_db: float,
) -> tuple[ChannelFacts, list[ProgramAdmissionRefusal]]:
    import numpy as np

    reasons: list[ProgramAdmissionRefusal] = []
    column = np.asarray(pcm[:, channel], dtype=np.float32)
    true_peak = float(np.max(np.abs(column))) if column.size else 0.0
    true_peak_dbfs = _dbfs(true_peak)
    effective_true_peak_dbfs = true_peak_dbfs + float(session_volume_db)
    mask = _out_of_segment_mask(program, channel, column.size)
    residual = column[mask]
    rms = (
        float(np.sqrt(np.mean(np.square(residual), dtype=np.float64)))
        if residual.size
        else 0.0
    )
    out_of_segment_rms_dbfs = _dbfs(rms)
    declared_peak_dbfs = _channel_declared_peak_dbfs(program, channel)

    peak_within_cap = effective_true_peak_dbfs <= float(cap_dbfs) + 1e-9
    quiet_out_of_segment = (
        out_of_segment_rms_dbfs < OUT_OF_SEGMENT_RMS_FLOOR_DBFS
    )
    peak_matches_manifest = (
        abs(true_peak_dbfs - declared_peak_dbfs) <= CHANNEL_PEAK_TOLERANCE_DB
    )
    facts = ChannelFacts(
        channel=channel,
        role=role,
        cap_dbfs=float(cap_dbfs),
        session_volume_db=float(session_volume_db),
        declared_peak_dbfs=declared_peak_dbfs,
        true_peak_dbfs=true_peak_dbfs,
        effective_true_peak_dbfs=effective_true_peak_dbfs,
        out_of_segment_rms_dbfs=out_of_segment_rms_dbfs,
        peak_within_cap=peak_within_cap,
        quiet_out_of_segment=quiet_out_of_segment,
        peak_matches_manifest=peak_matches_manifest,
    )
    if not peak_within_cap:
        reasons.append(ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP)
    if not quiet_out_of_segment:
        reasons.append(ProgramAdmissionRefusal.OUT_OF_SEGMENT_ENERGY)
    if not peak_matches_manifest:
        reasons.append(ProgramAdmissionRefusal.MANIFEST_PEAK_MISMATCH)
    return facts, reasons


def _evaluate_program(
    program: ExcitationProgram,
    pcm: Any,
    *,
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> ProgramAdmission:
    refusals: list[ProgramAdmissionRefusal] = []
    segments: list[SegmentAdmission] = []
    channels: list[ChannelFacts] = []
    # ``(permitted_lo_hz, permitted_hi_hz, maximum_duration_s)`` per segment:
    # the LIMIT side of two comparisons below, which `SegmentAdmission` does not
    # carry because it records what was REQUESTED. Read only by the refusal log;
    # a segment whose plan raised gets no entry and its limits are omitted.
    segment_limits: dict[str, tuple[float, float, float]] = {}

    try:
        channel_roles = _channel_roles(program)
    except _ChannelRoleInconsistent:
        refusals.append(ProgramAdmissionRefusal.CHANNEL_ROLE_INCONSISTENT)
        channel_roles = {}

    # --- per-segment prepared plans -----------------------------------------
    for segment in program.stimulus_segments():
        role = segment.role or ""
        target_fingerprint = role_targets.get(role)
        if not target_fingerprint:
            refusals.append(ProgramAdmissionRefusal.TARGET_NOT_MAPPED)
            segments.append(
                SegmentAdmission(
                    segment_id=segment.segment_id,
                    role=role,
                    channel=int(segment.channel or 0),
                    band=segment_emitted_band_hz(segment),
                    effective_peak_dbfs=float(segment.effective_peak_dbfs),
                    execution_allowed=False,
                    refusals=(ProgramAdmissionRefusal.TARGET_NOT_MAPPED.value,),
                )
            )
            continue
        requested = _requested_segment_plan(
            segment,
            target_fingerprint=target_fingerprint,
            session_volume_db=session_volume_db,
            program_id=program.program_id,
        )
        try:
            # program_admission=True: a CHECK/MEASURE program's channel routing
            # carries each driver's crossover filter (the tweeter's protective
            # HP included) by construction — the proven-HP path.
            prepared = prepare_driver_excitation_plan(
                topology,
                safety_profile,
                requested,
                program_admission=True,
                declared_sensitivities=declared_sensitivities,
            )
        except ExcitationSafetyPlanError as exc:
            reason = _map_safety_plan_error(exc)
            refusals.append(reason)
            segments.append(
                SegmentAdmission(
                    segment_id=segment.segment_id,
                    role=role,
                    channel=int(segment.channel or 0),
                    band=segment_emitted_band_hz(segment),
                    effective_peak_dbfs=float(segment.effective_peak_dbfs),
                    execution_allowed=False,
                    refusals=(reason.value,),
                )
            )
            continue
        segments.append(_segment_admission(segment, prepared))
        segment_limits[segment.segment_id] = (
            prepared.limits.permitted_band.lower_hz,
            prepared.limits.permitted_band.upper_hz,
            prepared.limits.maximum_duration_s,
        )
        if not prepared.execution_allowed:
            refusals.append(ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS)

    # --- per-channel whole-file facts (recomputed from the PCM bytes) --------
    if pcm.ndim != 2 or pcm.shape[1] != program.channels:
        refusals.append(ProgramAdmissionRefusal.RENDER_SHAPE_MISMATCH)
    else:
        # The gate contract's SECOND leg, under the metadata one rather than
        # instead of it: the rendered samples must agree with the schedule's
        # narrow band claim judged above.
        leaked = _gate_leak_refusals(program, pcm)
        if leaked:
            log_event(
                logger,
                "active_speaker.program_admission",
                level=logging.ERROR,
                action="gate_not_applied",
                program_id=program.program_id,
                segments=",".join(leaked),
            )
            refusals.append(ProgramAdmissionRefusal.GATE_NOT_APPLIED)
        for channel in sorted(channel_roles):
            role = channel_roles[channel]
            target_fingerprint = role_targets.get(role)
            if not target_fingerprint:
                continue  # already refused above as TARGET_NOT_MAPPED
            try:
                # Same proven-HP path as the per-segment plans above: this is
                # the whole-file cap the rendered channel's true peak is
                # attested against.
                _band, cap_dbfs = resolve_driver_excitation_ceilings(
                    safety_profile,
                    target_fingerprint,
                    program_admission=True,
                    declared_sensitivities=declared_sensitivities,
                )
            except ExcitationSafetyPlanError as exc:
                refusals.append(_map_safety_plan_error(exc))
                continue
            facts, reasons = _channel_facts(
                program, pcm, channel=channel, role=role, cap_dbfs=cap_dbfs,
                session_volume_db=session_volume_db,
            )
            channels.append(facts)
            refusals.extend(reasons)

    # De-duplicate refusals while preserving first-seen order.
    seen: dict[ProgramAdmissionRefusal, None] = {}
    for reason in refusals:
        seen.setdefault(reason, None)
    unique_refusals = tuple(seen)

    admission = ProgramAdmission(
        program_id=program.program_id,
        phase=program.phase,
        session_volume_db=float(session_volume_db),
        segments=tuple(segments),
        channels=tuple(channels),
        refusals=unique_refusals,
    )
    if not admission.allowed:
        # WHICH segment, and the REQUESTED value beside the LIMIT it was judged
        # against, because the aggregate ``REQUEST_OUTSIDE_LIMITS`` cannot tell
        # its folded comparisons apart: a bench triage once read it as a woofer
        # level breach when the real refusal was DURATION (the synchronized
        # sweep rounds to the nearest phase-closing length, exceeding a declared
        # 4.0 s by 5.8 ms). The repeat count is not rendered because it cannot
        # be the failing comparison — every segment is fixed at one repeat.
        # `session_volume_db` is named because a segment's effective peak is its
        # digital gain PLUS that value.
        durations_s = {
            segment.segment_id: segment.n_samples / PROGRAM_SAMPLE_RATE_HZ
            for segment in program.stimulus_segments()
        }
        refused_text: list[str] = []
        for refused in segments:
            if not refused.refusals:
                continue
            limits = segment_limits.get(refused.segment_id)
            permitted = f"/permitted={limits[0]:.1f}-{limits[1]:.1f}" if limits else ""
            max_duration = f"/max={limits[2]:.4f}" if limits else ""
            refused_text.append(
                f"{refused.segment_id}:{refused.role}"
                f":eff={refused.effective_peak_dbfs:.3f}"
                f":band={refused.band[0]:.1f}-{refused.band[1]:.1f}{permitted}"
                f":dur={durations_s.get(refused.segment_id, 0.0):.4f}{max_duration}"
                f":{'|'.join(refused.refusals)}"
            )
        log_event(
            logger,
            "active_speaker.program_admission",
            level=logging.WARNING,
            result="refused",
            program_id=program.program_id,
            phase=program.phase,
            refusals=",".join(reason.value for reason in unique_refusals),
            segments_refused=";".join(refused_text),
            role_caps_dbfs=",".join(
                f"{facts.role}={facts.cap_dbfs:.3f}" for facts in channels
            ),
            session_volume_db=f"{float(session_volume_db):.3f}",
        )
    return admission


def _segment_admission(
    segment: ProgramSegment, prepared: PreparedDriverExcitationPlan
) -> SegmentAdmission:
    return SegmentAdmission(
        segment_id=segment.segment_id,
        role=segment.role or "",
        channel=int(segment.channel or 0),
        band=segment_emitted_band_hz(segment),
        effective_peak_dbfs=float(prepared.requested_plan.effective_peak_dbfs),
        execution_allowed=prepared.execution_allowed,
        refusals=tuple(reason.value for reason in prepared.refusals),
    )


def _map_safety_plan_error(exc: ExcitationSafetyPlanError) -> ProgramAdmissionRefusal:
    message = str(exc)
    if message == ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value:
        return ProgramAdmissionRefusal.TARGET_NOT_MAPPED
    if message == ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value:
        return ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED
    return ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS


def _validate_program(program: ExcitationProgram) -> None:
    if not isinstance(program, ExcitationProgram):
        raise ProgramAdmissionError("program must be an ExcitationProgram")
    if program.phase not in {PROGRAM_PHASE_CHECK, PROGRAM_PHASE_MEASURE}:
        raise ProgramAdmissionError(
            "driver admission requires CHECK/MEASURE; summed VERIFY requires "
            "readmit_summed_program_from_wav and a protected graph"
        )


def readmit_program_from_wav(
    program: ExcitationProgram,
    wav_path: str | Path,
    *,
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> ProgramAdmission:
    """Re-admit a program from a FRESH readback of its rendered WAV bytes.

    The play-time gate: reads the actual WAV, not the in-memory program, and
    re-runs the whole evaluation, so tampered bytes are caught before playback.
    A shape/rate/channel mismatch refuses fail-closed.
    """
    _validate_program(program)
    pcm = _read_program_pcm(program, wav_path)
    if pcm is None:
        return _refused_program(program, session_volume_db, ProgramAdmissionRefusal.RENDER_SHAPE_MISMATCH)
    return _evaluate_program(
        program, pcm, topology=topology, safety_profile=safety_profile,
        role_targets=role_targets, session_volume_db=session_volume_db,
        declared_sensitivities=declared_sensitivities,
    )


def _read_program_pcm(program: ExcitationProgram, wav_path: str | Path) -> Any:
    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(str(wav_path))
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    if (
        int(rate) != program.sample_rate_hz
        or data.shape != (program.total_samples, program.channels)
        or data.dtype != np.int16
    ):
        return None
    return data.astype(np.float32) / np.float32(32767.0)


def _refused_program(
    program: ExcitationProgram, session_volume_db: float,
    reason: ProgramAdmissionRefusal,
) -> ProgramAdmission:
    log_event(
        logger, "active_speaker.program_admission", level=logging.WARNING,
        result="refused", program_id=program.program_id, phase=program.phase,
        refusals=reason.value,
    )
    return ProgramAdmission(
        program.program_id, program.phase, session_volume_db, (), (), (reason,),
    )


def readmit_summed_program_from_wav(
    program: ExcitationProgram,
    wav_path: str | Path,
    *,
    graph_yaml: str,
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> ProgramAdmission:
    """Admit a mono summed artifact through its complete protected tuning graph.

    The runtime contract re-proves branch boost compensation and routing. A
    declared HP/LP or proven protective-slope crossover LP must cover an out-of-band
    segment; its full emitted band stays in evidence. The caller must prove
    this exact graph live while holding the DSP writer lock through playback.
    """
    if (
        not isinstance(program, ExcitationProgram)
        or program.phase != PROGRAM_PHASE_VERIFY or program.channels != 1
        or not program.stimulus_segments()
        or any(
            segment.channel != 0 or segment.kind not in (KIND_PILOT, KIND_SUMMED_SWEEP)
            or segment.role not in (None, "summed")
            for segment in program.stimulus_segments()
        )
    ):
        raise ProgramAdmissionError("summed admission requires a mono VERIFY program")
    if not math.isfinite(session_volume_db) or session_volume_db > 0:
        raise ProgramAdmissionError("summed admission requires a non-positive finite volume")
    evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not evaluation.confirmed_and_current:
        return _refused_program(program, session_volume_db, ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED)
    physical = {target["target_fingerprint"]: target for target in active_driver_targets(topology)}
    if (
        not role_targets or set(role_targets.values()) != set(physical)
        or any(physical[fingerprint]["role"] != role for role, fingerprint in role_targets.items())
    ):
        return _refused_program(program, session_volume_db, ProgramAdmissionRefusal.TARGET_NOT_MAPPED)
    graph = classify_camilla_graph(
        topology=topology, text=graph_yaml,
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
    )
    if not graph.allowed or graph.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
        return _refused_program(program, session_volume_db, ProgramAdmissionRefusal.GRAPH_NOT_PROVEN)
    view = view_from_emitted_text(graph_yaml)
    pcm = _read_program_pcm(program, wav_path)
    if pcm is None:
        return _refused_program(program, session_volume_db, ProgramAdmissionRefusal.RENDER_SHAPE_MISMATCH)
    caps: list[float] = []
    segments: list[SegmentAdmission] = []
    refusals: list[ProgramAdmissionRefusal] = []
    declared = {target["target_fingerprint"]: target for target in safety_profile["targets"]}
    for role, fingerprint in role_targets.items():
        try:
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile, fingerprint, program_admission=True,
                declared_sensitivities=declared_sensitivities,
            )
            duration = effective_sweep_duration_limit_s(safety_profile, fingerprint)
        except ExcitationSafetyPlanError as exc:
            return _refused_program(program, session_volume_db, _map_safety_plan_error(exc))
        caps.append(cap)
        output = physical[fingerprint]["output_index"]
        requirements = declared[fingerprint]["required_protection_filters"]
        if not all(protection_requirement_present(
            view, output_index=output, allowed_channels={output}, requirement=requirement,
        ) for requirement in requirements):
            return _refused_program(program, session_volume_db, ProgramAdmissionRefusal.GRAPH_NOT_PROVEN)
        for segment in program.stimulus_segments():
            low, high = segment_emitted_band_hz(segment)
            low_ok = low >= band.lower_hz or any(
                requirement["kind"] == "highpass"
                and requirement["cutoff_hz"] >= band.lower_hz
                for requirement in requirements
            )
            high_ok = high <= band.upper_hz or any(
                requirement["kind"] == "lowpass"
                and requirement["cutoff_hz"] <= band.upper_hz
                for requirement in requirements
            ) or protection_requirement_present(
                view, output_index=output, allowed_channels={output},
                requirement={
                    "kind": "lowpass", "cutoff_hz": band.upper_hz,
                    "family_or_equivalent": "equivalent_or_steeper",
                    "minimum_slope_db_per_octave": PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
                },
            )
            peak = float(segment.gain_db) + session_volume_db
            allowed = (
                low_ok and high_ok and peak <= cap
                and segment.n_samples / program.sample_rate_hz <= duration
            )
            reasons = () if allowed else (ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS.value,)
            segments.append(SegmentAdmission(
                segment.segment_id, role, 0, (low, high), peak, allowed, reasons,
            ))
            if not allowed:
                refusals.append(ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS)
    facts, channel_refusals = _channel_facts(
        program, pcm, channel=0, role="summed", cap_dbfs=min(caps),
        session_volume_db=session_volume_db,
    )
    refusals.extend(channel_refusals)
    admission = ProgramAdmission(
        program.program_id, program.phase, session_volume_db,
        tuple(segments), (facts,), tuple(dict.fromkeys(refusals)),
    )
    if not admission.allowed:
        log_event(
            logger, "active_speaker.program_admission", level=logging.WARNING,
            result="refused", program_id=program.program_id, phase=program.phase,
            refusals=",".join(reason.value for reason in admission.refusals),
        )
    return admission
