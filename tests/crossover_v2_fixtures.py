# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.audio_routes import output_topology_store as output_topology_mod
from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker.plan_run import prepare_plan_captures

from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume
import asyncio
import sys
import pytest
from jasper.active_speaker import commission_wiring
from jasper.active_speaker import design_draft
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X
from jasper.active_speaker.playback_route import ACTIVE_PLAYBACK_DEVICE_ENV
from jasper.audio_routes.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)
from jasper.active_speaker.crossover_v2 import conductor_context as v2ctx
from jasper.web import correction_crossover_v2 as v2host

from tests.run_manifest_fixture import write_manifest
from tests.program_baseline_fixtures import fake_program_baselines

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.bundles import BUNDLE_SCHEMA_VERSION
from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING, Pose
from jasper.active_speaker.crossover_v2.take_impulses import IMPULSES_KEY, write_take_impulses
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session, V2FlowSeams, V2RecordPublishers
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_inline_session_spec,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.web.correction_run_host import compose_plan_program
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.evidence_grid import evidence_bins
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.recorded_impulse import RecordedImpulse
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    DriverResponse,
    GainPlan,
    ProgramAnalysis,
    solve_branch_trims,
)
from jasper.audio_measurement.program_analysis.dispatch import MEASURE_PAIR_SINGLE_DRIVER
from jasper.audio_measurement.program_analysis.model import (
    AlignmentEstimate,
    CrossoverCandidate,
    DriftEstimate,
    PilotObservation,
    RoleGainSolve,
    SegmentLocation,
)
from jasper.audio_measurement.program_analysis.verify_integrity import _verify_capture_integrity
from jasper.web.correction_crossover_v2_wired import WiredCaptureAnswer

from tests.active_speaker_fixtures import empty_protection
from tests.test_active_speaker_profile import _two_way_preset
from jasper.active_speaker.crossover_section import CrossoverSection, sections_by_role
from jasper.active_speaker.branch_chain import crossover_response_db

SESSION = "cap_test_session_1"

FC_HZ = 1600.0

SESSION_VOLUME_DB = -20.0

#: The fader a measurement door must give back; unlike SESSION_VOLUME_DB, so the give-back shows.
HOUSEHOLD_DB = -14.0

CAPS = {"woofer": 0.0, "tweeter": -65.0}


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


WAY1_BAND = FrequencyBand(45.0, 18000.0)


def _roles_way1() -> list[RoleBand]:
    return [RoleBand("full_range", 0, WAY1_BAND)]


def _one_way_preset() -> ActiveSpeakerPreset:
    """The preset the PRODUCTION resolver answers for a subless passive box."""
    from jasper.active_speaker import commission_wiring
    from tests.active_speaker_fixtures import mono_output_topology

    return commission_wiring.resolve_capture_preset(
        mono_output_topology(mode="full_range_passive")
    )


def _loc(segment_id: str, kind: str = "sweep", *, confidence: float = 0.9,
         clipped: bool = False, residual_samples: float = 0.0) -> SegmentLocation:
    return SegmentLocation(
        segment_id=segment_id, kind=kind, role=None,
        scheduled_start=0, located_start=0, residual_samples=residual_samples,
        confidence=confidence, peak_dbfs=-12.0, clipped=clipped,
    )


_SUMMED_FREQS_HZ = np.linspace(100.0, 20000.0, 64)


def _in_room_summed_db() -> np.ndarray:
    octaves = np.log2(_SUMMED_FREQS_HZ / 1000.0)
    return -2.0 * octaves - 3.0 * np.exp(-0.5 * (octaves / 0.8) ** 2)


_FIXTURE_TRUSTED_BAND_HZ = (float(_SUMMED_FREQS_HZ[0]), float(_SUMMED_FREQS_HZ[-1]))


def _driver_response(
    role: str, window_ms: float, *, summed_db: np.ndarray | None = None,
    floor_source: str | None = None,
    trusted_band_hz: tuple[float, float] | None = _FIXTURE_TRUSTED_BAND_HZ,
) -> DriverResponse:
    if summed_db is not None:
        magnitude_db = np.asarray(summed_db, dtype=float)
    else:
        magnitude_db = _in_room_summed_db() if role == "summed" else np.zeros(64)
    complex_tf = (10.0 ** (magnitude_db / 20.0)).astype(complex)
    return DriverResponse(
        role=role, freqs_hz=_SUMMED_FREQS_HZ, magnitude_db=magnitude_db,
        complex_tf=complex_tf, ungated_tf=complex_tf[evidence_bins(_SUMMED_FREQS_HZ)],
        gating={
            "applied": True, "window_ms": window_ms,
            **({"floor_source": floor_source} if floor_source else {}),
            **(
                {"pre_post_gate_delta": {
                    "eval_band_hz": [float(trusted_band_hz[0]),
                                     float(trusted_band_hz[1])],
                }}
                if trusted_band_hz is not None else {}
            ),
        },
        snr=None, validity_floor_hz=None,
    )


_LINEARIZABLE_FREQS_HZ = np.linspace(100.0, 20000.0, 2048)

_FIXTURE_FC_HZ = 1600.0


def _linearizable_response(
    role: str, magnitude_db: np.ndarray, *,
    n_repeats: int = 2, validity_floor_hz: float = 140.0,
) -> DriverResponse:

    complex_tf = (10.0 ** (magnitude_db / 20.0)).astype(complex)

    def make() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=_LINEARIZABLE_FREQS_HZ, magnitude_db=magnitude_db,
            complex_tf=complex_tf, ungated_tf=complex_tf[evidence_bins(_LINEARIZABLE_FREQS_HZ)],
            gating={"applied": True, "window_ms": 8.0},
            snr=None, validity_floor_hz=validity_floor_hz,
        )

    return replace(make(), repeat_responses=tuple(make() for _ in range(n_repeats)))


def _check_analysis(
    program, *, linearity=True, channel_map=True, snr_floor_ok=True,
    locate_confidence=0.9, pilot_snr_ok=None,
) -> ProgramAnalysis:
    return ProgramAnalysis(
        phase="check",
        stimulus_id=program.stimulus_id,
        locations=(
            _loc("pilot_woofer_hi", "pilot", confidence=locate_confidence),
        ),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        linearity_ok=linearity,
        channel_map_ok=channel_map,
        pilot_snr_ok=pilot_snr_ok,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0,
            snr_floor_ok=snr_floor_ok,
        ),
    )


def _alignment(
    *, delay_us=150.0, status=ALIGNMENT_OK, polarity="normal", confidence=0.8,
    anchor_delay_us=None,
) -> AlignmentEstimate:
    return AlignmentEstimate(
        delay_us=delay_us, raw_delay_us=delay_us, parallax_us=11.0,
        polarity=polarity, polarity_sign=1 if polarity == "normal" else -1,
        polarity_agrees_with_sum=True, confidence=confidence, status=status,
        anchor_delay_us=anchor_delay_us,
    )


def _measure_analysis(
    program, *, glitch=False, clipped=False, linearity=True,
    alignment=None, locate_confidence=0.9, gate_ms=8.0,
    predicted_ripple_db=0.8, sweep_locations=None, pilot_snr_ok=None,
    mic_calibrated=None,
) -> ProgramAnalysis:
    freqs = np.linspace(100.0, 20000.0, 64)
    locations = (
        sweep_locations if sweep_locations is not None else (
            _loc("sweep_w", confidence=locate_confidence, clipped=clipped),
            _loc("sweep_t", confidence=locate_confidence),
            _loc("sweep_w_rep", confidence=locate_confidence),
        )
    )
    return ProgramAnalysis(
        phase="measure",
        stimulus_id=program.stimulus_id,
        locations=locations,
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=glitch,
        ),
        driver_responses=(
            _driver_response("woofer", gate_ms),
            _driver_response("tweeter", gate_ms + 1.0),
        ),
        alignment=alignment if alignment is not None else _alignment(),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.1, "tweeter": 0.0},
            polarity="normal", delay_us=150.0,
            predicted_ripple_db=predicted_ripple_db, confidence=0.8,
        ),
        linearity_ok=linearity,
        pilot_snr_ok=pilot_snr_ok,
        predicted_sum=(freqs, np.zeros(64)),
        glitch_detected=glitch,
        mic_calibrated=mic_calibrated,
    )


def _verify_pilot(hi_dbfs: float) -> PilotObservation:
    return PilotObservation(
        role="summed", level_lo_dbfs=hi_dbfs - 10.0, level_hi_dbfs=hi_dbfs,
        programmed_delta_db=10.0, captured_delta_db=10.0,
        linearity_ok=True, channel_map_ok=True,
    )


_INTEGRITY_FROM_LOCATIONS = object()


def _verify_analysis(
    program, *, max_db=0.9, gate_ms=8.5, linearity=True, locate_confidence=0.9,
    pilot_hi_dbfs=None, summed_db=None,
    pilot_snr_ok=None, floor_source=None, residual_samples=0.0,
    n_graded_bins=120,
    integrity=_INTEGRITY_FROM_LOCATIONS,
    verify_absolute=None,
    trusted_band_hz: tuple[float, float] | None = _FIXTURE_TRUSTED_BAND_HZ,
) -> ProgramAnalysis:
    locations = (
        _loc(
            "sweep_verify", "summed_sweep",
            confidence=locate_confidence, residual_samples=residual_samples,
        ),
    )
    if integrity is _INTEGRITY_FROM_LOCATIONS:
        integrity = _verify_capture_integrity(
            program, program.sample_rate_hz, locations,
            reconcile_capture_frames(None, received_frames=0),
        )
    return ProgramAnalysis(
        phase="verify",
        stimulus_id=program.stimulus_id,
        locations=locations,
        capture_integrity=integrity,
        glitch_detected=bool(integrity is not None and integrity.glitched),
        summed_response=_driver_response(
            "summed", gate_ms, summed_db=summed_db, floor_source=floor_source,
            trusted_band_hz=trusted_band_hz,
        ),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4,
            "max_db": max_db,
            "max_db_notch_excluded": max_db,
            "frame": {"n_bins": n_graded_bins},
        },
        verify_absolute=verify_absolute,
        linearity_ok=linearity,
        pilot_snr_ok=pilot_snr_ok,
        pilots=(
            (_verify_pilot(pilot_hi_dbfs),)
            if pilot_hi_dbfs is not None else ()
        ),
    )


@dataclass
class FakeSeams:
    """Recorder seams; per-phase analysis factories are swappable mid-test."""

    check: Any = _check_analysis
    measure: Any = _measure_analysis
    verify: Any = _verify_analysis
    analyzed: list = field(default_factory=list)
    published_checks: list = field(default_factory=list)

    def seams(self) -> V2FlowSeams:
        def analyze(program, result, priors, geometry, *, phase=None):
            self.analyzed.append((phase, program.phase, result, priors, geometry))
            factory = {
                "check": self.check, "measure": self.measure, "verify": self.verify,
            }[program.phase]
            return factory(program)

        return V2FlowSeams(
            analyze=analyze,
            records=V2RecordPublishers(
                check=lambda plan, ambient: self.published_checks.append(plan),
            ),
        )


def _fixture_applied_profile(
    trim_db: dict[str, float] | None = None,
    *,
    linearization: dict[str, Any] | None = None,
    delay_ms: dict[str, float] | None = None,
    inverted: dict[str, bool] | None = None,
    fc_hz: float = FC_HZ,
) -> dict[str, Any]:
    preset = _two_way_preset()
    preset["crossover_regions"] = [
        {**region, "fc_hz": float(fc_hz)}
        for region in preset["crossover_regions"]
    ]
    return {
        "status": "applied",
        "recomposition_snapshot": {
            "preset": preset,
            "corrections": {
                role: {
                    "gain_db": float((trim_db or _FIXTURE_RAW_TRIM_DB)[role]),
                    "delay_ms": float((delay_ms or {}).get(role, 0.0)),
                    "inverted": bool((inverted or {}).get(role, False)),
                }
                for role in (trim_db or _FIXTURE_RAW_TRIM_DB)
            },
            "linearization": dict(linearization or {}),
        },
    }


def _conductor(
    fakes: FakeSeams,
    *,
    roles_bands: list[RoleBand] | None = None,
    fc_hz: float | None = FC_HZ,
    driver_caps_dbfs: Mapping[str, float] | None = None,
    driver_spacing_m: float = 0.15,
    index_phase_map: Mapping[int, str] | None = None,
    gain_plan_db: Mapping[str, float] | None = None,
    **kwargs,
) -> CrossoverV2Session:
    seams = kwargs.pop("seams", fakes.seams())
    source_preset = kwargs.pop("source_preset", _preset())
    kwargs.setdefault("measurement_protection_sections_by_role", empty_protection(source_preset))
    supplied_prior = "timing_prior" in kwargs
    if index_phase_map is None:
        index_phase_map = {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: journey.PHASE_VERIFY}
    conductor = CrossoverV2Session(
        session_id=kwargs.pop("session_id", SESSION),
        source_preset=source_preset,
        roles_bands=_roles() if roles_bands is None else roles_bands,
        fc_hz=fc_hz,
        driver_caps_dbfs=CAPS if driver_caps_dbfs is None else driver_caps_dbfs,
        session_volume_db=SESSION_VOLUME_DB,
        seams=seams,
        driver_spacing_m=driver_spacing_m,
        index_phase_map=index_phase_map,
        **kwargs,
    )
    if gain_plan_db:
        conductor._gain_plan_db = dict(gain_plan_db)
    if not supplied_prior and journey.PHASE_TIMING not in index_phase_map.values():
        conductor.set_timing_prior("fixture-timing-take")
    return conductor


def _way1_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Session:
    return _conductor(
        fakes,
        roles_bands=_roles_way1(),
        fc_hz=None,
        driver_caps_dbfs={"full_range": 0.0},
        driver_spacing_m=0.0,
        source_preset=kwargs.pop("source_preset", _one_way_preset()),
        **kwargs,
    )


def _capture() -> WiredCaptureAnswer:
    return WiredCaptureAnswer(wav=b"fake-wav")


def _candidate_sections(conductor, fc_hz: float) -> dict:
    from dataclasses import replace

    return {
        role: tuple(replace(section, fc_hz=float(fc_hz)) for section in sections)
        for role, sections in sections_by_role(
            getattr(conductor._preset, "crossover_regions", ()) or ()
        ).items()
    }


def _phase_program(conductor, phase, spec=None):
    """What a run plays for a take of ``phase``: its spec through the door's composer.
    With no spec, CHECK, MEASURE and a lateral pose play the drivers graph and a
    summed take the timing graph, each at the level reference's own scope."""
    if spec is None:
        summed = phase not in (PHASE_CHECK, PHASE_MEASURE, journey.PHASE_LATERAL)
        spec = MeasureSpec(kind="baseline", scope_gains_db={},
                           **({"graph_scope": "timing", "candidate_id": "base"} if summed else {}))
    return compose_plan_program(conductor, replace(spec, program_phase=phase), None)


def _run_phase(conductor, index, attempt, result=None):
    from jasper.active_speaker.run_manifest import RunManifest
    from jasper.web.correction_run_host import bind_plan_analysis

    conductor.authorize_begin(index, attempt)
    phase = conductor.phase_of_index(index)
    program = _phase_program(conductor, phase, conductor._measure_specs_by_index.get(index))
    manifest = RunManifest(conductor.session_id, SimpleNamespace())
    manifest.begin(
        {"index": index, "candidate_id": "base", "pose": {"kind": "bearing", "azimuth_deg": 0}, "purpose": "speaker", "purposes": ["speaker"]},
        attempt=attempt,
        pose_index=0,
    )
    records = SimpleNamespace()
    analyze, assess = bind_plan_analysis(
        conductor, records, manifest=manifest, evidence={}
    )
    record = {
        "take_id": f"take-{index}-{attempt}",
        "index": index,
        "attempt": attempt,
        "phase": phase,
        "program": program.to_dict(),
    }
    records.enrich(result if result is not None else _capture(), record)
    verdict = assess(analyze(record), phase=program.phase, program=program)
    records.after_bank(record, record["take_id"])
    return verdict


def _snr_pilot(role: str, snr_db: float) -> PilotObservation:
    return PilotObservation(
        role=role, level_lo_dbfs=-40.0, level_hi_dbfs=-30.0,
        programmed_delta_db=10.0, captured_delta_db=10.0,
        linearity_ok=True, channel_map_ok=True,
        snr_valid=math.isfinite(snr_db) or snr_db > 0, snr_db=snr_db,
    )


def _dummy_program():
    from jasper.audio_measurement.program import build_check_program

    return build_check_program(_roles(), ambient_s=0.5, pilot_duration_s=0.3)


def _pilot_obs(
    role: str, *,
    snr_db: float = 20.0,
    captured_delta_db: float = 10.0,
    programmed_delta_db: float = 10.0,
    target_rise_db: float | None = 18.0,
    cross_rise_db: float | None = 1.0,
    snr_valid: bool = True,
    linearity_ok: bool = True,
    channel_map_ok: bool = True,
    peak_hi_dbfs: float = -24.0,
    delta_implausible: bool = False,
    mic_meter_status: str | None = "usable",
) -> PilotObservation:
    return PilotObservation(
        role=role, level_lo_dbfs=-40.0, level_hi_dbfs=-30.0,
        programmed_delta_db=programmed_delta_db, captured_delta_db=captured_delta_db,
        linearity_ok=linearity_ok, channel_map_ok=channel_map_ok, snr_valid=snr_valid,
        snr_db=snr_db, peak_hi_dbfs=peak_hi_dbfs,
        channel_map_target_rise_db=target_rise_db,
        channel_map_cross_rise_db=cross_rise_db,
        delta_implausible=delta_implausible, mic_meter_status=mic_meter_status,
    )


def _check_analysis_with_solves(program, *, snr_floor_ok=True, pilot_snr_ok=True):
    """A CHECK analysis whose gain plan carries #1825 per-role solves."""
    return ProgramAnalysis(
        phase="check", stimulus_id=program.stimulus_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(_pilot_obs("woofer"), _pilot_obs("tweeter")),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=pilot_snr_ok,
        gain_plan=GainPlan(
            gain_db={"woofer": -19.0, "tweeter": -31.0},
            predicted_peak_dbfs=-19.0, snr_floor_ok=snr_floor_ok,
            role_solves={
                "woofer": RoleGainSolve(
                    role="woofer", gain_db=-19.0, flat_target_gain_db=-11.0,
                    bound_by="room_snr", band_hz=(150.0, 2000.0),
                    ambient_dbfs=-60.0, required_snr_db=41.0,
                    required_capture_dbfs=-19.0,
                ),
                "tweeter": RoleGainSolve(
                    role="tweeter", gain_db=-31.0, flat_target_gain_db=-13.0,
                    bound_by="room_snr", band_hz=(1500.0, 20000.0),
                    ambient_dbfs=-72.0, required_snr_db=41.0,
                    required_capture_dbfs=-31.0,
                ),
            },
        ),
    )


def _resp_with_repeats(role: str, n_repeats: int) -> DriverResponse:
    freqs = np.linspace(150.0, 20000.0, 256)
    mag = np.zeros_like(freqs)

    def make() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=freqs, magnitude_db=mag,
            complex_tf=np.ones_like(freqs, dtype=complex),
            gating={}, snr=None, validity_floor_hz=140.0,
        )

    repeats = tuple(make() for _ in range(n_repeats))
    return DriverResponse(
        role=role, freqs_hz=freqs, magnitude_db=mag,
        complex_tf=np.ones_like(freqs, dtype=complex),
        gating={}, snr=None, validity_floor_hz=140.0,
        repeat_responses=repeats,
    )


def _fixture_branch_db() -> tuple[np.ndarray, np.ndarray]:
    freqs = _LINEARIZABLE_FREQS_HZ
    woofer_db = np.clip(-1.5 * np.log2(np.maximum(freqs, 1.0) / 1600.0), -6.0, 6.0)
    woofer_db = woofer_db - 6.0 * np.exp(
        -0.5 * ((np.log2(freqs / 400.0) / 0.3) ** 2)
    )
    tweeter_db = 3.0 * np.exp(-0.5 * ((np.log2(freqs / 2400.0) / 0.25) ** 2))
    woofer_db = woofer_db + crossover_response_db(
        freqs, (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),),
    )
    tweeter_db = tweeter_db + crossover_response_db(
        freqs, (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),),
    )
    return woofer_db, tweeter_db


def _solve_fixture_raw_trim(
    woofer_db: np.ndarray | None = None, tweeter_db: np.ndarray | None = None,
) -> dict[str, float]:
    freqs = _LINEARIZABLE_FREQS_HZ
    if woofer_db is None or tweeter_db is None:
        default_woofer_db, default_tweeter_db = _fixture_branch_db()
        woofer_db = default_woofer_db if woofer_db is None else woofer_db
        tweeter_db = default_tweeter_db if tweeter_db is None else tweeter_db
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (np.asarray(woofer_db) / 20.0)).astype(complex),
        (10.0 ** (np.asarray(tweeter_db) / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    return {"woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3)}


_FIXTURE_RAW_TRIM_DB = _solve_fixture_raw_trim()


def _way1_measure_analysis(program) -> ProgramAnalysis:
    freqs = _LINEARIZABLE_FREQS_HZ
    magnitude_db = (
        -5.0 * np.exp(-0.5 * ((np.log2(freqs / 400.0) / 0.3) ** 2))
        + 3.0 * np.exp(-0.5 * ((np.log2(freqs / 4000.0) / 0.25) ** 2))
    )
    solo = _linearizable_response("full_range", magnitude_db, n_repeats=2)
    return ProgramAnalysis(
        phase="measure",
        stimulus_id=program.stimulus_id,
        locations=(_loc("sweep_w"), _loc("sweep_w_rep")),
        drift=DriftEstimate(
            epsilon_ppm=5.0, max_residual_samples=0.1, glitch_detected=False,
        ),
        mic_tier="reference",
        driver_responses=(solo,),
        alignment=None,
        candidate=None,
        measure_pair_not_evaluated=MEASURE_PAIR_SINGLE_DRIVER,
        linearity_ok=True,
        predicted_sum=(solo.freqs_hz, solo.magnitude_db),
        glitch_detected=False,
    )


def _absolute(max_db, *, band=(1000.0, 4000.0), worst_db=None, worst_hz=1700.0):
    """A kernel ``verify_absolute`` record, in the shape the analyzer emits."""
    return {
        "band_hz": [band[0], band[1]],
        "rms_db": max_db / 2.0,
        "max_db": max_db,
        "worst_db": -max_db if worst_db is None else worst_db,
        "worst_hz": worst_hz,
        "n_bins": 16384,
    }


CAPTURE_RATE = 48_000

CAPTURE_AZIMUTHS_DEG = (-22.0, -7.0, 0.0, 7.0, 22.0)
#: Where :func:`bank_capture_round` banks its take records, under the bundle.
CAPTURE_RECORDS = "evidence/v1/artifacts/crossover_v2/wired-test/positions"


def bank_capture_round(
    root: Path,
    irs: Sequence[np.ndarray],
    *,
    capture_ids: Sequence[str] | None = None,
    positions_deg: Sequence[float] | None = None,
    vertical_deg: int = 0,
    distance_m: float | None = 1.0,
    radiated_band_hz: tuple[float, float] | None = (150.0, 20000.0),
    kept_role: str | None = "summed",
) -> Path:
    """One take per impulse response, banked as a record that names its
    recording and keeps its impulse as ``kept_role``'s (ADR-0354), origin at
    sample 0: ``None`` keeps none, and a driver role keeps no summed one, as a
    MEASURE take does. No recording is written, so a reader that opens one fails."""
    bundle = root / "bundle" / "b0"
    records = bundle / CAPTURE_RECORDS
    records.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": "b0", "bundle_schema_version": BUNDLE_SCHEMA_VERSION}))
    for index, ir in enumerate(irs):
        capture_id = f"cloud_verify_{index:02d}" if capture_ids is None else capture_ids[index]
        wav_path = f"summed/summed_{capture_id}.wav"
        role = kept_role or "summed"
        kept = SimpleNamespace(role=role, repeat_index=None, repeat_responses=(), impulse=RecordedImpulse(
            np.asarray(ir, dtype=np.float32), CAPTURE_RATE, origin_index=0, segment_id="sweep_verify"))
        doc: dict[str, Any] = {
            "kind": POSITION_EVIDENCE_KIND,
            "take_id": capture_id,
            "phase": "cloud_verify",
            "wav_path": wav_path,
            "wav_sha256": hashlib.sha256(wav_path.encode()).hexdigest(),
            "position_deg": (
                CAPTURE_AZIMUTHS_DEG[index % len(CAPTURE_AZIMUTHS_DEG)]
                if positions_deg is None
                else float(positions_deg[index])
            ),
            "vertical_deg": vertical_deg,
            "pose_kind": POSE_KIND_BEARING,
            "mark_distance_m": distance_m,
            "provenance": {"stimulus": {"phase": "verify", "wav_sha256": "c" * 64}},
        }
        if radiated_band_hz is not None:
            doc["curves"] = [{"role": role, "band_hz": list(radiated_band_hz)}]
        if kept_role is not None:
            doc[IMPULSES_KEY] = write_take_impulses(bundle, capture_id, SimpleNamespace(
                driver_responses=() if role == "summed" else (kept,), summed_response=kept if role == "summed" else None,
            ), recording=wav_path)
        (records / f"{capture_id}.json").write_text(json.dumps(doc))
    write_manifest(root)
    return root


def capture_record(root: Path, capture_id: str) -> Path:
    """The record :func:`bank_capture_round` banked for ``capture_id``."""
    return root / "bundle" / "b0" / CAPTURE_RECORDS / f"{capture_id}.json"


def fake_measurement_mic():
    from jasper.audio_measurement.wired_capture import WiredMicDevice

    return WiredMicDevice(
        card_id="UMIK2", card_index=9, usb_id="2752:0072",
        model_key="minidsp_umik2", model_label="miniDSP UMIK-2",
    )


class FakeCam:

    def __init__(
        self,
        entry_path: str | None,
        *,
        load_ok: bool = True,
        load_raises: Exception | None = None,
        volume_db: float = 0.0,
    ) -> None:
        self.entry_path = entry_path
        self.load_ok = load_ok
        self.load_raises = load_raises
        self.ops: list = []
        self.ducked: list[bool] = []
        self.live: str | None = None
        self.volume_db = volume_db

    @property
    def loaded(self) -> list[str]:
        return [op[1] for op in self.ops if isinstance(op, tuple) and op[0] == "set_raw"]

    async def get_config_file_path(self, *, best_effort: bool = False) -> str | None:
        self.ops.append("get_path")
        if self.entry_path is None:
            return None
        return str(self.entry_path)

    async def set_active_config_raw(
        self, config: str, *, best_effort: bool = False, duck: bool = True,
    ) -> bool:
        self.ops.append(("set_raw", config))
        self.ducked.append(duck)
        if self.load_raises is not None:
            raise self.load_raises
        if not self.load_ok:
            return False
        self.live = config
        return True

    async def patch_config(self, patch: dict, *, best_effort: bool = False) -> bool:
        if not isinstance(patch, dict) or not patch:
            if best_effort:
                return False
            raise ValueError("patch must be a non-empty mapping")
        self.ops.append(("patch", patch))
        return True

    async def normalize_config_raw(self, config: str, *, best_effort: bool = False) -> str:
        return config

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str:
        return self.loaded[-1]

    async def get_volume_db(self, *, best_effort: bool = False) -> float:
        return self.volume_db

    async def set_volume_db(self, db: float, *, best_effort: bool = False) -> bool:
        self.volume_db = float(db)
        return True


_TWO_WAY_GROUP = [{
    "id": "mono",
    "label": "Mono",
    "kind": "mono",
    "mode": "active_2_way",
    "channels": [
        {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
        {
            "role": "tweeter",
            "physical_output_index": 1,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": True,
        },
    ],
}]


def _topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "t",
        "name": "n",
        "status": "draft",
        "hardware": {
            "device_id": HIFIBERRY_DAC8X.id,
            "device_label": "Test device",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": _TWO_WAY_GROUP,
        "routing": {"mono_group_id": "mono"},
    })


def with_rear_target(context: Any) -> Any:
    """``context`` on a box that also declares its rear woofer, as a box that
    offers a rear pair does: a branch take probes the rear alone (ADR-0403 §3)."""
    bands, caps = context.driver_bands, context.driver_caps_dbfs
    return replace(context, driver_bands={**bands, "woofer:rear": bands["woofer"]},
                   driver_caps_dbfs={**caps, "woofer:rear": caps["woofer"]})


def _status() -> dict[str, Any]:
    return {
        "active": True,
        "setup": {"status": "ready"},
        "targets": {
            "drivers": [
                {"role": "woofer", "target_fingerprint": "fp-woofer"},
                {"role": "tweeter", "target_fingerprint": "fp-tweeter"},
            ],
        },
    }


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path):
    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2state.set_state_path_for_tests(None)
    v2volume.set_volume_plan_for_tests(None)


def _jasper_modules_binding(symbol: str, value: Any):
    """Every imported ``jasper`` module whose ``symbol`` attribute IS ``value``."""
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("jasper"):
            continue
        if getattr(module, symbol, None) is value:
            yield module


@pytest.fixture(autouse=True)
def _production_host_seams(monkeypatch, tmp_path):
    from jasper.active_speaker import preflight_live
    from tests.test_preflight import ready_facts

    fake_program_baselines(monkeypatch)
    monkeypatch.setattr(preflight_live, "read_preflight_facts", lambda plan, **kw: ready_facts(plan))
    monkeypatch.setattr(v2host, "secrets", SimpleNamespace(
        token_hex=lambda _: "minted_by_this_stage", token_urlsafe=v2host.secrets.token_urlsafe))
    monkeypatch.setattr(v2host, "_resolve_prepare_wired_mic", fake_measurement_mic)
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    preset = load_active_speaker_preset()
    _originals = {
        "load_output_topology": output_topology_mod.load_output_topology,
        "resolve_capture_preset": commission_wiring.resolve_capture_preset,
        "load_design_draft": design_draft.load_design_draft,
        "resolve_driver_excitation_ceilings": (
            excitation_safety_plan_mod.resolve_driver_excitation_ceilings
        ),
    }
    _home = {
        "load_output_topology": output_topology_mod,
        "resolve_capture_preset": commission_wiring,
        "load_design_draft": design_draft,
        "resolve_driver_excitation_ceilings": excitation_safety_plan_mod,
    }
    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda *a, **k: _topology())
    monkeypatch.setattr(commission_wiring, "resolve_capture_preset", lambda topo: preset)
    monkeypatch.setattr(
        design_draft, "load_design_draft",
        lambda **kw: {"driver_safety_profile": {
            "targets": [
                {
                    "role": role,
                    "target_fingerprint": f"fp-{role}",
                    "required_protection_filters": [{
                        "kind": kind,
                        "cutoff_hz": cutoff,
                        "minimum_slope_db_per_octave": 24.0,
                    }],
                }
                for role, kind, cutoff in (
                    ("woofer", "lowpass", 6000.0), ("tweeter", "highpass", 300.0),
                )
            ],
        }},
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        lambda safety_profile, fingerprint, **kw: (
            FrequencyBand(20.0, 20000.0),
            90.0,
        ),
    )
    _fakes = {_symbol: getattr(_home[_symbol], _symbol) for _symbol in _originals}
    for _symbol, _original in _originals.items():
        for _module in _jasper_modules_binding(_symbol, _original):
            monkeypatch.setattr(_module, _symbol, _fakes[_symbol])
    monkeypatch.setattr(v2ctx, "ensure_crossover_preview_ready", lambda design_draft=None: None)
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "effective_sweep_duration_limit_s",
        lambda safety_profile, fingerprint: 6.0,
    )
    monkeypatch.delenv(ACTIVE_PLAYBACK_DEVICE_ENV, raising=False)
    monkeypatch.setattr(
        v2evidence, "open_v2_evidence_store",
        lambda topology: (_AcceptingStore(tmp_path / "bundle"), "bundle-test"),
    )
    v2volume.set_volume_plan_for_tests(
        SessionVolumePlan(state_path=tmp_path / "session_volume.json")
    )
    yield
    for _symbol, _original in _originals.items():
        for _module in _jasper_modules_binding(_symbol, _fakes[_symbol]):
            setattr(_module, _symbol, _original)


_MINTED_CAPTURE_SESSION_ID = "wired-minted_by_this_stage"


def _open_prepared(monkeypatch, prepared: Any, run=None) -> tuple[Any, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def _fake_mint(_device, spec):
        from jasper.web.correction_crossover_v2_wired import WiredOpened, WiredCaptureSession
        return WiredOpened(WiredCaptureSession(_MINTED_CAPTURE_SESSION_ID, spec, _device))

    def _fake_runner(conductor, **_kwargs):
        captured["conductor"] = conductor

        async def _run(_client, _pi_session):
            return None

        return run or _run

    monkeypatch.setattr(v2host, "_mint_wired_session", _fake_mint)
    monkeypatch.setattr(v2host, "_build_wired_run", _fake_runner)

    prepared.open()

    return captured["conductor"], (v2state.load_v2_state() or {})


def _inline_body():
    # The speaker program's timing take is the run's probe, which plays before CHECK (ADR-0403 §4).
    return {"request": {"program": "speaker/mark", "poses": [0]}}


def _stage_1(monkeypatch) -> tuple[Any, dict[str, Any]]:
    prepared = v2host.prepare_v2_session(
        _inline_body(), status=_status(), run_async=asyncio.run, camilla_factory=None
    )
    return _open_prepared(monkeypatch, prepared)


class _AcceptingStore:

    session_id = "bundle-test"

    def __init__(self, bundle_dir: Any) -> None:
        self.bundle_dir = str(bundle_dir)

    def publish_json_artifact(self, relpath: str, payload: Any) -> Any:
        return SimpleNamespace(fingerprint=f"fp-{relpath}", byte_size=0)

    def identify_artifact(self, relpath: str) -> Any:
        return SimpleNamespace(fingerprint=f"fp-{relpath}")


class _RecordingCheckStore:
    """An evidence store that keeps what was published through it."""

    session_id = "bundle-check-pin"
    bundle_dir = "/var/lib/jasper/bundle-check-pin"

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def publish_json_artifact(self, relpath: str, payload: Any) -> Any:
        self.published.append((relpath, payload))
        return SimpleNamespace(fingerprint="fp-check-pin", byte_size=0)

    def identify_artifact(self, relpath: str) -> Any:
        return SimpleNamespace(fingerprint="fp-check-pin")


def _regradable_fixture() -> tuple[Any, Any, Any]:
    import numpy as np

    freqs = np.logspace(math.log10(100.0), math.log10(20_000.0), 2048)
    commanded = np.full_like(freqs, 6.0)
    error = np.where((freqs >= 2_000.0) & (freqs <= 3_000.0), 6.0, 0.0)
    return freqs, commanded, error


_PERSISTED_TOP_LEVEL_KEYS = {
    "candidate",
    "evidence",
    "failure",
    "kind",
    "schema_version",
    "session_id",
    "updated_at",
}


def _session_from_real_open(monkeypatch, fakes) -> Any:
    from jasper.active_speaker.crossover_v2.door import OpenMeasurementDoor
    from jasper.audio_measurement.wired_capture import WiredSplMonitor

    captured = {}
    real_bind = v2host.bind_run_door
    monkeypatch.setattr(v2host, "bind_v2_engine_seams", lambda **kwargs: fakes.seams())
    def bind(**kwargs):
        binding, analyze, assessor = real_bind(**kwargs)
        monitor = WiredSplMonitor(binding.sensitivity, binding.ceiling_db_spl, 0)
        door = OpenMeasurementDoor(fakes.graph, fakes.volume, None, SESSION_VOLUME_DB, "graph", monitor)
        captured["tuning"] = binding.build_session(door, kwargs["manifest"].allocate_take_id)
        return binding, analyze, assessor
    monkeypatch.setattr(v2host, "bind_run_door", bind)
    captured["conductor"], _state = _stage_1(monkeypatch)
    return captured


def _inline_spec():
    request = ac.AngleCaptureRequest((ac.AngleStop(Pose(0, 0), ac.REGIME_PER_DRIVER, purpose="speaker"),))
    captures = prepare_plan_captures(request, roles_bands=_roles())
    return build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        roles_bands=_roles(), fc_hz=FC_HZ,
        acknowledgement_binding="b" * 24, retries_per_pose=0,
    )
