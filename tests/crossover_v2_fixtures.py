# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixture library for the crossover-v2 test surface.

Lifted verbatim out of ``tests/test_crossover_v2_conductor.py`` (#2291 phase
5c-i). Every definition below is byte-identical to the one it replaced: this
extraction moved code, it did not change any of it.

Why it exists: the conductor test file had become the campaign's de-facto
shared fixture library — 18 other test modules imported 25 symbols from it,
including all three Phase-0 characterization pins (capture sequence, phase
admission, evidence retention). That made a 12,680-line file full of
conductor-specific tests undeletable, because deleting it would have taken
the pinned surfaces with it. The fixtures now have a home that outlives the
conductor test file; importers name this module instead.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

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
from jasper.active_speaker.attempts_loop import FloorStats
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2_flow import (
    GEOMETRY_RETRY_POSITIONS,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    CrossoverV2Session,
    V2FlowSeams,
    build_v2_cloud_index_phase_map,
    build_v2_verify_index_phase_map,
    resolve_plan_shape,
    spec_report_for_predicted_sum,
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


CAPS = {"woofer": 0.0, "tweeter": -65.0}


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


# --- the 1-way (subless passive main) shape ------------------------------------
#
# One amp channel, one declaration, no crossover region and no local sub: its
# MEASURE is ONE routed solo, so no upper role and no corner exist anywhere.

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


# --- fake analyses -------------------------------------------------------------


def _loc(segment_id: str, kind: str = "sweep", *, confidence: float = 0.9,
         clipped: bool = False, residual_samples: float = 0.0) -> SegmentLocation:
    return SegmentLocation(
        segment_id=segment_id, kind=kind, role=None,
        scheduled_start=0, located_start=0, residual_samples=residual_samples,
        confidence=confidence, peak_dbfs=-12.0, clipped=clipped,
    )


_SUMMED_FREQS_HZ = np.linspace(100.0, 20000.0, 64)


def _in_room_summed_db() -> np.ndarray:
    """A modest, physically plausible in-room summed magnitude.

    **Why this is not ``np.zeros``** (PR-L4). The cloud positions play this
    curve, so it becomes the group's spatially-combined "how flat is the
    speaker" measurement — and a perfectly flat 8-position in-room spatial
    power mean does not exist. A zero curve made the measured pre-apply spec
    residual exactly 0.00 dB, which is not a demanding fixture but an
    impossible one, and PR-L4 item 2's gate (predicted post-apply vs measured
    pre-apply) reads exactly that number. Any assertion about "did the
    correction improve on what we measured" is meaningless against a
    measurement that claims perfection.

    A broadband tilt plus one wide dip, landing at ~2.7 dB of pooled spec
    residual — the regime the 2026-07-27 JTS3 session actually measured
    (3.15-3.76 dB pooled across its ten positions). Note what the PRE-APPLY
    cloud is measuring: an UNCORRECTED speaker in a room, which is supposed to
    look like this. Deliberately smooth — the honesty screen and the null gate
    are exercised by their own fixtures, and a combed curve here would couple
    these wiring tests to those detectors.
    """
    octaves = np.log2(_SUMMED_FREQS_HZ / 1000.0)
    return -2.0 * octaves - 3.0 * np.exp(-0.5 * (octaves / 0.8) ** 2)


# The measured pre-apply pooled spec residual each room scale in
# ``test_prediction_gate_verdict_does_not_depend_on_the_room`` produces. Quoted
# so that test can prove the room ACTUALLY moved between its cases — a
# room-independence claim is worthless if the fixture room never varied. Under
# the pre-B1 gate these three scales spanned refuse / pass / pass; they must now
# all reach the same verdict.
_ROOM_SCALE_EXPECTED_RMS_DB = {0.4: 1.626, 1.0: 4.331, 2.5: 12.787}


#: The evaluation band a production gating block carries for these fixtures —
#: the whole fixture grid, so a probe reading it grades exactly the bins it
#: graded before the trusted band became its input (#2521). Tests that care
#: about the clamp pass a narrower one.
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
        # ``floor_source`` is WHY the window is that long (issue #1966) —
        # optional here because most fixtures only care that a window exists,
        # and a block without it reads as "unknown" exactly like a schema-1
        # record does.
        #
        # ``pre_post_gate_delta.eval_band_hz`` is the band the capture's own
        # gate says it can be judged over — written by
        # ``gate_disclosure.pre_post_gate_delta`` in production and read back by
        # ``build_gate_disclosure``. The delta probe's band comes from here
        # since #2521, so a fixture without it is a capture with no trusted
        # band, which is a real state (an ungateable capture) and leaves the
        # probe unavailable. Pass ``trusted_band_hz=None`` to build one.
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
    """A finer-grained DriverResponse (2048 bins, vs _driver_response's
    coarse 64) carrying real repeat_responses — Layer-1a linearization
    (#1668 PR-C) needs enough frequency resolution for a synthetic bump to
    survive resampling onto DEFAULT_ENVELOPE_GRID_HZ and enough occurrences
    to clear the paired-N gate."""

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
    """The fixture alignment. ``anchor_delay_us`` defaults to ``None`` — the
    shape every pre-R10b test was written against, in which the summed model's
    residual delay is 0.0 and the prediction is byte-identical to the
    independently-aligned sum. Pass it to exercise the committed-delay model.
    """
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
    """A VERIFY leading-pilot observation — role ``summed`` (VERIFY_PILOT_ROLE),
    the only role a v2 VERIFY program ever carries."""
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
        # Derived by PRODUCTION code from this fixture's own locations (#1971),
        # not hand-built: a hand-built record would let the conductor's gate be
        # tested against a shape the analyzer never produces. Pass
        # ``integrity=None`` for the pre-#1971 analysis shape.
        integrity = _verify_capture_integrity(
            program, program.sample_rate_hz, locations,
            # This fixture builds an analysis from locations alone — there is no
            # capture and therefore no capture-page report to reconcile, so the
            # frame-accounting checks (#2094) are not-evaluated here, exactly as
            # they are for any capture whose page declared no counts.
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
        # W6.7 ruling 1: the conductor gates on the notch-excluded max, not the
        # raw ``max_db`` — this fake keeps them equal (a fake with no notch to
        # exclude), so the ``max_db`` parameter still controls the gate.
        verify_tracking={
            "rms_db": 0.4,
            "max_db": max_db,
            "max_db_notch_excluded": max_db,
            "frame": {"n_bins": n_graded_bins},
        },
        # R18 (#1868): ``None`` is the honest default for a fixture that
        # supplies no crossover-region evidence — the kernel records that as
        # not-evaluated, which never gates. Tests that exercise the claim pass
        # a record explicitly.
        verify_absolute=verify_absolute,
        linearity_ok=linearity,
        pilot_snr_ok=pilot_snr_ok,
        pilots=(
            (_verify_pilot(pilot_hi_dbfs, programmed_hi_gain_db=programmed_hi_gain_db),)
            if pilot_hi_dbfs is not None else ()
        ),
    )


# --- fake seams -----------------------------------------------------------------


def bank_into(
    sink: list[Any], *, with_capture: bool = False, phase: str | None = None,
) -> flow.BankTake:
    """A ``bank_take`` seam that records what it banked and answers an id.

    ``phase`` narrows what reaches ``sink`` to one phase's takes. Every
    accepted capture banks one — CHECK and MEASURE included — so a test about
    the cloud group's records would otherwise read the walk's warm-up takes as
    group members. The seam still answers an id for the takes it filters out:
    they were banked, they are simply not this test's subject.

    The production binding answers the store id that finds the record again,
    and ``""`` only when nothing was stored — so a recorder that answered
    ``""`` would make every test of the one caller that READS the answer
    (``CrossoverV2Session._retain_entry_baseline``) pass for the wrong reason.
    The id is minted the way the store mints it, off the record's OWN take id,
    which is the invariant the seam exists to keep.

    ``with_capture`` keeps the :class:`WiredCaptureAnswer` beside the record,
    for the two tests that assert the raw bytes cross the seam.
    """
    def bank_take(result: Any, record: Mapping[str, Any]) -> str:
        banked = dict(record)
        if phase is None or banked.get("phase") == phase:
            sink.append((result, banked) if with_capture else banked)
        return f"crossover_v2/fixture/positions/{banked.get('take_id') or ''}.json"

    return bank_take


@dataclass
class FakeSeams:
    """Recorder seams; per-phase analysis factories are swappable mid-test."""

    check: Any = _check_analysis
    measure: Any = _measure_analysis
    verify: Any = _verify_analysis
    played: list = field(default_factory=list)
    analyzed: list = field(default_factory=list)
    published_checks: list = field(default_factory=list)
    published_candidates: list = field(default_factory=list)
    apply_done: bool = False
    # Simulates the host's auto-apply background thread hitting a TERMINAL
    # failure (owner ruling, 2026-07-20) — empty string while pending/never
    # attempted, a REASON_REGISTRY code once the auto-apply gives up.
    apply_failed_code: str = ""
    # PR-L5: the delta probe's automatic-rollback seam. ``None`` (the default)
    # is the honest "no binding" case the conductor must still refuse under.
    rollback: Any = None
    # #2291's state half of "can this host restore" — a prior candidate is
    # recorded — beside ``rollback``'s process fact. Production binds it
    # UNCONDITIONALLY on both stages (``bind_v2_stage_seams``), so a fixture
    # that binds ``rollback`` and leaves this unbound is not modelling any
    # real speaker: the round would route to ``recovery_required`` on a host
    # whose way back works.
    #
    # It matters here since the fifth-principle routing. A delta-probe rollback
    # class used to restore from the probe's own seam, which asked only whether
    # ``rollback`` was bound; it now restores through the adoption table, which
    # correctly asks both halves. Defaulted to follow ``rollback`` so every
    # existing fixture keeps modelling the host it meant to.
    rollback_available: Any = None
    # #1866: every level-frame finding the conductor banks, in order. Bound by
    # default (unlike ``rollback``) because "no findings seam" is the degraded
    # case here, not the normal one — a test that wants it unbound passes
    # ``publish_findings=None`` through ``dataclasses.replace``.
    banked_findings: list = field(default_factory=list)
    # #2291/#2318: does the APPLIED graph boost? Bound by default and FALSE,
    # because these fixtures grade rounds whose subject is something else and
    # the seam's unbound answer is deliberately "boosted" — an intervention
    # nobody can inspect comes off. Leaving it unbound here would route every
    # indeterminate round through the fail-closed restore and make each of
    # these tests assert that rule instead of its own. A test that wants the
    # rule ITSELF sets this True (or passes ``applied_boosts=None`` through
    # ``dataclasses.replace`` for the unbound case).
    applied_boosts: bool = False
    # #2611: the profile the speaker is playing when a candidate is committed —
    # the PREVIOUS side of the commanded axis. Bound by default (unlike
    # ``rollback``) because "no applied profile" is the degraded case here, not
    # the normal one: without it the commanded delta is ``None`` and every probe
    # fixture would assert ``unavailable`` instead of its own subject. The
    # default names the graph whose predicted sum is EXACTLY
    # ``_eligible_measure_analysis``'s own ``predicted_sum`` — the fixture's raw
    # branch pair at the fixture's raw trim, no correction, no delay, no
    # inversion — so a probe fixture's commanded delta is byte-identical to the
    # pre-#2611 one and every test written against it still measures what it
    # says it measures. A test that wants the ABSENT case passes
    # ``applied_profile=None`` through ``dataclasses.replace``.
    applied_profile_state: Any = None

    def applied_profile(self) -> dict[str, Any]:
        return (
            self.applied_profile_state
            if self.applied_profile_state is not None
            else _fixture_applied_profile()
        )

    def seams(self) -> V2FlowSeams:
        def analyze(program, result, priors, geometry, *, phase=None):
            # ``phase`` is the conductor's OWN flow phase (issue #1855) —
            # recorded separately from ``program.phase`` since the two
            # diverge for cloud positions (every cloud position plays the
            # verify-shaped summed sweep, so ``program.phase`` is always
            # "verify" there; see test_cloud_positions_play_the_summed_
            # program_and_get_no_tracking_prior).
            self.analyzed.append((phase, program.phase, result, priors, geometry))
            factory = {
                "check": self.check, "measure": self.measure, "verify": self.verify,
            }[program.phase]
            return factory(program)

        return V2FlowSeams(
            play=lambda phase, program: self.played.append((phase, program)),
            analyze=analyze,
            publish_check=lambda plan, ambient: self.published_checks.append(plan),
            publish_candidate=self.published_candidates.append,
            apply_complete=lambda: self.apply_done,
            apply_failed=lambda: self.apply_failed_code,
            rollback=self.rollback,
            # Follows ``rollback`` unless a test says otherwise — see the
            # field's own note for why an unbound anchor beside a bound
            # rollback models no real host.
            rollback_available=(
                self.rollback_available
                if self.rollback_available is not None
                else (None if self.rollback is None else (lambda: True))
            ),
            applied_boosts=lambda: self.applied_boosts,
            applied_profile=self.applied_profile,
            publish_findings=self.banked_findings.append,
        )


def _fixture_applied_profile(
    trim_db: dict[str, float] | None = None,
    *,
    linearization: dict[str, Any] | None = None,
    delay_ms: dict[str, float] | None = None,
    inverted: dict[str, bool] | None = None,
    fc_hz: float = FC_HZ,
) -> dict[str, Any]:
    """An applied Layer-A profile in the shape the commanded axis reads (#2611).

    Only the fields the commanded axis consults —
    ``recomposition_snapshot['corrections']`` and ``['linearization']`` for
    :func:`~jasper.active_speaker.crossover_v2.commanded.profile_graph_summation`,
    and ``['preset']`` for
    :func:`~jasper.active_speaker.crossover_v2.commanded.profile_crossover_fc_hz`
    — because a fixture that fabricated a whole profile would be asserting the
    profile schema rather than the axis. The defaults describe the graph
    :func:`_eligible_measure_analysis` was measured through in these fixtures:
    the raw trim, no correction, no delay, no inversion, at the session's own
    corner.

    ``fc_hz`` is the corner this profile's graph was BUILT at (#2614). It
    defaults to the session's, which is the ordinary case; passing a different
    one is how a test reaches the corner-mismatch refusal, and it is a
    parameter because that refusal is a hearing-safety-adjacent door rather
    than an unreachable branch.
    """
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


#: The entry baseline's pooled spec residual, in dB, and the post-apply
#: fixture's. Quoted from the shipped reducer rather than asserted by eye, for
#: the reason ``_ROOM_SCALE_EXPECTED_RMS_DB`` above is quoted: a "the before was
#: worse" fixture is worthless if nobody checked which way worse runs. Higher is
#: worse (measured through ``spec_convergence_residual``), so 6.88 → 4.33 is a
#: 2.55 dB win — over five times #2291's 0.5 dB claim margin. The absolute
#: figures moved with the low-mid reference frame (ADR-0194); the WIN grew,
#: because the frame no longer averages the defect the round removed into the
#: zero that defect is measured against.
_ENTRY_BASELINE_SCALE = 1.5


_ENTRY_BASELINE_RESIDUAL_DB = 6.877


_POST_APPLY_RESIDUAL_DB = 4.331


def _fixture_entry_baseline(conductor: CrossoverV2Session) -> EntryBaseline:
    """The pre-apply capture #2291 Phase 3c grades every round against.

    **Why a conductor fixture carries one at all.** Since Phase 3c, stage 1
    always captures an entry baseline immediately before the household applies,
    so a stage-2 conductor without one is no longer a state production can
    reach. A fixture that omitted it graded every round
    ``entry_baseline_unavailable`` — an INDETERMINATE benefit — and the adoption
    table fails that closed for a boosted candidate, so the round replaced the
    verdict under test with a refusal about the missing baseline rather than
    about anything the test was asking.

    Built the way ``_retain_entry_baseline`` builds it: this session's OWN
    ``_verify_program`` reduced through the shipped reducer, so ``program_id``,
    grid, and mask agree with the post-apply side by construction instead of by
    resemblance — a lookalike program would grade ``incomparable_program`` and a
    lookalike mark ``incomparable_reference_mark``. The curve is the fixture's
    in-room sum at ``_ENTRY_BASELINE_SCALE`` times the deviation: a speaker that
    measurably improved, which is what makes the round grade a real benefit.

    The program only exists once the conductor is constructed (``__init__``
    composes it), so this cannot ride the ``measure_entry_baseline`` constructor
    argument without duplicating ``SessionExcitation.verify_program`` — and a duplicate
    is exactly the lookalike the comparability check exists to catch. It writes
    the attribute production writes instead.
    """
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
        session_id=SESSION,
        source_preset=source_preset,
        roles_bands=_roles() if roles_bands is None else roles_bands,
        fc_hz=fc_hz,
        driver_caps_dbfs=CAPS if driver_caps_dbfs is None else driver_caps_dbfs,
        session_volume_db=SESSION_VOLUME_DB,
        seams=seams,
        driver_spacing_m=driver_spacing_m,
        **kwargs,
    )
    # A session that walks ``PHASE_ENTRY_BASELINE`` is stage ONE: it captures
    # its own "before", so starting it with one pre-banked would hide the very
    # thing those tests measure (``tests/test_crossover_v2_entry_baseline.py``
    # imports this helper). Every other session is a stage 2, which production
    # only ever reaches carrying a baseline stage 1 already took.
    if not supplied_baseline and (
        flow.PHASE_ENTRY_BASELINE not in conductor.session_phases
    ):
        conductor._measure_entry_baseline = _fixture_entry_baseline(conductor)
    return conductor


def _way1_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Session:
    """:func:`_conductor`'s 1-way twin. ``fc_hz`` is None rather than a stand-in
    corner and ``driver_spacing_m`` 0.0 rather than omitted: a passive main has
    no corner, and parallax is a woofer-to-tweeter geometry."""
    return _conductor(
        fakes,
        roles_bands=_roles_way1(),
        fc_hz=None,
        driver_caps_dbfs={"full_range": 0.0},
        driver_spacing_m=0.0,
        source_preset=kwargs.pop("source_preset", _one_way_preset()),
        **kwargs,
    )


def _attempt_floor() -> FloorStats:
    return FloorStats.from_repeat_study(
        metric=flow.ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        median_db=0.05,
        p95_db=0.1,
        source="test repeat study",
        measured_at="2026-08-03",
    )


def _verify_only_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Session:
    return _conductor(
        fakes,
        index_phase_map={1: PHASE_VERIFY},
        accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
        attempt_floor=_attempt_floor(),
        **kwargs,
    )


def _capture() -> WiredCaptureAnswer:
    return WiredCaptureAnswer(wav=b"fake-wav")


def _configured_sections(conductor, role: str) -> tuple:
    """The sections this session's own preset gives ``role``.

    The conductor derived this itself until #2291 Phase 2b (as
    ``_branch_crossover_sections``); the planner now builds its candidate
    context from the same shared ``sections_by_role``, so a test comparing
    against "what the session's preset says" asks that function directly.
    """
    from jasper.active_speaker.branch_chain import sections_by_role

    return sections_by_role(
        getattr(conductor._preset, "crossover_regions", ()) or ()
    ).get(role, ())


def _candidate_sections(conductor, fc_hz: float) -> dict:
    """This session's preset sections, re-cornered at ``fc_hz``.

    Same shape and same reason as :func:`_configured_sections` above, and the
    LAST owner of this binding: production carried it while a corner sweep built
    a candidate per corner, and the sweep was deleted by ticket 2.3 of
    ``docs/tuning-master-plan.md``. What still needs an alternative corner's
    sections is the ``candidate_sections=`` argument of the build request — a
    surviving seam whose tests live here — so the two-line binding lives beside
    them rather than as production nothing calls.
    """
    from dataclasses import replace

    from jasper.active_speaker.branch_chain import sections_by_role

    return {
        role: tuple(replace(section, fc_hz=float(fc_hz)) for section in sections)
        for role, sections in sections_by_role(
            getattr(conductor._preset, "crossover_regions", ()) or ()
        ).items()
    }


def _plan_spy(mp) -> list:
    """Capture every ``LinearizationPlan`` a walk produces, in order.

    Since #2291 Phase 2b the planner returns its level frame, its realized-level
    verdict and its linearized prediction as values on one plan, rather than
    stashing them on the conductor as ``_last_*`` fields. A test that used to
    read those fields after the walk observes the plan on its way past instead —
    which is also stricter, since a plan belongs unambiguously to the candidate
    whose build produced it.
    """
    plans: list = []
    original = flow.CrossoverV2Session._plan_linearization

    def spy(self, *args, **kwargs):
        plan = original(self, *args, **kwargs)
        plans.append(plan)
        return plan

    mp.setattr(flow.CrossoverV2Session, "_plan_linearization", spy)
    return plans


def _run_phase(conductor, index, attempt) -> dict:
    # Mirrors the production host's own authorize wrapper
    # (``correction_crossover_v2_wired.build_v2_wired_run_and_consume``):
    # admission, and ONLY admission. It used to call
    # ``confirm_cloud_measure_group(index)``
    # first, because the household's confirmation was inferred from a begin
    # past the cloud group; since the two-stage split (work order D1) the
    # confirmation is its own explicit signal and rides no begin at all.
    conductor.authorize_begin(index, attempt)
    conductor.on_armed()
    return conductor.consume_capture(index, attempt, _capture())


def _confirm_cloud(conductor) -> dict:
    """The confirm seam's own payload — ``{candidate_fingerprint,
    headroom_cost_db}``.

    The explicit close the host calls on the phone's set-completion signal. One
    shot by construction (``self._candidate`` is the guard), so a second call
    returns ``None`` rather than re-fitting.
    """
    return conductor.confirm_cloud_measure_group() or {}


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
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    return c


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


# Stage 1 (measure) and stage 2 (verify) are separate SESSIONS since the
# two-stage split (work order D1/D2), so they are separate maps and separate
# conductors — there is no single index space spanning the whole journey any
# more.
CLOUD_MAP = build_v2_cloud_index_phase_map()


CLOUD_MEASURE_INDEXES = tuple(
    i for i, p in sorted(CLOUD_MAP.items()) if p == PHASE_CLOUD_MEASURE
)


STAGE2_SHAPE = resolve_plan_shape()


STAGE2_MAP = build_v2_verify_index_phase_map(plan_shape=STAGE2_SHAPE)


VERIFY_INDEX = next(i for i, p in STAGE2_MAP.items() if p == PHASE_VERIFY)


CLOUD_VERIFY_INDEXES = tuple(
    i for i, p in sorted(STAGE2_MAP.items()) if p == PHASE_CLOUD_VERIFY
)


# A deliberately SHORT verify group — the anchor plus ONE prompted position.
# Not a shipped plan shape (the tiers ship M=1 or M=6), but the conductor takes
# its index map as a constructor argument, and this is the compact way to reach
# the position floor: give that one position up and the group has no curve left
# and nothing unwalked to recover with. Building it from the same vocabulary the
# production map builder emits, so it cannot drift into a shape the conductor
# would never see.
SHORT_VERIFY_MAP = {1: PHASE_VERIFY, 2: PHASE_CLOUD_VERIFY}


SHORT_VERIFY_CLOUD_INDEXES = tuple(
    i for i, p in sorted(SHORT_VERIFY_MAP.items()) if p == PHASE_CLOUD_VERIFY
)


def _cloud_conductor(fakes: FakeSeams, **kwargs) -> CrossoverV2Session:
    kwargs.setdefault("index_phase_map", CLOUD_MAP)
    # What ``prepare_v2_session`` declares: this measuring session has no
    # VERIFY entry of its own, and the correction it proposes is verified by
    # stage 2 (work order D2). Without it the fit would be refused boost, which
    # is the shape of the regression the declaration exists to prevent.
    kwargs.setdefault("post_apply_verifies", True)
    return _conductor(fakes, **kwargs)


def _walk(conductor, indexes, start_attempt: int) -> int:
    attempt = start_attempt
    for index in indexes:
        _run_phase(conductor, index, attempt)
        attempt += 1
    return attempt


_COMB_N_FFT = 8192


_COMB_RATE = 48_000


def _comb_summed_response(seed: int, *, r: float = 0.37, delay_samples: int = 15):
    """A two-path summed response — the shape a position-invariant
    interference comb has, and the ONE shape the power-vs-median screen
    structurally cannot catch on its own (plan "S0 executed" § e.1).

    Built as a real rfft-grid transfer function because
    ``cloud_position_capture`` derives the IR the echo detector reads by
    inverting exactly this array; the coarse 64-bin ``_driver_response`` above
    cannot carry an invertible one.
    """
    freqs = np.fft.rfftfreq(_COMB_N_FFT, 1.0 / _COMB_RATE)
    rng = np.random.default_rng(seed)
    tf = 1.0 + r * np.exp(-2j * np.pi * freqs * (delay_samples / _COMB_RATE))
    tf = tf + rng.normal(0.0, 1e-6, tf.shape)
    # The comb rides a ROOM, not a flat 0 dB reference (PR-L4) — a real
    # position measures the speaker's own in-room shape with the interference
    # comb on top of it, and the pre-apply cloud's spec residual is read off
    # exactly that product. See `_in_room_summed_db` for why a flat base is not
    # a demanding fixture but an impossible one. Zero-phase magnitude shaping,
    # so the two-path ladder the echo detector reads is untouched.
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
    """A ``verify``-program analysis factory whose every capture carries the
    same comb — one per call, so a group of them is position-INVARIANT."""
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


def _walk_measure_cloud_to_close(c, *, start_attempt: int = 1) -> dict:
    """CHECK → MEASURE → every pre-apply cloud position → the CONFIRM.

    Returns the closing verdict MERGED with the confirm's own payload, which is
    where ``candidate_fingerprint``/``auto_apply`` live since
    flow-simplification §2.6 moved the fit off the final position's acceptance
    and onto the household's confirmation past it.

    A position-invariant cloud legitimately trips PR-3b's geometry-locked
    retake (that is the point of the verdict), so the last index is re-walked
    until it is accepted — bounded by ``GEOMETRY_RETRY_POSITIONS``' own budget
    rather than looping forever.
    """
    attempt = _walk(c, (1, 2), start_attempt)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    for _ in range(GEOMETRY_RETRY_POSITIONS + 1):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        if verdict["accepted"]:
            return {**verdict, **_confirm_cloud(c)}
    raise AssertionError("the cloud-measure group never closed")


def _walk_measure_cloud_to_accept(c, *, start_attempt: int = 1) -> int:
    """CHECK → MEASURE → the whole pre-apply cloud, stopping at the ACCEPT.

    The HELD WINDOW itself — walked, unconfirmed, the phone still offering
    Retake — which is where the eager fit lives and which
    ``_walk_measure_cloud_to_close`` walks straight past. Returns the next
    unused attempt number, so a caller can drive a voluntary retake of the
    final position from exactly where the household would.
    """
    attempt = _walk(c, (1, 2), start_attempt)
    attempt = _walk(c, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    for _ in range(GEOMETRY_RETRY_POSITIONS + 1):
        verdict = _run_phase(c, last, attempt)
        attempt += 1
        if verdict["accepted"]:
            assert verdict["awaiting_confirm"] is True
            return attempt
    raise AssertionError("the cloud-measure group never closed")


def _count_builds(c) -> list:
    """Record every FIT this conductor runs, so a test can tell a commit from
    a re-fit. The eager rider's whole claim is about which of the two the
    household's confirmation pays for."""
    builds: list = []
    real_build = c._build_candidate

    def _counting_build(analysis, cloud):
        builds.append(1)
        return real_build(analysis, cloud)

    c._build_candidate = _counting_build
    return builds


def _lock(monkeypatch, *, thin: bool = False):
    """Force the group-end geometry verdict.

    ``_geometry_verdict_from_combined`` is a pure function of an already-
    combined result; manufacturing a genuinely position-invariant echo across
    a synthetic cloud is ``spatial_combine``'s own test territory (and is
    covered there). What this file owns is the CONDUCTOR's response to a
    verdict, so the verdict is injected.

    Patches ``_geometry_verdict_from_combined`` rather than
    ``cloud_geometry_verdict`` (S3 review finding, 2026-07-26:
    ``_close_cloud_group`` combines each group's positions exactly ONCE and
    derives its retry-gating verdict from that single ``combined`` object via
    ``_geometry_verdict_from_combined`` — it no longer calls
    ``cloud_geometry_verdict`` at all, which stays a positions-only
    convenience wrapper for other callers, e.g. the corpus acceptance test).
    The real (unmocked) ``combine_cloud_positions`` still runs underneath —
    this lambda ignores its ``combined`` argument entirely, so whatever the
    fake seams' synthetic captures actually combine to is irrelevant to the
    injected verdict.
    """
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


# Golden wire bytes for the two shipped v2 capture plans, canonicalized exactly
# the way `PiCaptureSession.capture_spec_json` serializes the enclosing spec
# (`json.dumps(..., separators=(",", ":"))`), so these really are the bytes the
# phone receives — not a proxy for them.
#
# WHAT MUST NEVER CHANGE THEM: raising the capture's transport ceiling
# (`capture_protocol.MAX_CAPTURE_PLAN_ATTEMPTS`). That is the original point
# of this pin — the capacity raise from 8 to 32 had to be invisible to the
# shipped flows, and a value-level assertion alone would not have caught a
# serialization change that came along with it.
#
# WHAT LEGITIMATELY CHANGES THEM: editing a `screen` title/body/auto-advance,
# changing the plan's capture target or attempt budget, altering
# `CapturePlan.to_dict`'s schema, or shifting any composed program's length
# (prelude/pilot durations, `CAPTURE_ENTRY_MARGIN_MS`) — every one of those
# changes what a household's phone is told to do, so a failure here is a prompt
# to confirm the change was intended, not a nuisance.
#
# TO UPDATE: run the assertion, read the actual digest out of the failure
# message, and paste it here in the same commit as the intended change.
#
# UPDATED 2026-07-26 (flat-linearization PR-3b): the "3-entry" main-session
# entry became the CLOUD plan — the intended product change, not drift. The
# measurement is now the spatial cloud (plan fundamental 1), so the main
# session emits CHECK + MEASURE + N−1 prompted pre-apply positions + VERIFY
# + M−1 prompted post-apply positions. The re-verify re-arm plan is UNCHANGED
# and its digest is byte-for-byte the pre-PR-3b one: it re-runs the
# single-position tracking verdict, and evidence cannot cross capture sessions
# anyway (§5.6), so a cloud there could never join the original one.
#
# RE-DERIVED 2026-07-26 (round-1 review): N 8 → 9 (adjudication 3a — the
# delivered curve must rest on 8 summed sweeps, which is N−1, so the floor is
# met in CURVES not positions) took the plan 15 → 16 entries, and the entry
# titles gained the "— hold still" suffix that disambiguates them from the
# phone's own capture counter (nit N1). Both are intended copy/shape changes.
#
# UPDATED 2026-07-27 (flow-simplification PR-U1): the screen GRAMMAR changed on
# every entry of both shipped plans — each now carries the one server-derived
# counter (`progress`), the instruction as `title`, and the supporting clause
# as `body` (§2.1); the VERIFY entry additionally gained the
# `confirm_title`/`confirm_body` keys the post-apply tap renders (§2.2), and
# the 1-entry re-verify leads with the "you do NOT need to redo the walk"
# sentence (§2.4). All intended copy changes. Program DURATIONS deliberately
# did NOT move at THAT revision: the §2.5 courtesy-tone fix reordered where
# the prelude is spliced without changing its length, so every `duration_ms`
# was byte-identical to the pre-fix plan — a useful independent check that the
# pacing change was a reorder and not a lengthening. (The 2026-07-28 revision
# below is the first one that does lengthen them, and says so.)
#
# ADDED 2026-07-27: a third `"express"` pin. The express tier is a second
# SHIPPED plan shape (N=5, M=1 — flow-simplification §1.1), so it earns the
# same protection the full plan has: a change to its copy, counts, or the
# M=1 done-screen placement must be an intended edit, not drift.
#
# RE-DERIVED 2026-07-28 (issues #1810 / #1812): this time the DURATIONS did
# move, and only they — the pre-pilot ambient window adds exactly 1000 ms to
# every program that carries a leading pilot pair. Measured on this fixture:
# check 22819 ms (unchanged — CHECK's own 12 s ambient window already served
# the guard), measure 39385 → 40385, verify and all 14 cloud entries
# 16207 → 17207. Copy, counts, screen keys and byte LENGTH are all identical
# (3897 / 2107 / 324 bytes before and after), because the digit counts of the
# changed numbers did not change — which is precisely why these pins are
# hashes and not lengths.
#
# RE-DERIVED 2026-07-28 (issue #1823): MEASURE's entry — index 1 of both the
# cloud and express plans — flipped from `auto_advance: countdown` to `tap`,
# dropping the countdown-only `countdown_s`/`cancelable` keys with it, and its
# `body` now names what the tap is consenting to. Net +32 bytes on each plan:
# the removed countdown keys are smaller than the added sentence. That sentence
# was rewritten TWICE before landing, which is the useful part of the story —
# #1825/#1829 landed mid-review and made a flat "louder" false in a quiet room
# (hence the "can be the loudest" hedge), and a plain-language ruling then
# replaced "at the level the fit needs" with wording that says what the level
# is FOR. Both are household-visible copy, so both moved these digests; the
# pins are re-derived against the tree they ship on, never copied forward. The
# 1-entry re-verify plan has no MEASURE entry and its digest is byte-for-byte
# unchanged, which is the check that this edit touched only what it meant to.
#
# RE-DERIVED 2026-07-29 (issue #1806, PR-T3 — the two-stage split). This is the
# largest movement these pins have ever taken, because the SHAPE moved rather
# than the copy: one 16-entry session became a 10-entry measuring session and a
# separate 6-entry post-apply one (work order D1/D2). The keys are renamed to
# say which stage each is, and two NEW shipped shapes earn pins of their own —
# stage 2 is a shipped plan now, not a hypothetical.
#
#   cloud     → stage1-full    16 entries, 3929 B → 10 entries, 2301 B
#                              (target 16 → 10, max_attempts 23 → 17)
#   express   → stage1-express  7 entries, 2139 B →  6 entries, 1531 B
#                              (target 7 → 6, max_attempts 14 → 13)
#   (new)       stage2-full     6 entries, 1612 B  (target 6, max_attempts 13)
#   (new)       stage2-express  1 entry,    609 B  (target 1, max_attempts 8)
#
# Every attempt budget is the same derivation as before —
# ``target + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE`` — applied to
# each stage's own target rather than to the sum of both.
#
# What moved BESIDES the entry count, deliberately, and nothing else did:
#   * every ``progress`` counter, because "Measurement N of T" is per-session
#     and T is now 10 (or 6, or 6, or 1) rather than 16;
#   * the done copy moved off stage 1 entirely and onto stage 2's last entry —
#     the ``M = 1`` placement rule moved WITH the post-apply group it is about;
#   * stage 2's anchor carries a truthful pre-arm instruction plus §2.2's
#     unchanged ``confirm_title``/``confirm_body``, where the single-session
#     VERIFY entry carried the apply-hold copy and ``auto_advance: on_apply``.
#     There is no apply inside either session to hold for.
# Program DURATIONS did not move: the same three composed programs at the same
# lengths, redistributed across two plans.
#
# **The 1-entry recovery re-verify is byte-for-byte unchanged — 324 B and the
# same digest it has carried since 2026-07-28.** That is the load-bearing check
# in this revision: work order D2 keeps that form exactly as it is, and an
# identical digest is the proof that generalizing its builder over a plan shape
# left its own output untouched.
# RE-DERIVED 2026-07-30 (issue #1806, PR-T4 — the phone's honesty layer). COPY
# ONLY: every target, attempt budget, entry count, screen key, and program
# duration is byte-identical to the PR-T3 revision above. What moved is the
# prompted-position copy and CHECK's, per work order D7/D8:
#
#   * the position prompts became numeric ABSOLUTE poses in inches AND
#     centimetres, generated from each row's own ``offset_cm`` ("Move the
#     microphone 16 in (40 cm) to the LEFT of the mark, at mark height.") —
#     longer sentences than "A forearm's length LEFT of the mark", which is
#     where nearly all of the added bytes are;
#   * CHECK's entry stopped asking for quiet before the window that
#     deliberately measures an un-hushed room and gained a ``noise_note`` for
#     the phone's own pre-arm floor window (#1835);
#   * the actor became "the microphone" rather than "the phone".
#
#   stage1-full     2301 B → 2918 B  (+617; 8 prompted positions + CHECK)
#   stage1-express  1531 B → 1945 B  (+414; 4 prompted positions + CHECK)
#   stage2-full     1612 B → 1939 B  (+327; 5 prompted positions, no CHECK)
#   stage2-express   609 B →  609 B  (UNCHANGED — one anchor, no prompted move)
#   1-entry          324 B →  324 B  (UNCHANGED)
#
# The two unchanged digests are the load-bearing check in this revision: both
# are plans with no prompted position and no CHECK entry, so an identical
# digest is the proof that a prompt-copy rewrite reached exactly the entries it
# was about. Byte lengths grew and are pinned alongside the hashes because this
# copy sits inside the capture's 4 KiB per-screen cap — see
# ``test_cloud_plan_stays_inside_the_capture_spec_byte_budgets`` for the margin.
#
# RE-DERIVED 2026-07-30 (issue #1941 stage 2, R4 — the vocabulary sweep). COPY
# ONLY, and a single string of it: the 1-entry recovery re-verify's ``body``
# became "Put the microphone back on the mark and hold it still." The PR-T4
# revision above finished the sweep in the PROMPTED-position copy; this one
# reaches the last screen in this file that still called the instrument a phone.
#
#   stage1-full     2918 B → 2918 B  (UNCHANGED)
#   stage1-express  1945 B → 1945 B  (UNCHANGED)
#   stage2-full     1939 B → 1939 B  (UNCHANGED)
#   stage2-express   609 B →  609 B  (UNCHANGED)
#   1-entry          324 B →  329 B  (+5 — exactly len("microphone") - len("phone"))
#
# FOUR unchanged digests are the load-bearing check in this revision, and the
# +5 is the whole diff: a noun swap that reached one screen and nothing else.
# No target, attempt budget, entry count, screen key, or program duration moved.
#
# RE-DERIVED 2026-08-18 — the two session trims. NO COPY MOVED; what moved is
# per-entry ``duration_ms`` (the courtesy prelude now rides only the capture
# that OPENS a session, ``crossover_v2.programs.courtesy_prelude_for_phase``)
# and stage 2's entry COUNT (``DEFAULT_CLOUD_VERIFY_POSITIONS`` 6 → 5).
#
#   stage1-full     2918 B → 2918 B  (length UNCHANGED; MEASURE and the 8
#                                     prompted positions each −3600 ms, same
#                                     digit count. These two entries build the
#                                     BUILDER's bare defaults — pre-apply cloud
#                                     on, lateral off — not the shipped stage 1)
#   stage1-express  1945 B → 1945 B  (length UNCHANGED; same reason)
#   stage2-full     1942 B → 1683 B  (−259; one prompted position dropped, and
#                                     the four that remain are −3600 ms each)
#   stage2-express   630 B →  630 B  (UNCHANGED, digest included)
#   1-entry          329 B →  329 B  (UNCHANGED, digest included)
#
# The two UNCHANGED DIGESTS are the load-bearing check again, and they check
# the trim's scope precisely: both are plans whose only entry is the announced
# anchor, so a byte-identical digest says the prelude rule left the announced
# capture alone while it took 3.6 s off every other one.
#
# RE-DERIVED 2026-08-24 — the geometry ruling's post-apply pose set. ONE PLAN
# MOVED. Stage 2's walk stopped taking a prefix of ``CLOUD_POSITION_PROMPTS``
# and took its own table, ``CLOUD_VERIFY_POSE_PROMPTS``, whose first member is
# the DESIGN AXIS — so ``DEFAULT_CLOUD_VERIFY_POSITIONS`` went 5 → 6 and Full's
# stage 2 gained one prompted entry. No copy changed on any pose that already
# existed, and no duration moved.
#
#   stage1-full     2918 B → 2918 B  (UNCHANGED, digest included)
#   stage1-express  1945 B → 1945 B  (UNCHANGED, digest included)
#   stage2-full     1683 B → 1925 B  (+242; one prompted at-mark entry, plus
#                                     five progress labels reading "of 6"
#                                     instead of "of 5" — same digit count)
#   stage2-express   630 B →  630 B  (UNCHANGED, digest included)
#   1-entry          329 B →  329 B  (UNCHANGED, digest included)
#
# FOUR unchanged digests are the load-bearing check, and they bound the ruling's
# scope exactly: stage 1 does not walk the post-apply table at all, and neither
# single-entry stage-2 plan has a prompted pose to gain one — so a change that
# reached any of them would have been a change to something it was not about.
_GOLDEN_V2_PLAN_BYTES = {
    "stage1-full": (
        2918,
        "2127852c43a515dd855042e2c99f37a35d877cc32d0d78ba94299c019d19ad3b",
    ),
    "stage1-express": (
        1945,
        "74e9e4d773d5f7bfb717c7429421f098f4e89f0276a59340f32928e61826cb64",
    ),
    # Moved by #1964: Full's done_body no longer pre-commits "Verified and
    # applied." before the first tone plays. Moved again by the 2026-08-24
    # geometry ruling: the walk gained its design-axis pose.
    "stage2-full": (
        1925,
        "485a0ab680e52625c21fda3da47a3dea0cc34e85d6c5d0a68621db08fefebdbf",
    ),
    # Moved by #1964's fix round: Express's upgrade-path phrase drops the
    # withdrawn "verified-everywhere" overclaim for the B2 wording jts.local
    # already ships.
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
    """A conductor whose caps come from a REAL confirmed safety profile, plus the
    (topology, profile, targets, session_volume) that admission needs."""
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )

    from tests.test_active_speaker_program_admission import _profile_and_targets

    topology, profile, targets = _profile_and_targets(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )
    sv = session_measurement_volume_db(profile, targets.values())
    caps = {"woofer": float(woofer_peak), "tweeter": float(tweeter_peak)}
    # Bands within the profile's permitted [500, 20000] excitation band.
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
) -> PilotObservation:
    return PilotObservation(
        role=role, level_lo_dbfs=-40.0, level_hi_dbfs=-30.0,
        programmed_delta_db=programmed_delta_db, captured_delta_db=captured_delta_db,
        linearity_ok=linearity_ok, channel_map_ok=channel_map_ok, snr_valid=snr_valid,
        snr_db=snr_db,
        channel_map_target_rise_db=target_rise_db,
        channel_map_cross_rise_db=cross_rise_db,
    )


def _driver_response_diag(
    role: str, *, window_ms: float = 8.0, floor_hz: float | None = None,
    snr_db: float | None = None, snr_verdict: str | None = None,
    snr_band: str | None = "mid",
    floor_source: str = gating.FLOOR_MEASURED,
) -> DriverResponse:
    freqs = np.linspace(100.0, 20000.0, 64)
    # ``band_id`` is part of every ``worst_relevant`` ``snr_policy.
    # band_snr_verdicts`` builds, so this fixture carries it too — a double
    # missing it would let a consumer that drops the band identity look
    # correct (#2613). ``snr_band=None`` is the defensive case
    # ``worst_band_verdict`` can still select: it filters on band overlap and
    # verdict rank, never on identity.
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
        # ``floor_source`` defaults to the "gate found a reflection" state and
        # is overridden per test — the two states print the same
        # ``window_ms`` and mean opposite things (#1966).
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
    """A gating block with the two absolute times AND the priced delta on it.

    The fixture ``_driver_response_diag`` builds carries neither, because the
    consumers it was written for read only ``window_ms``/``floor_source``.
    Ticket 1.5's numbers come off the other fields, so they need a block that
    has them — and one where ``first_reflection_ms`` differs from the DELAY by
    a lot, so a helper that returned the absolute time by mistake could not
    pass by coincidence.
    """
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
    """The eligible fixture's two branch magnitude curves.

    Split out from ``_eligible_measure_analysis`` so ``_FIXTURE_RAW_TRIM_DB``
    below can be SOLVED from the same curves the fixture hands the conductor
    (see that constant for why a hand-written trim stopped being acceptable).

    **Each branch carries its own crossover (R10a, #1817).** These curves stand
    in for a per-driver MEASUREMENT, and a real one is captured through the
    graph the speaker is running — so the woofer's low-pass and the tweeter's
    high-pass belong in them. Verified on the banked 2026-07-30 bench session
    rather than assumed (``captures/r10a-objective-20260801/premise_probe.py``):
    against its own passband median the real JTS3 woofer measures **-3.22 dB at
    0.79*Fc, -7.32 dB at Fc and -32.62 dB at 2*Fc**, and the real tweeter
    **-38.81 dB at 0.79*Fc**, both tracking their committed LR4 sections (and
    falling faster still, since the driver's natural rolloff adds to the
    electrical one).

    Before R10a they did NOT, and it did not show: a flat target and a
    crossover-free curve are self-consistent, so the fixture modelled an
    impossible speaker without contradicting anything. The crossover-shaped
    target is what makes the omission observable — a fit graded against the
    shape of a crossover its own measurement never went through would be asked
    to CUT its passband edges into existence. Two tests in this file
    (``test_..._one_set_of_crossover_sections``'s neighbours at the
    ``crossover_response_db`` call sites below) had already adopted the
    faithful shape locally; this makes it the fixture's own.
    """
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, crossover_response_db,
    )

    freqs = _LINEARIZABLE_FREQS_HZ
    # A mild, monotonic -1.5 dB/octave tilt around Fc (capped at +/-6 dB
    # at the band extremes) -- NOT a perfectly flat 0 dB reference.
    # #1667's ripple-optimal trim solve needs the woofer branch to carry
    # SOME of its own frequency-dependent shape: against a perfectly
    # flat woofer, attenuating the tweeter toward silence is always
    # "more flat" (there is nothing on the woofer side to trade off
    # against), so the search has no genuine interior minimum and walks
    # to its own scan-window edge -- which the sanity guard then
    # (correctly) distrusts and rejects. A mild tilt is enough for a
    # real interior optimum to exist while leaving the tweeter-bump
    # linearity checks below unaffected (the fit and its own filters are
    # sibling-independent; verified offline).
    woofer_db = np.clip(-1.5 * np.log2(np.maximum(freqs, 1.0) / 1600.0), -6.0, 6.0)
    # …plus a -6 dB dip at 400 Hz, INSIDE the woofer's own radiating band
    # (#1809). Without it this fixture cannot exercise the lift vocabulary at
    # all any more, and the reason is the defect #1809 filed: the tilt's only
    # deficit is its falling tail, which lives ABOVE the 1600 Hz crossover,
    # where the woofer has handed off. Every boost this fixture used to emit
    # was a stopband boost — a miniature of the 2026-07-28 JTS3 profile, where
    # +11.6155 dB at 2747 Hz sat 750 Hz into the woofer's own LR4 stopband. A
    # real in-band dip is what a driver the fit should lift actually looks
    # like, and it lands the fixture's own boost at +4.25 dB / 399 Hz, beside
    # that profile's real +4.8807 dB / 377.4 Hz. It moves the solved raw trim
    # by 0.002 dB, so nothing else in this file shifts under it.
    woofer_db = woofer_db - 6.0 * np.exp(
        -0.5 * ((np.log2(freqs / 400.0) / 0.3) ** 2)
    )
    # A bump inside the [800, 3200] Hz overlap band (Fc=1600) — validated
    # offline (PR-C sanity pass) to survive envelope/fit and move the
    # re-solved trim measurably vs the raw candidate.
    #
    # **+3 dB at 2400 Hz since R10a; it was +6 dB at 1500 Hz.** Both numbers
    # moved for the same reason — the fixture became faithful above — and both
    # were measured rather than guessed
    # (``captures/r10a-objective-20260801/fixture_sweep.txt``):
    #
    # * 1500 -> 2400 Hz, because the bump has to sit in the tweeter's OWN
    #   radiating band (LR4 high-passed at 1600 Hz, so 1997 Hz up) to be a
    #   driver defect at all. At 1500 Hz the branch's own crossover is ~7 dB
    #   down, so a faithful fixture attenuates the bump before it reaches the
    #   sum and the fit is asked to correct something the graph already
    #   removed. 2400 Hz keeps it inside the overlap band the trim scans.
    # * 6 -> 3 dB, because a bump that big in the tweeter's own passband
    #   drives the ripple-optimal trim to -3.9 dB, which puts the shared level
    #   frame 4.361 dB apart — past its frozen 3.0 dB tolerance, so the gate
    #   (correctly) refuses before any of this file's other assertions run.
    #   The sweep is monotonic and unambiguous: at 2400 Hz the disagreement is
    #   2.643 dB at +3, 3.275 at +4, 3.760 at +5, 4.361 at +6. +3 dB is a
    #   realistic driver defect that still moves the trim measurably
    #   (-2.267 dB) and still lands the fixture's boost at +3.72 dB / 399 Hz.
    tweeter_db = 3.0 * np.exp(-0.5 * ((np.log2(freqs / 2400.0) / 0.25) ** 2))
    # …each behind its own committed section, so the curve the fixture hands
    # the conductor is the one a measurement would have produced.
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
    """The raw trim a pair of branch curves actually call for.

    **Why this is solved and not written down** (PR-L4). The fixture carried a
    hand-written ``{"woofer": 0.0, "tweeter": -2.211}`` chosen to exercise the
    ripple scan, but its two branches are near-equal-sensitivity synthetics
    that call for about -0.7 dB. Nothing noticed, because until PR-L4 nothing
    in the chain ever compared the two branches' realized levels — which is
    precisely the hole PR-L4 item 1 exists to close, reproduced inside the test
    fixture that was meant to model the thing. A production MEASURE analysis
    derives ``candidate.trim_db`` from its own branches via
    ``solve_branch_trims``, so this fixture now does the same and cannot drift
    from its own physics again.

    **Why it takes optional curves instead of only reading the default pair**
    (#1938). ``_eligible_measure_analysis`` reuses this exact solve for
    whichever ``woofer_db``/``tweeter_db`` a call is actually using, default or
    custom. Defaulting a custom-curve call's trim to a constant solved from the
    DEFAULT curves (below) hands the conductor one speaker's branches and
    another speaker's trim — the identical incoherence this function was
    written to remove for the default curves in PR-L4, reintroduced through
    the custom-curve parameters (#1938). Arguments default to
    :func:`_fixture_branch_db`'s pair, so ``_FIXTURE_RAW_TRIM_DB`` below is
    unaffected by this generalization.

    **Why the woofer trim is returned too, not hardcoded to 0.0** (#1938
    gate follow-up). Before this generalization, the woofer trim was always
    ``0.0`` by construction: the one fixed pair this function ever solved had
    a quieter woofer, so ``solve_branch_trims``'s "attenuate the louder
    branch" rule always left the woofer untouched. That stopped being true
    the moment this function started solving ARBITRARY curve pairs — a pair
    whose woofer is louder in its own band needs a nonzero woofer trim, and
    hardcoding it to 0.0 silently drops that attenuation. Returning both
    solved values is the same fix this function exists for, applied to
    itself.
    """
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
    """The RAW pre-fit two-branch sum of the eligible fixture's own branches.

    PR-L4 review B1: item 2's gate grades this against the LINEARIZED
    prediction through the same evaluator, so the baseline has to be the
    fixture's real uncorrected sum. It used to be a hardcoded flat zero curve,
    which claimed the uncorrected speaker was already perfect and made every
    correction score as a regression — the same "a fixture field nobody derived
    from the fixture" shape as the hand-written raw trim above.

    #1938 gate follow-up: an omitted ``trim_db`` is derived from THESE curves
    (default or custom), never from ``_FIXTURE_RAW_TRIM_DB`` — the same
    default-curves-constant trap ``_eligible_measure_analysis`` had, closed
    here too even though every current caller already passes matching
    curves/trim together (no caller today hits this branch with an
    incoherent pair — this is closing the trap-door, not fixing an observed
    incoherence).
    """
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
        # #1938: derive the trim from THESE branches (default or custom) —
        # never default to a constant solved from a DIFFERENT pair. A caller
        # that hands the conductor custom woofer_db/tweeter_db but inherits
        # _FIXTURE_RAW_TRIM_DB (solved from the DEFAULT curves) hands it one
        # speaker's branches and another speaker's trim — the exact defect
        # _solve_fixture_raw_trim's docstring documents for the default curves,
        # closed here for every curve pair. A caller that deliberately wants an
        # incoherent pair passes trim_db= explicitly (the level gates' own
        # branch-forcers, e.g.
        # test_the_level_frame_refusal_names_the_levels_and_bands_it_read).
        trim_db = _solve_fixture_raw_trim(woofer_db, tweeter_db)
    if trim_band_average_db is None:
        # Production always sets it (`_build_candidate`), and it COINCIDES with
        # the applied trim whenever the ripple polish did not move it. Splitting
        # the two is what a test does to exercise PR-L5's level-frame gate,
        # which reads the level-match result rather than the polished trim.
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
        # The RAW pre-fit two-branch sum of THIS fixture's own branches at THIS
        # fixture's own trim — not a flat zero curve (PR-L4 review B1). Item 2's
        # gate grades this against the LINEARIZED prediction through the same
        # evaluator, so a hardcoded-flat baseline claimed the uncorrected
        # speaker was already perfect and made every correction look like a
        # regression. Same incoherence class as the hand-written trim above:
        # a fixture field that was never derived from the fixture.
        predicted_sum=_fixture_raw_predicted_sum(
            woofer_db=woofer_db, tweeter_db=tweeter_db, trim_db=trim_db,
        ),
        glitch_detected=False,
    )


def _way1_measure_analysis(program) -> ProgramAnalysis:
    """:func:`_eligible_measure_analysis`'s 1-way twin: every inter-driver field
    absent with a name, as ``_analyze_measure`` returns for a one-role program.
    ``predicted_sum`` is not one of them — one branch sums to itself.

    The branch carries one dip a lift would target and one bump a cut would
    remove, on a FLAT baseline rather than :func:`_fixture_branch_db`'s tilt:
    the tilt exists for the ripple-optimal trim solve a lone branch never runs,
    and broadband it would spend the whole lift budget on an HF-tail request
    the envelope refuses, leaving a cut-only round looking like a decision
    rather than an absence of material.
    """
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
    """A conductor whose TWEETER sweep starts AT Fc — JTS3's real geometry.

    ``overlap_band_hz`` then clamps the shared band to ``[Fc, 2*Fc]``, the
    one-sided shape PR-L3 is about. Built inline rather than through
    ``_conductor`` because the role bands are the whole point of the fixture.
    """
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
    """``(before_rms_db, after_rms_db)`` — item 2's two terms, recomputed from
    the conductor's own predictions through the SAME evaluator the gate used."""
    before = spec_report_for_predicted_sum(
        _fixture_raw_predicted_sum()
    )
    after = spec_report_for_predicted_sum(conductor.measure_predicted_sum)
    return (
        spec_convergence_residual(before).rms_db,
        spec_convergence_residual(after).rms_db,
    )


def _probed_conductor(fakes: FakeSeams, *, rollback=None, entry_error_db=0.0):
    """A conductor walked to the point where VERIFY is the next capture.

    Uses the ELIGIBLE measure fixture because a probe needs something to have
    been commanded: an ineligible session emits no linearization filters, so
    relative to the raw crossover it commands nothing this probe can grade
    (pinned by ``test_the_commanded_delta_is_none_for_a_trims_only_candidate``).

    **The pre-apply anchor is STATED, not inherited** — ``entry_error_db``, the
    model-vs-measurement disagreement this session went in with, defaulting to
    the exactly-anchored 0.0. #2533 made the residual a change measured against
    that capture and three tests started saying so by hand; series-2 D1 made the
    two directional safety findings a change against it too, per bin, so every
    probe fixture needs it. Without it the walk's entry capture is an unrelated
    synthetic response and its anchor is a −4.5 dB phantom that no test intends
    and none states — which is a fixture measuring itself. Pass a value (or a
    callable of frequency) to state a different one deliberately.
    """
    fakes.rollback = rollback
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    c.note_apply_complete()
    _anchor_entry_baseline(c, entry_error_db)
    return c


def _tracking_curve(c, error_db):
    """VERIFY's smoothed ``(freqs, measured, predicted)`` triple, on the grid
    the session's own commanded delta lives on, with ``error_db`` (a callable
    of frequency, or a scalar) as measured−predicted."""
    freqs = np.asarray(c.measure_commanded_delta[0], dtype=float)
    predicted = np.asarray(c.measure_predicted_sum[1], dtype=float)
    error = error_db(freqs) if callable(error_db) else np.full_like(freqs, error_db)
    return freqs, predicted + error, predicted


def _anchor_entry_baseline(c, error_db=0.0):
    """Give ``c`` a PRE-apply capture in ``_tracking_curve``'s own frame (#2533).

    The delta probe now reads the entry baseline as the anchor its residual is
    measured against, so a probe fixture has to say what the model-vs-measurement
    disagreement was BEFORE the apply — the same way ``_tracking_curve`` says
    what it is after. ``error_db`` (callable or scalar) is that disagreement:
    ``0.0``, the default, is a session whose model was anchored exactly right
    going in, which is what every pre-#2533 probe test implicitly assumed by
    reporting the absolute post-apply disagreement as a level move.

    Without this the walked fixture's entry capture is an unrelated synthetic
    response, so its anchor is a phantom (−4.5 dB on the eligible fixture) that
    no test intended and none states.

    Replaces only the curve and its exclusion mask; the record's identity
    (program, mark, graph fingerprint, capture time) is the one the walk banked.
    """
    freqs = np.asarray(c.measure_commanded_delta[0], dtype=float)
    commanded = np.asarray(c.measure_commanded_delta[1], dtype=float)
    predicted = np.asarray(c.measure_predicted_sum[1], dtype=float)
    error = error_db(freqs) if callable(error_db) else np.full_like(freqs, error_db)
    # measured_pre = predicted_raw + error, and predicted_raw is the post-apply
    # prediction with the command taken back out — the same recovery
    # ``entry_delta_db`` performs.
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
    """Spy recording the WHOLE ``FitVocabulary``, not just ``allow_boost``.

    ``_boost_vocabulary_spy`` answers "was boost permitted"; the ruling also
    makes a claim about what the permission came WITH (an empty exclusion set,
    because there is no spatial evidence to compose), so that needs the object.
    """
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
    """Two HEALTHY drivers, each measured THROUGH its own side of a matched
    LR4 pair at the fixture's Fc, each with one benign in-band dip.

    "Healthy" is the load-bearing word and it is true by construction: the two
    branches are the SAME flat driver behind mirrored halves of one crossover,
    so they sit at the same level and a level gate has nothing to find. The
    dips (400 Hz on the woofer, 6 kHz on the tweeter) are inside each driver's
    own radiating band, so whatever the fit does about them is driver work
    rather than crossover work.

    **The depth no longer selects an outcome, and the sweep that says so is
    below (R10a, #1817).** It used to: the depth was tuned so the two arms
    straddled item 2's 0.5 dB improvement floor, 7 dB being the value that
    maximised the smaller margin (pre 0.132 dB below the floor, post 0.091 dB
    above it). That straddle is gone. Re-swept against the shaped target,
    reading ``event=correction.crossover_v2_prediction_gate`` (floor 0.5 dB;
    ``pre`` is the pre-#1929 arm, ``post`` the shipped one):

        depth dB        3        5        7        9       11       15
        pre  improve  -0.044   +0.023   +0.036   +0.045   +0.062   +0.106
        post improve  -0.011   +0.024   +0.035   +0.046   +0.062   +0.105
        pre  frame     7.886    6.916    6.156    5.509    5.566    6.389
        post frame     0.947    0.973    0.988    1.058    0.919    0.951

    Both arms are refused at every depth, and the two arms' improvements never
    differ by more than 0.001 dB. The cause is #1809's own doctrine rather than
    a fixture that needs more amplitude: **a cut-only fit cannot fill a dip.**
    Under the old flat target the fit generated large spurious cuts instead —
    the tweeter drew about 9 dB of broadband cut, because a level mask spanning
    the crossover dragged its ``target_level_db`` to −9.80 dB for a driver whose
    passband is ~0 dB — and those cuts moved the predicted sum enough to clear
    the floor. With the shaped target the tweeter draws ZERO filters and the
    woofer's residual halves (3.4308 → 1.7063 dB rms), so what is left to grade
    is a speaker a cut-only correction genuinely cannot improve. Flipping the
    sign does not restore the straddle either: at +5/+7/+9/+11 dB bumps BOTH
    arms complete.

    7 dB is retained because nothing selects a depth any more, and moving it
    would churn the two frame numbers the caller pins for no gain. What still
    binds at 7 dB, and is what the caller reads: the pre-#1929 frame is over
    the 3.0 dB tolerance while its realized check passes (so #1866's
    finding-and-proceed path is the one exercised, not the hard refusal), and
    the shipped frame is well under it.

    The declared sweep spans (``_roles()``: woofer 150-6000, tweeter
    300-20000) both cross the 1600 Hz Fc, which is the #1929 premise and the
    ordinary case for a real declaration — the woofer radiates only below
    1282 Hz and the tweeter only above 1996 Hz.
    """
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
    """A real ``CombinedResponse`` whose positions each carry one narrow notch
    at their own frequency — so the cross-position check has something to
    disagree about, below the registry's 4 kHz floor."""
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


# --------------------------------------------------------------------------- #
# Banked rounds that carry CAPTURES
# --------------------------------------------------------------------------- #

#: Every round below is banked at the pipeline's own rate.
CAPTURE_RATE = 48_000

#: The pose ladder a walked round declares, cycled when a round has more
#: captures than the walk has stops.
CAPTURE_AZIMUTHS_DEG = (-22.0, -7.0, 0.0, 7.0, 22.0)

#: Both programs a banked round carries, and the phase every sidecar's
#: ``provenance.stimulus`` NAMES whichever one its ``wav_sha256`` hashes. On a
#: cloud round the two disagree — the live mislabel #3504 documents.
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
    """A banked-round directory whose captures are known convolutions.

    One capture per entry in ``irs``: the played program convolved with that
    impulse response, peak-normalised, beside the sidecar declaring its pose —
    so what a reader of this round should recover is knowable in advance.

    Both programs are written whichever one played, so every round built here
    exercises the binding a reader has to get right: a capture belongs to the
    program its bytes HASH, never to the one its phase label names.

    ``radiated_band_hz`` of ``None`` omits the ``curves`` key — the shape of a
    round banked without a declared radiated band.
    """
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
    return root


def fake_measurement_mic():
    """The resolved measurement mic a suite names rather than probes: no
    machine running these tests has one plugged in."""
    from jasper.audio_measurement.wired_capture import WiredMicDevice

    return WiredMicDevice(
        card_id="UMIK2", card_index=9, usb_id="2752:0072",
        model_key="minidsp_umik2", model_label="miniDSP UMIK-2",
    )
