# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper import output_topology_store as output_topology_mod
from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker.plan_run import prepare_plan_captures

from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume
import asyncio
import sys
import pytest
from jasper.active_speaker import commission_wiring
from jasper.active_speaker import session_volume_plan as session_volume_plan_mod
from jasper.active_speaker import design_draft
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X
from jasper.output_topology import ACTIVE_PLAYBACK_DEVICE_ENV, OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.active_speaker.crossover_v2 import conductor_context as v2ctx
from jasper.web import correction_crossover_v2 as v2host

from tests.run_manifest_fixture import write_manifest

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.contracts import (
    REFERENCE_MARK_DESIGN_AXIS,
    ResponseCurve,
)
from jasper.active_speaker.crossover_v2.round_evidence import (
    EntryBaseline,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.capture_dispatch import SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
from jasper.active_speaker.crossover_v2.diagnostics import spec_report_for_predicted_sum
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session, V2FlowSeams, V2RecordPublishers
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_v2_cloud_index_phase_map,
    build_inline_session_spec,
    resolve_plan_shape,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import gating
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.sweep import synchronized_swept_sine, write_sweep_wav
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    MEASURE_PAIR_SINGLE_DRIVER,
    AlignmentEstimate,
    CrossoverCandidate,
    DriftEstimate,
    DriverResponse,
    GainPlan,
    PilotObservation,
    ProgramAnalysis,
    RoleGainSolve,
    SegmentLocation,
    _verify_capture_integrity,
    predicted_branch_sum,
    solve_branch_trims,
)
from jasper.active_speaker.flat_spec import spec_convergence_residual
from jasper.web.correction_crossover_v2_wired import WiredCaptureAnswer

from tests.test_active_speaker_profile import _two_way_preset

SESSION = "cap_test_session_1"

FC_HZ = 1600.0

SESSION_VOLUME_DB = -20.0

#: The fader a measurement door must give back; unlike SESSION_VOLUME_DB, so the give-back shows.
HOUSEHOLD_DB = -14.0

CAPS = {"woofer": 0.0, "tweeter": -65.0}

def plan_context() -> SimpleNamespace:
    targets = {role: f"fp-{role}" for role in CAPS}
    return SimpleNamespace(
        safety_profile={"targets": [
            {"target_fingerprint": fingerprint, "role": role}
            for role, fingerprint in targets.items()
        ]},
        role_targets=targets,
    )

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

_ROOM_SCALE_EXPECTED_RMS_DB = {0.4: 1.626, 1.0: 4.331, 2.5: 12.787}

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
    return DriverResponse(
        role=role, freqs_hz=_SUMMED_FREQS_HZ, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
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

    def make() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=_LINEARIZABLE_FREQS_HZ, magnitude_db=magnitude_db,
            complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
            gating={"applied": True, "window_ms": 8.0},
            snr=None, validity_floor_hz=validity_floor_hz,
        )

    repeats = tuple(make() for _ in range(n_repeats))
    return DriverResponse(
        role=role, freqs_hz=_LINEARIZABLE_FREQS_HZ, magnitude_db=magnitude_db,
        complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
        gating={"applied": True, "window_ms": 8.0},
        snr=None, validity_floor_hz=validity_floor_hz,
        repeat_responses=repeats,
    )

def _check_analysis(
    program, *, linearity=True, channel_map=True, snr_floor_ok=True,
    locate_confidence=0.9, pilot_snr_ok=None,
) -> ProgramAnalysis:
    return ProgramAnalysis(
        phase="check",
        program_id=program.program_id,
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
        program_id=program.program_id,
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

def _verify_pilot(hi_dbfs: float, *, programmed_hi_gain_db: float = -20.0) -> PilotObservation:
    return PilotObservation(
        role="summed", level_lo_dbfs=hi_dbfs - 10.0, level_hi_dbfs=hi_dbfs,
        programmed_delta_db=10.0, captured_delta_db=10.0,
        linearity_ok=True, channel_map_ok=True,
        programmed_hi_gain_db=programmed_hi_gain_db,
    )

_INTEGRITY_FROM_LOCATIONS = object()

def _verify_analysis(
    program, *, max_db=0.9, gate_ms=8.5, linearity=True, locate_confidence=0.9,
    pilot_hi_dbfs=None, programmed_hi_gain_db=-20.0, summed_db=None,
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
        program_id=program.program_id,
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
            (_verify_pilot(pilot_hi_dbfs, programmed_hi_gain_db=programmed_hi_gain_db),)
            if pilot_hi_dbfs is not None else ()
        ),
    )

def bank_into(
    sink: list[Any], *, with_capture: bool = False, phase: str | None = None,
) -> flow.BankTake:
    def bank_take(result: Any, record: Mapping[str, Any]) -> str:
        banked = dict(record)
        if phase is None or banked.get("phase") == phase:
            sink.append((result, banked) if with_capture else banked)
        return f"crossover_v2/fixture/positions/{banked.get('take_id') or ''}.json"

    return bank_take

def with_records(seams: V2FlowSeams, **overrides: Any) -> V2FlowSeams:
    """``seams`` with one or more of the five ``records`` publishers swapped."""
    return dataclasses.replace(
        seams, records=dataclasses.replace(seams.records, **overrides),
    )

@dataclass
class FakeSeams:
    """Recorder seams; per-phase analysis factories are swappable mid-test."""

    check: Any = _check_analysis
    measure: Any = _measure_analysis
    verify: Any = _verify_analysis
    analyzed: list = field(default_factory=list)
    published_checks: list = field(default_factory=list)
    published_candidates: list = field(default_factory=list)
    apply_done: bool = False
    apply_failed_code: str = ""
    rollback_available: Any = None
    banked_findings: list = field(default_factory=list)
    applied_boosts: bool = False
    applied_profile_state: Any = None

    def applied_profile(self) -> dict[str, Any]:
        return (
            self.applied_profile_state
            if self.applied_profile_state is not None
            else _fixture_applied_profile()
        )

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
                candidate=self.published_candidates.append,
                findings=self.banked_findings.append,
            ),
            apply_complete=lambda: self.apply_done,
            apply_failed=lambda: self.apply_failed_code,
            rollback_available=self.rollback_available,
            applied_boosts=lambda: self.applied_boosts,
            applied_profile=self.applied_profile,
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

_ENTRY_BASELINE_SCALE = 1.5

_ENTRY_BASELINE_RESIDUAL_DB = 6.877

_POST_APPLY_RESIDUAL_DB = 4.331

def _fixture_entry_baseline(conductor: CrossoverV2Session) -> EntryBaseline:
    measured = measured_response_from_analysis(
        _verify_analysis(
            conductor.program_for_phase(PHASE_VERIFY),
            summed_db=_in_room_summed_db() * _ENTRY_BASELINE_SCALE,
        ),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )
    return EntryBaseline.from_measurement(
        measured,
        graph_fingerprint="fixture_entry_graph",
        captured_at="2026-08-10T00:00:00Z",
    )

def _conductor(
    fakes: FakeSeams,
    *,
    roles_bands: list[RoleBand] | None = None,
    fc_hz: float | None = FC_HZ,
    driver_caps_dbfs: Mapping[str, float] | None = None,
    driver_spacing_m: float = 0.15,
    **kwargs,
) -> CrossoverV2Session:
    seams = kwargs.pop("seams", fakes.seams())
    source_preset = kwargs.pop("source_preset", _preset())
    supplied_baseline = "measure_entry_baseline" in kwargs
    conductor = CrossoverV2Session(
        session_id=kwargs.pop("session_id", SESSION),
        source_preset=source_preset,
        roles_bands=_roles() if roles_bands is None else roles_bands,
        fc_hz=fc_hz,
        driver_caps_dbfs=CAPS if driver_caps_dbfs is None else driver_caps_dbfs,
        session_volume_db=SESSION_VOLUME_DB,
        seams=seams,
        driver_spacing_m=driver_spacing_m,
        **kwargs,
    )
    if not supplied_baseline and (
        journey.PHASE_ENTRY_BASELINE not in conductor.session_phases
    ):
        conductor._measure_entry_baseline = _fixture_entry_baseline(conductor)
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

def _stage2_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Session:
    return _conductor(
        fakes,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        **kwargs,
    )

def _stage2_after_measure(fakes: FakeSeams) -> CrossoverV2Session:
    stage1 = _conductor(fakes)
    _run_phase(stage1, 1, 1)
    _run_phase(stage1, 2, 2)
    snapshot = stage1.snapshot()
    return _stage2_conductor(
        fakes,
        index_phase_map={3: PHASE_VERIFY},
        gain_plan_db=snapshot.gain_plan_db,
        measure_gain_ceiling_db=snapshot.measure_gain_ceiling_db,
        measure_predicted_sum=stage1.measure_predicted_sum,
        measure_predicted_spec_report=stage1.measure_predicted_spec_report,
        measure_commanded_delta=stage1.measure_commanded_delta,
        measure_declared_transfer=stage1.measure_declared_transfer,
        measure_proposal_fingerprint=stage1.measure_proposal_fingerprint,
        measure_entry_baseline=stage1.measure_entry_baseline,
        measure_alignment_objective=stage1.measure_alignment_objective,
        measure_gate_window_ms=stage1.measure_gate_window_ms,
        attempt_history=snapshot.attempt_history,
    )

def _capture() -> WiredCaptureAnswer:
    return WiredCaptureAnswer(wav=b"fake-wav")

def _configured_sections(conductor, role: str) -> tuple:
    from jasper.active_speaker.branch_chain import sections_by_role

    return sections_by_role(
        getattr(conductor._preset, "crossover_regions", ()) or ()
    ).get(role, ())

def _candidate_sections(conductor, fc_hz: float) -> dict:
    from dataclasses import replace

    from jasper.active_speaker.branch_chain import sections_by_role

    return {
        role: tuple(replace(section, fc_hz=float(fc_hz)) for section in sections)
        for role, sections in sections_by_role(
            getattr(conductor._preset, "crossover_regions", ()) or ()
        ).items()
    }

def _plan_spy(mp) -> list:
    plans: list = []
    original = flow.CrossoverV2Session._plan_linearization

    def spy(self, *args, **kwargs):
        plan = original(self, *args, **kwargs)
        plans.append(plan)
        return plan

    mp.setattr(flow.CrossoverV2Session, "_plan_linearization", spy)
    return plans

def _run_phase(conductor, index, attempt) -> dict:
    conductor.authorize_begin(index, attempt)
    return conductor.consume_capture(index, attempt, _capture())

def _snr_pilot(role: str, snr_db: float) -> PilotObservation:
    return PilotObservation(
        role=role, level_lo_dbfs=-40.0, level_hi_dbfs=-30.0,
        programmed_delta_db=10.0, captured_delta_db=10.0,
        linearity_ok=True, channel_map_ok=True,
        snr_valid=math.isfinite(snr_db) or snr_db > 0, snr_db=snr_db,
    )

def _snr_analysis(*pilots: PilotObservation) -> ProgramAnalysis:
    return ProgramAnalysis(
        phase="measure", program_id="p", locations=(), pilots=pilots,
    )

def _spliced_verify(program, **kwargs):
    """A VERIFY analysis whose summed sweep landed a splice off its slot."""
    off_slot = SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * program.sample_rate_hz * 3
    return _verify_analysis(program, residual_samples=off_slot, **kwargs)

def _verify_to_apply(fakes):
    return _stage2_after_measure(fakes)

def _rearm_conductor(fakes, **kwargs):
    """A verify-only re-arm's conductor — the verify-only prepare's shape."""
    return CrossoverV2Session(
        session_id="verify_rearm_session",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        gain_plan_db={"woofer": -11.0, "tweeter": -13.0},
        index_phase_map={1: PHASE_VERIFY},
        measure_gate_window_ms=8.0,
        **kwargs,
    )

CLOUD_MAP = build_v2_cloud_index_phase_map()

STAGE2_SHAPE = resolve_plan_shape()

STAGE2_MAP = dict(enumerate([PHASE_VERIFY] + [PHASE_CLOUD_VERIFY] * (STAGE2_SHAPE.verify_capture_target - 1), 1))

VERIFY_INDEX = next(i for i, p in STAGE2_MAP.items() if p == PHASE_VERIFY)

CLOUD_VERIFY_INDEXES = tuple(
    i for i, p in sorted(STAGE2_MAP.items()) if p == PHASE_CLOUD_VERIFY
)

SHORT_VERIFY_MAP = {1: PHASE_VERIFY, 2: PHASE_CLOUD_VERIFY}

SHORT_VERIFY_CLOUD_INDEXES = tuple(
    i for i, p in sorted(SHORT_VERIFY_MAP.items()) if p == PHASE_CLOUD_VERIFY
)

def _walk(conductor, indexes, start_attempt: int) -> int:
    attempt = start_attempt
    for index in indexes:
        _run_phase(conductor, index, attempt)
        attempt += 1
    return attempt

_COMB_N_FFT = 8192

_COMB_RATE = 48_000

def _comb_summed_response(seed: int, *, r: float = 0.37, delay_samples: int = 15):
    freqs = np.fft.rfftfreq(_COMB_N_FFT, 1.0 / _COMB_RATE)
    rng = np.random.default_rng(seed)
    tf = 1.0 + r * np.exp(-2j * np.pi * freqs * (delay_samples / _COMB_RATE))
    tf = tf + rng.normal(0.0, 1e-6, tf.shape)
    tf = tf * 10.0 ** (
        np.interp(freqs, _SUMMED_FREQS_HZ, _in_room_summed_db()) / 20.0
    )
    return DriverResponse(
        role="summed", freqs_hz=freqs,
        magnitude_db=20.0 * np.log10(np.maximum(np.abs(tf), 1e-12)),
        complex_tf=tf.astype(complex),
        gating={"applied": True, "window_ms": 8.0},
        snr=None, validity_floor_hz=140.0,
    )

def _comb_cloud_analysis_factory():
    counter = {"n": 0}

    def factory(program) -> ProgramAnalysis:
        counter["n"] += 1
        return ProgramAnalysis(
            phase="verify",
            program_id=program.program_id,
            locations=(_loc("sweep_verify", "summed_sweep", confidence=0.9),),
            summed_response=_comb_summed_response(4000 + counter["n"]),
            summed_ripple_db=1.1,
            verify_tracking={
                "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            },
            linearity_ok=True,
        )

    return factory

def _count_builds(c) -> list:
    builds: list = []
    real_build = c._build_candidate

    def _counting_build(analysis, cloud):
        builds.append(1)
        return real_build(analysis, cloud)

    c._build_candidate = _counting_build
    return builds

def _lock(monkeypatch, *, thin: bool = False):
    import jasper.active_speaker.crossover_v2_flow as flow

    monkeypatch.setattr(
        flow, "_geometry_verdict_from_combined",
        lambda combined, n_positions: {
            "locked": True, "reason": "geometry_locked", "thin_evidence": thin,
            "n_positions": n_positions, "median_tau_us": 320.0,
        },
    )

def _dummy_program():
    from jasper.audio_measurement.program import build_check_program

    return build_check_program(_roles(), ambient_s=0.5, pilot_duration_s=0.3)

_GOLDEN_V2_PLAN_BYTES = {
    "stage2-full": (
        1925,
        "485a0ab680e52625c21fda3da47a3dea0cc34e85d6c5d0a68621db08fefebdbf",
    ),
    "stage2-express": (
        630,
        "a5f499d6c1219460a377ee4cd083a45fc86aa93dff3d446bc2c1c4c58955f07b",
    ),
    "1-entry": (
        329,
        "5289e8602bfe37469abd91cc12dff53387b512c358a616dd7e2df20d79b0fccb",
    ),
}

def _profiled_conductor(*, woofer_peak: float, tweeter_peak: float):
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )

    from tests.test_active_speaker_program_admission import _profile_and_targets

    topology, profile, targets = _profile_and_targets(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )
    sv = session_measurement_volume_db(profile, targets.values())
    caps = {"woofer": float(woofer_peak), "tweeter": float(tweeter_peak)}
    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10000.0)),
    ]
    c = CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=roles,
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=sv,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )
    return c, topology, profile, targets, sv

_DIAG_LOGGER = "jasper.active_speaker.crossover_v2_flow"

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

def _driver_response_diag(
    role: str, *, window_ms: float = 8.0, floor_hz: float | None = None,
    snr_db: float | None = None, snr_verdict: str | None = None,
    snr_band: str | None = "mid",
    floor_source: str = gating.FLOOR_MEASURED,
) -> DriverResponse:
    freqs = np.linspace(100.0, 20000.0, 64)
    snr = (
        {
            "worst_relevant": {
                "band_id": snr_band,
                "estimated_snr_db": snr_db,
                "verdict": snr_verdict,
            }
        }
        if snr_db is not None else None
    )
    return DriverResponse(
        role=role, freqs_hz=freqs, magnitude_db=np.zeros(64),
        complex_tf=np.ones(64, dtype=complex),
        gating={
            "applied": True, "window_ms": window_ms, "floor_source": floor_source,
        },
        snr=snr, validity_floor_hz=floor_hz,
    )

def _gate_block(
    *,
    direct_peak_ms: float = 10.40,
    first_reflection_ms: float = 15.73,
    rms_db: float | None = 2.59,
    floor_source: str = gating.FLOOR_MEASURED,
) -> dict:
    delta = None if rms_db is None else {
        "rms_db": rms_db, "max_db": 6.1, "eval_band_hz": [357.0, 20000.0],
    }
    return {
        "applied": True,
        "window_ms": 5.33,
        "floor_source": floor_source,
        "direct_peak_ms": direct_peak_ms,
        "first_reflection_ms": first_reflection_ms,
        "pre_post_gate_delta": delta,
    }

def _check_analysis_with_solves(program, *, snr_floor_ok=True, pilot_snr_ok=True):
    """A CHECK analysis whose gain plan carries #1825 per-role solves."""
    return ProgramAnalysis(
        phase="check", program_id=program.program_id,
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
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

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

def _fixture_raw_predicted_sum(
    *, woofer_db=None, tweeter_db=None, trim_db=None,
) -> tuple[np.ndarray, np.ndarray]:
    if woofer_db is None or tweeter_db is None:
        default_woofer_db, default_tweeter_db = _fixture_branch_db()
        woofer_db = default_woofer_db if woofer_db is None else woofer_db
        tweeter_db = default_tweeter_db if tweeter_db is None else tweeter_db
    if trim_db is None:
        trim_db = _solve_fixture_raw_trim(woofer_db, tweeter_db)
    summed = predicted_branch_sum(
        (10.0 ** (np.asarray(woofer_db) / 20.0)).astype(complex),
        (10.0 ** (np.asarray(tweeter_db) / 20.0)).astype(complex),
        float(trim_db.get("woofer", 0.0)), float(trim_db.get("tweeter", 0.0)), 1,
    )
    return (
        _LINEARIZABLE_FREQS_HZ,
        20.0 * np.log10(np.maximum(np.abs(summed), 1e-12)),
    )

def _eligible_measure_analysis(
    program, *, mic_tier="reference", woofer_repeats=2, tweeter_repeats=2,
    woofer_db=None, tweeter_db=None, trim_db=None, trim_band_average_db=None,
) -> ProgramAnalysis:
    default_woofer_db, default_tweeter_db = _fixture_branch_db()
    if woofer_db is None:
        woofer_db = default_woofer_db
    if tweeter_db is None:
        tweeter_db = default_tweeter_db
    if trim_db is None:
        trim_db = _solve_fixture_raw_trim(woofer_db, tweeter_db)
    if trim_band_average_db is None:
        trim_band_average_db = dict(trim_db)
    return ProgramAnalysis(
        phase="measure",
        program_id=program.program_id,
        locations=(
            _loc("sweep_w"), _loc("sweep_t"), _loc("sweep_w_rep"), _loc("sweep_t_rep"),
        ),
        drift=DriftEstimate(
            epsilon_ppm=5.0,
            max_residual_samples=0.1, glitch_detected=False,
        ),
        mic_tier=mic_tier,
        driver_responses=(
            _linearizable_response("woofer", woofer_db, n_repeats=woofer_repeats),
            _linearizable_response("tweeter", tweeter_db, n_repeats=tweeter_repeats),
        ),
        alignment=_alignment(),
        candidate=CrossoverCandidate(
            trim_db=trim_db, polarity="normal", delay_us=150.0,
            predicted_ripple_db=0.8, confidence=0.8,
            trim_band_average_db=trim_band_average_db,
        ),
        linearity_ok=True,
        predicted_sum=_fixture_raw_predicted_sum(
            woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
        ),
        glitch_detected=False,
    )

def _way1_measure_analysis(program) -> ProgramAnalysis:
    freqs = _LINEARIZABLE_FREQS_HZ
    magnitude_db = (
        -5.0 * np.exp(-0.5 * ((np.log2(freqs / 400.0) / 0.3) ** 2))
        + 3.0 * np.exp(-0.5 * ((np.log2(freqs / 4000.0) / 0.25) ** 2))
    )
    solo = _linearizable_response("full_range", magnitude_db, n_repeats=2)
    return ProgramAnalysis(
        phase="measure",
        program_id=program.program_id,
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

def _one_sided_conductor(fakes: FakeSeams) -> CrossoverV2Session:
    return CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=[
            RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
            RoleBand("tweeter", 1, FrequencyBand(FC_HZ, 20000.0)),
        ],
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
    )

def _gate_residuals(conductor) -> tuple[float, float]:
    before = spec_report_for_predicted_sum(
        _fixture_raw_predicted_sum()
    )
    after = spec_report_for_predicted_sum(conductor.measure_predicted_sum)
    return (
        spec_convergence_residual(before).rms_db,
        spec_convergence_residual(after).rms_db,
    )

def _tracking_curve(c, error_db):
    freqs = np.asarray(c.measure_commanded_delta[0], dtype=float)
    predicted = np.asarray(c.measure_predicted_sum[1], dtype=float)
    error = error_db(freqs) if callable(error_db) else np.full_like(freqs, error_db)
    return freqs, predicted + error, predicted

def _anchor_entry_baseline(c, error_db=0.0):
    freqs = np.asarray(c.measure_commanded_delta[0], dtype=float)
    commanded = np.asarray(c.measure_commanded_delta[1], dtype=float)
    predicted = np.asarray(c.measure_predicted_sum[1], dtype=float)
    error = error_db(freqs) if callable(error_db) else np.full_like(freqs, error_db)
    measured_pre = (predicted - commanded) + error
    banked = c.measure_entry_baseline
    assert banked is not None, "walk the session past ENTRY_BASELINE first"
    c._measure_entry_baseline = dataclasses.replace(
        banked,
        curve=ResponseCurve(freqs, measured_pre),
        excluded=tuple(False for _ in freqs),
    )
    return c._measure_entry_baseline

def _boost_vocabulary_spy(seen: list[bool]):
    real_fit = iv.fit_driver_linearization

    def _spy(resp, envelope, **kwargs):
        seen.append(kwargs["vocabulary"].allow_boost)
        return real_fit(resp, envelope, **kwargs)

    return _spy

def _vocabularies_seen(seen: list):
    real_fit = iv.fit_driver_linearization

    def _spy(resp, envelope, **kwargs):
        seen.append(kwargs["vocabulary"])
        return real_fit(resp, envelope, **kwargs)

    return _spy

def _emitted_boosts(candidate) -> list[dict]:
    return [
        f
        for fit in candidate.linearization.values()
        for f in fit["filters"]
        if f["gain"] > 0.0
    ]

def _healthy_crossed_over_pair(dip_db: float = 7.0):
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ

    def dip(center_hz: float) -> np.ndarray:
        return -dip_db * np.exp(-0.5 * ((np.log2(freqs / center_hz) / 0.3) ** 2))

    lowpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=False),)
    highpass = (CrossoverSection(fc_hz=_FIXTURE_FC_HZ, order=4, highpass=True),)
    woofer_db = crossover_response_db(freqs, lowpass) + dip(400.0)
    tweeter_db = crossover_response_db(freqs, highpass) + dip(6000.0)
    trim_w, trim_t, _lw, _lt = solve_branch_trims(
        freqs,
        (10.0 ** (woofer_db / 20.0)).astype(complex),
        (10.0 ** (tweeter_db / 20.0)).astype(complex),
        _FIXTURE_FC_HZ,
    )
    return woofer_db, tweeter_db, {
        "woofer": round(float(trim_w), 3), "tweeter": round(float(trim_t), 3),
    }

def _tracking_with_frame(**frame_overrides):
    frame = {
        "offset_db": -0.75,
        "tilt_db_per_octave": -0.79,
        "pivot_hz": 2828.4,
        "n_bins": 400,
        "band_hz": [2000.0, 4000.0],
        "raw": {"rms_db": 0.4, "max_db": 0.9},
        "tilt_removed": {"rms_db": 0.18, "max_db": 0.31},
    }
    frame.update(frame_overrides)
    return {
        "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
        "tracking_band_hz": [2000.0, 4000.0],
        "frame": frame,
    }

def _moving_notch_cloud(notch_hz: list[float]):
    from jasper.audio_measurement.spatial_combine import (
        PositionCapture,
        combine_positions,
    )

    freqs = np.fft.rfftfreq(4096, 1.0 / 48_000)
    log_f = np.log2(np.maximum(freqs, 1.0))
    baseline = 1.5 * np.sin(2.0 * np.pi * log_f / 1.7)
    return combine_positions([
        PositionCapture(
            position_id=f"p{k:02d}", freqs_hz=freqs,
            magnitude_db=baseline
            - 18.0 * np.exp(-0.5 * ((log_f - np.log2(f0)) / 0.06) ** 2),
            sample_rate=48_000, ir=None,
        )
        for k, f0 in enumerate(notch_hz)
    ])

_BLIND_SPAN_RESULT = {"validity_floor_hz": 1200.0, "null_registry": {
    "classification": "insufficient_evidence", "reason": "no_corroborating_arrivals",
}}

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

_PROGRAM_PHASES = ("cloud_verify", "verify")
_DECLARED_STIMULUS_PHASE = "verify"

def bank_capture_round(
    root: Path,
    irs: Sequence[np.ndarray],
    *,
    program: np.ndarray | None = None,
    phase: str = "cloud_verify",
    capture_ids: Sequence[str] | None = None,
    positions_deg: Sequence[float] | None = None,
    vertical_deg: float = 0.0,
    distance_m: float | None = 1.0,
    radiated_band_hz: tuple[float, float] | None = (150.0, 20000.0),
    declared_sha: str | None = None,
) -> Path:
    """Retained captures made from known convolutions."""
    bundle = root / "bundle" / "b0"
    programs = bundle / "crossover_v2" / "wired-test"
    summed = bundle / "summed"
    programs.mkdir(parents=True)
    summed.mkdir(parents=True)

    played = (
        synchronized_swept_sine(duration_approx_s=1.0, sample_rate=CAPTURE_RATE)[0]
        if program is None
        else np.asarray(program, dtype=np.float64)
    )
    decoy, _ = synchronized_swept_sine(
        f1=30.0, duration_approx_s=1.0, sample_rate=CAPTURE_RATE
    )
    played_path = programs / f"{phase}_program.wav"
    write_sweep_wav(played_path, played, CAPTURE_RATE)
    write_sweep_wav(
        programs / f"{next(p for p in _PROGRAM_PHASES if p != phase)}_program.wav",
        decoy,
        CAPTURE_RATE,
    )
    played_sha = hashlib.sha256(played_path.read_bytes()).hexdigest()

    for index, ir in enumerate(irs):
        capture = np.convolve(
            played.astype(np.float64), np.asarray(ir, dtype=np.float64)
        )
        capture = 0.5 * capture / float(np.max(np.abs(capture)))
        capture_id = (
            f"{phase}_{index:02d}" if capture_ids is None else capture_ids[index]
        )
        stem = f"summed_{capture_id}"
        write_sweep_wav(
            summed / f"{stem}.wav", capture.astype(np.float32), CAPTURE_RATE
        )
        doc: dict[str, Any] = {
            "position_id": capture_id,
            "phase": phase,
            "wav_path": f"summed/{stem}.wav",
            "position_deg": (
                CAPTURE_AZIMUTHS_DEG[index % len(CAPTURE_AZIMUTHS_DEG)]
                if positions_deg is None
                else float(positions_deg[index])
            ),
            "vertical_deg": vertical_deg,
            "mark_distance_m": distance_m,
            "provenance": {
                "stimulus": {
                    "phase": _DECLARED_STIMULUS_PHASE,
                    "wav_sha256": declared_sha or played_sha,
                }
            },
        }
        if radiated_band_hz is not None:
            doc["curves"] = [{"role": "summed", "band_hz": list(radiated_band_hz)}]
        (summed / f"{stem}.json").write_text(json.dumps(doc))
    (bundle / "info.json").write_text(json.dumps({"session_id": "b0"}))
    write_manifest(root)
    return root

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

    async def get_loudness_volume_db(self, *, best_effort: bool = False) -> float:
        return getattr(self, "loudness_db", self.volume_db)

    async def set_loudness_volume_db(
        self, db: float, *, best_effort: bool = False, immediate: bool = False,
    ) -> bool:
        self.loudness_db = db
        return True

    async def get_volume_db(self, *, best_effort: bool = False) -> float:
        return self.volume_db

    async def set_volume_db(self, db: float, *, best_effort: bool = False) -> bool:
        self.volume_db = float(db)
        return True

def _flow_seams(conductor: Any) -> Any:
    return conductor._seams

def _install_commanded_delta(conductor: Any, commanded: Any) -> None:
    conductor._measure_commanded_delta = commanded

def _delta_probe_given_a_tracking_curve(conductor: Any, tracked: Any) -> Any:
    conductor._verify_tracking_curve = tracked
    conductor._verify_trusted_band_hz = (
        float(min(tracked[0])), float(max(tracked[0])),
    )
    return conductor._run_delta_probe()

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

    from tests.test_plan_run import fake_program_baselines

    fake_program_baselines(monkeypatch)
    monkeypatch.setattr(preflight_live, "read_preflight_facts", lambda plan, **kw: ready_facts(plan))
    monkeypatch.setattr(v2host, "secrets", SimpleNamespace(
        token_hex=lambda _: "minted_by_this_stage", token_urlsafe=v2host.secrets.token_urlsafe))
    monkeypatch.setattr(v2host, "_resolve_prepare_wired_mic", fake_measurement_mic)
    from jasper.active_speaker import model_error_store
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
    monkeypatch.setattr(
        session_volume_plan_mod, "session_measurement_volume_db",
        lambda safety_profile, fps, **kw: -20.0,
    )
    monkeypatch.delenv(ACTIVE_PLAYBACK_DEVICE_ENV, raising=False)
    monkeypatch.setenv(
        model_error_store.STATE_PATH_ENV, str(tmp_path / "model_errors.json")
    )
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
    from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, REGIME_PER_DRIVER
    return {"plan": AngleCaptureRequest(stops=(AngleStop(0, REGIME_PER_DRIVER),)).to_dict()}

def _stage_1(monkeypatch) -> tuple[Any, dict[str, Any]]:
    prepared = v2host.prepare_v2_session(
        _inline_body(), status=_status(), run_async=asyncio.run, camilla_factory=None
    )
    return _open_prepared(monkeypatch, prepared)

_PILOT_AT = 1_760_000_000.0

_GATE_WINDOW_MS = 6.5

_PREDICTED_SPEC = {"overall_within_target": True, "bands": [{"f_lo_hz": 1000.0, "within_target": True}]}

_COMMANDED_FREQS_HZ = [
    500.0, 630.0, 800.0, 1000.0, 1250.0, 1600.0, 2000.0,
    2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0,
]

_COMMANDED_DELTA_DB = [
    0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.0, 2.2, 2.4, 2.5, 2.5, 2.5, 2.5,
]

_ENTRY_BASELINE_PROGRAM_ID = "prog-entry-baseline-stage-1"

_ENTRY_BASELINE_GRAPH = "fp-entry-graph"

_ENTRY_BASELINE_CAPTURED_AT = "2026-08-10T12:34:56Z"

_ENTRY_BASELINE_FREQS_HZ = [200.0, 400.0, 800.0, 1600.0, 3200.0]

_ENTRY_BASELINE_DB = [-2.5, -1.25, 0.0, 1.25, 2.5]

_ENTRY_BASELINE_EXCLUDED = [True, False, False, False, False]

def _entry_baseline_record() -> dict[str, Any]:
    from jasper.active_speaker.crossover_v2.round_evidence import (
        ENTRY_BASELINE_KIND,
    )

    return {
        "kind": ENTRY_BASELINE_KIND,
        "program_id": _ENTRY_BASELINE_PROGRAM_ID,
        "reference_mark": contracts.REFERENCE_MARK_DESIGN_AXIS,
        "freqs_hz": list(_ENTRY_BASELINE_FREQS_HZ),
        "magnitude_db": list(_ENTRY_BASELINE_DB),
        "excluded": list(_ENTRY_BASELINE_EXCLUDED),
        "graph_fingerprint": _ENTRY_BASELINE_GRAPH,
        "captured_at": _ENTRY_BASELINE_CAPTURED_AT,
        "artifact_ref": "entry_baseline_09_a01",
    }

def _seed_applied_stage_1_state() -> dict[str, Any]:
    state = {
        "session_id": "cap_stage1_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
        "candidate": {"fingerprint": "fp-stage-1"},
        "gain_plan_db": {"woofer": -3.0, "tweeter": -6.0},
        "verify_priors": {
            "predicted_sum": {
                "freqs_hz": [500.0, 1000.0, 2000.0, 4000.0],
                "magnitude_db": [-1.0, -0.5, 0.5, 1.0],
            },
            "predicted_spec": dict(_PREDICTED_SPEC),
            "commanded_delta": {
                "freqs_hz": list(_COMMANDED_FREQS_HZ),
                "delta_db": list(_COMMANDED_DELTA_DB),
            },
            "entry_baseline": _entry_baseline_record(),
            "gate_window_ms": _GATE_WINDOW_MS,
            "pilot_transfer_reference": {
                "values": {"woofer": -41.5, "tweeter": -39.25}, "at": _PILOT_AT,
            },
        },
    }
    v2state.save_v2_state(state)
    return state

class _AcceptingStore:

    session_id = "bundle-test"

    def __init__(self, bundle_dir: Any) -> None:
        self.bundle_dir = str(bundle_dir)

    def publish_json_artifact(self, relpath: str, payload: Any) -> Any:
        return SimpleNamespace(fingerprint=f"fp-{relpath}")

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
        return SimpleNamespace(fingerprint="fp-check-pin")

    def identify_artifact(self, relpath: str) -> Any:
        return SimpleNamespace(fingerprint="fp-check-pin")

def _regradable_fixture() -> tuple[Any, Any, Any]:
    import numpy as np

    freqs = np.logspace(math.log10(100.0), math.log10(20_000.0), 2048)
    commanded = np.full_like(freqs, 6.0)
    error = np.where((freqs >= 2_000.0) & (freqs <= 3_000.0), 6.0, 0.0)
    return freqs, commanded, error

_PERSISTED_TOP_LEVEL_KEYS = {
    "accepted_phases",
    "accepted_sound_revision",
    "accepted_sound_declaration_change",
    "applied",
    "attempts_loop",
    "candidate",
    "cloud",
    "evidence",
    "expected_post_apply_offset_db",
    "failure",
    "gain_plan_db",
    "kind",
    "measure",
    "measure_gain_ceiling_db",
    "measure_sweep_durations_s",
    "previous_candidate_fingerprint",
    "previous_candidate_displaced_by",
    "round_ordinal_epoch",
    "round_receipt",
    "schema_version",
    "session_id",
    "session_phases",
    "sound_design_revision",
    "updated_at",
    "verify",
    "verify_priors",
}

def _session_from_real_open(monkeypatch, fakes) -> Any:
    from jasper.active_speaker.crossover_v2.door import OpenMeasurementDoor
    from jasper.audio_measurement.wired_capture import WiredSplMonitor

    captured = {}
    real_bind = v2host.bind_run_door
    monkeypatch.setattr(v2host, "bind_v2_engine_seams", lambda **kwargs: fakes.seams())
    def bind(**kwargs):
        binding, analyze, assessor, execute = real_bind(**kwargs)
        level = kwargs["conductor"]._excitation.session_volume_db
        monitor = WiredSplMonitor(binding.sensitivity, binding.ceiling_db_spl, 0)
        door = OpenMeasurementDoor(fakes.graph, fakes.volume, None, level, level, "graph", monitor)
        captured["tuning"] = binding.build_session(door, kwargs["manifest"].allocate_take_id)
        return binding, analyze, assessor, execute
    monkeypatch.setattr(v2host, "bind_run_door", bind)
    captured["conductor"], _state = _stage_1(monkeypatch)
    return captured


def _inline_spec():
    request = ac.per_driver_at([0])
    captures = prepare_plan_captures(request, roles_bands=_roles())
    return build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        roles_bands=_roles(), fc_hz=FC_HZ,
        acknowledgement_binding="b" * 24, retries_per_pose=0,
    )
