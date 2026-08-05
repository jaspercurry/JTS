"""R16 — the lateral-evidence producer (crossover-linearization plan §4.4).

Everything here is hardware-free. The harness (``FakeSeams``, ``_conductor``,
``_run_phase``) is the conductor suite's own, imported rather than re-built for
the reason the program-analysis equivalence fixture already imports it: two
copies of a conductor factory is two definitions of what a session is.
"""

from __future__ import annotations

import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CHECK,
    PHASE_LATERAL,
    PHASE_MEASURE,
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
    POSITION_ROLE_XOVR,
    REASON_AGC_BEHAVIORAL_FAIL,
    REASON_CLIPPED,
    REASON_DRIFT_BASELINES_DISAGREE,
    REASON_LOCATE_FAILED,
    REASON_PILOT_LEVEL_COLLAPSE,
    LateralPose,
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
    lateral_evidence_grid_hz,
    resolve_plan_shape,
)
from jasper.audio_measurement.program import KIND_SWEEP
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
)

from tests.test_crossover_v2_conductor import (  # noqa: E402
    FC_HZ,
    FakeSeams,
    _alignment,
    _conductor,
    _measure_analysis,
    _roles,
    _run_phase,
)

LATERAL_COUNT = len(flow.LATERAL_POSE_PROMPTS)
FIRST_LATERAL_INDEX = 3
LAST_LATERAL_INDEX = FIRST_LATERAL_INDEX + LATERAL_COUNT - 1


def _lateral_conductor(fakes: FakeSeams, **kwargs):
    """A conductor whose stage 1 is CHECK + MEASURE + the lateral walk."""
    return _conductor(
        fakes,
        index_phase_map=build_v2_cloud_index_phase_map(
            tier="full", include_cloud_measure=False, include_lateral=True,
        ),
        **kwargs,
    )


def _walk(conductor, *, through: int = LAST_LATERAL_INDEX) -> list[dict]:
    """CHECK, MEASURE, then the lateral poses up to and including ``through``."""
    out = [_run_phase(conductor, 1, 1), _run_phase(conductor, 2, 1)]
    for index in range(FIRST_LATERAL_INDEX, through + 1):
        out.append(_run_phase(conductor, index, 1))
    return out


# --- the pose table -----------------------------------------------------------


def test_the_walk_is_derived_from_the_cloud_table_and_bracketed_by_the_mark():
    """§4.4: "reuses the existing ±12 cm and ±40 cm left/right moves".

    Derived by PREDICATE off ``CLOUD_POSITION_PROMPTS``, so the two tables
    cannot state different distances for the same move, and bracketed by the
    two at-mark poses the drift check reads.
    """
    poses = flow.LATERAL_POSE_PROMPTS
    assert poses[0] is flow.LATERAL_MARK_PROMPT
    assert poses[-1] is flow.LATERAL_MARK_RETURN_PROMPT
    assert [p.offset_cm for p in poses] == [0.0, 12.0, 12.0, 40.0, 40.0, 0.0]
    assert [p.role for p in poses] == [
        POSITION_ROLE_ONAX, POSITION_ROLE_ONAX, POSITION_ROLE_ONAX,
        POSITION_ROLE_OFFAX, POSITION_ROLE_OFFAX, POSITION_ROLE_ONAX,
    ]
    # No vertical pose: §4.4 names lateral moves, and a vertical one answers a
    # different question (the xovr lobe) that this round does not claim.
    assert all(p.role != POSITION_ROLE_XOVR for p in poses)
    # One LEFT and one RIGHT at each offset — what makes a left/right
    # disagreement statement meaningful at all.
    for offset in (12.0, 40.0):
        sides = [p.headline for p in poses if p.offset_cm == offset]
        assert len(sides) == 2
        assert sum("LEFT" in h for h in sides) == 1
        assert sum("RIGHT" in h for h in sides) == 1
    # Every prompt states a distance it actually carries (the generated-copy
    # rule), and the at-mark rows never quote one.
    for pose in poses:
        if pose.offset_cm:
            assert flow.format_position_distance(pose.offset_cm) in pose.headline
        else:
            assert "cm)" not in pose.headline


def test_the_walk_derivation_refuses_a_cloud_table_that_lost_a_lateral_row():
    """Mutation of the import-time guard: drop one 40 cm row from the cloud
    table and the derived walk is lopsided, so the guard must fire."""
    survivors = tuple(
        p for p in flow.CLOUD_POSITION_PROMPTS
        if not (p.offset_cm == 40.0 and "LEFT" in p.headline)
    )
    derived = (
        (flow.LATERAL_MARK_PROMPT,)
        + tuple(
            p for p in survivors
            if p.role != POSITION_ROLE_XOVR
            and float(p.offset_cm) in flow._LATERAL_POSE_OFFSETS_CM
        )
        + (flow.LATERAL_MARK_RETURN_PROMPT,)
    )
    assert len(derived) != 2 * len(flow._LATERAL_POSE_OFFSETS_CM) + 2


# --- the capture plan ---------------------------------------------------------


def test_stage_1_walks_the_poses_with_the_anchors_own_program_duration():
    plan = build_v2_capture_plan(
        _roles(), FC_HZ, tier="full",
        include_cloud_measure=False, include_lateral=True,
    )
    kinds = [entry.kind_label for entry in plan.entries]
    assert kinds == ["check", "measure"] + ["lateral"] * LATERAL_COUNT
    measure_entry = plan.entries[1]
    for entry, prompt in zip(plan.entries[2:], flow.LATERAL_POSE_PROMPTS):
        # A pose replays the MEASURE program, so its budget is MEASURE's — a
        # phone sized for the summed sweep would stop recording mid-pose.
        assert entry.duration_ms == measure_entry.duration_ms
        assert entry.screen["title"] == prompt.headline
        assert entry.screen["auto_advance"] == flow.AUTO_ADVANCE_TAP
        assert entry.screen["progress"] == flow.capture_progress_label(
            entry.index + 1, plan.capture_target
        )
    assert plan.capture_target == 2 + LATERAL_COUNT


def test_the_index_phase_map_and_the_emitted_entries_agree():
    for include_cloud in (False, True):
        mapping = build_v2_cloud_index_phase_map(
            tier="full", include_cloud_measure=include_cloud, include_lateral=True,
        )
        plan = build_v2_capture_plan(
            _roles(), FC_HZ, tier="full",
            include_cloud_measure=include_cloud, include_lateral=True,
        )
        assert len(mapping) == plan.capture_target
        assert [mapping[e.index + 1] for e in plan.entries] == [
            {"check": PHASE_CHECK, "measure": PHASE_MEASURE,
             "lateral": PHASE_LATERAL,
             "cloud_measure": flow.PHASE_CLOUD_MEASURE}[e.kind_label]
            for e in plan.entries
        ]
        # The walk sits between the anchor and any pre-apply cloud.
        lateral_indexes = [i for i, p in mapping.items() if p == PHASE_LATERAL]
        assert lateral_indexes == list(range(3, 3 + LATERAL_COUNT))


def test_the_retry_budget_is_byte_identical_on_both_pre_r16_shapes():
    """The ``max_attempts`` derivation moved from the shape's cloud arithmetic
    to the plan's own entries. It must reproduce both shipped values exactly."""
    shape = resolve_plan_shape("full")
    with_cloud = build_v2_capture_plan(
        _roles(), FC_HZ, plan_shape=shape, include_cloud_measure=True,
    )
    assert with_cloud.max_attempts == shape.measure_max_attempts
    without = build_v2_capture_plan(
        _roles(), FC_HZ, plan_shape=shape, include_cloud_measure=False,
    )
    assert without.max_attempts == (
        without.capture_target + flow.CLOUD_RETAKE_ALLOWANCE
    )
    # And the walk grows the budget with itself rather than borrowing. Both
    # shapes, because the cloud-OFF one is satisfied by the OLD derivation too
    # (its ``target`` already counted the poses); only the cloud-ON one
    # distinguishes them, and that is the case the change exists for — a plan
    # whose entries exceed ``shape.measure_capture_target``.
    for include_cloud, baseline in ((False, without), (True, with_cloud)):
        walked = build_v2_capture_plan(
            _roles(), FC_HZ, plan_shape=shape,
            include_cloud_measure=include_cloud, include_lateral=True,
        )
        assert walked.capture_target == baseline.capture_target + LATERAL_COUNT
        assert walked.max_attempts == baseline.max_attempts + LATERAL_COUNT


def test_the_relay_capacity_guard_counts_the_lateral_walk():
    """The worst case the relay must carry is cloud PLUS walk, not cloud alone.

    Re-derived rather than asserted: ``ceiling`` is picked to sit strictly
    between the cloud-only requirement and the cloud-plus-walk one, so the
    guard passing there would prove it had stopped counting the poses.
    """
    import jasper.capture_relay.spec as spec

    flow.assert_cloud_plan_fits_relay_capacity()  # holds today
    cloud_only = flow.cloud_plan_max_attempts(
        cloud_measure_positions=flow.MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=flow.DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    with_walk = cloud_only + LATERAL_COUNT
    assert with_walk <= spec.MAX_CAPTURE_PLAN_ATTEMPTS, (
        "the shipped relay ceiling no longer carries the worst-case plan"
    )
    ceiling = cloud_only + 1
    assert ceiling < with_walk, "the walk must be long enough to be countable"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spec, "MAX_CAPTURE_PLAN_ATTEMPTS", ceiling)
        with pytest.raises(flow.CrossoverV2FlowError):
            flow.assert_cloud_plan_fits_relay_capacity()


def test_a_lateral_only_stage_1_still_consents_to_a_walk():
    """The consent copy was gated on the CLOUD. A lateral-only session prompts
    five moves, so promising a stationary microphone would be a lie."""
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24, tier="full",
        include_cloud_measure=False, include_lateral=True,
    )
    assert spec.capture_plan.capture_target == 2 + LATERAL_COUNT
    notes = [c["text"] for c in spec.screen if c["type"] == "note"]
    assert flow.walk_shape_for(cloud_positions=0, lateral=True) in notes
    # The tier line is the ``guided_tier`` half, and it only renders for a
    # session the spec builder was told is a guided WALK.
    steps = [i for c in spec.screen if c["type"] == "steps" for i in c["items"]]
    assert any("Full measurement" in step for step in steps)
    # …and the pre-R16 no-walk shape still says none of it.
    quiet = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24, tier="full",
        include_cloud_measure=False, include_lateral=False,
    )
    quiet_notes = [c["text"] for c in quiet.screen if c["type"] == "note"]
    assert not any("of the mark" in note for note in quiet_notes)
    quiet_steps = [
        i for c in quiet.screen if c["type"] == "steps" for i in c["items"]
    ]
    assert not any("Full measurement" in step for step in quiet_steps)


def test_the_walk_shape_quotes_the_furthest_group_it_runs():
    lateral_only = flow.walk_shape_for(cloud_positions=0, lateral=True)
    assert flow.format_position_distance(50.0) in lateral_only
    # Unchanged for a cloud-only session, and the combined sentence is the
    # cloud's (its table reaches further).
    assert flow.walk_shape_for(cloud_positions=9, lateral=False) == (
        flow.cloud_walk_shape(9)
    )
    assert flow.walk_shape_for(cloud_positions=9, lateral=True) == (
        flow.cloud_walk_shape(9)
    )
    assert flow.walk_shape_for(cloud_positions=0, lateral=False) == ""


# --- priors: the pose evidence stays NEUTRAL ----------------------------------


def test_a_pose_is_analyzed_neutrally_while_the_anchor_is_composed():
    """§4.2's composition is per-candidate, so it is the consumer's step.

    The anchor carries the configured ``C``/``P``/polarity maps; a pose carries
    none of them, which is what leaves its retained curve as ``M``.
    """
    fakes = FakeSeams()
    c = _lateral_conductor(
        fakes,
        measurement_protection_sections_by_role={"woofer": (), "tweeter": ()},
    )
    _walk(c, through=FIRST_LATERAL_INDEX)
    by_phase = {phase: priors for phase, _pp, _r, priors, _g in fakes.analyzed}
    anchor = by_phase[PHASE_MEASURE]
    pose = by_phase[PHASE_LATERAL]
    assert anchor.configured_crossover_response_by_role is not None
    assert anchor.measurement_protection_response_by_role is not None
    assert anchor.configured_polarity_sign_by_role is not None
    for field in (
        "configured_crossover_response_by_role",
        "measurement_protection_response_by_role",
        "configured_polarity_sign_by_role",
        "candidate_required_band_hz_by_role",
        "predicted_sum",
        "alignment_delay_bounds_us",
    ):
        assert getattr(pose, field) is None, field
    # Still MEASURE-shaped in every other respect: the analyzer needs the Fc
    # and CHECK's ambient floor to grade the pose's own SNR.
    assert pose.crossover_fc_hz == anchor.crossover_fc_hz
    assert pose.ambient_report == anchor.ambient_report


def test_a_pose_replays_the_anchors_own_program_object():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)
    played = dict(fakes.played)
    assert played[PHASE_LATERAL] is played[PHASE_MEASURE]
    # …and therefore is NOT the summed sweep every cloud position plays.
    assert PHASE_LATERAL not in flow.SUMMED_SWEEP_PHASES


# --- retained evidence --------------------------------------------------------


def test_each_pose_retains_both_branches_on_the_shared_basis_with_its_identity():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c)
    poses = c.lateral_poses
    assert len(poses) == LATERAL_COUNT
    assert [p.index for p in poses] == list(
        range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1)
    )
    grid = lateral_evidence_grid_hz()
    program = c._program_for_phase(PHASE_LATERAL)
    bands = {
        s.role: (s.f1_hz, s.f2_hz)
        for s in program.segments if s.kind == KIND_SWEEP and s.role
    }
    for pose, prompt in zip(poses, flow.LATERAL_POSE_PROMPTS):
        assert pose.prompt == prompt.text
        assert pose.role == prompt.role
        assert pose.offset_cm == prompt.offset_cm
        assert pose.at_mark is (prompt.offset_cm == 0.0)
        assert pose.pose_id == f"lateral_{pose.index:02d}"
        assert {c.role for c in pose.curves} == {"woofer", "tweeter"}
        for curve in pose.curves:
            assert curve.freqs_hz.size == grid.size
            assert curve.complex_tf.size == grid.size
            # Sampled, never interpolated: every retained frequency is one the
            # analysis grid actually carried.
            assert np.all(np.isin(curve.freqs_hz, np.asarray(
                next(r.freqs_hz for r in
                     _measure_analysis(program).driver_responses
                     if r.role == curve.role)
            )))
            assert curve.band_hz == bands[curve.role]
    # §4.4: the anchor solution is held fixed, so a pose carries no second one.
    assert not {"trim_db", "delay_us", "polarity"} & set(
        LateralPose.__dataclass_fields__
    )


def test_the_retained_band_reads_the_sweep_segment_not_a_pilot():
    """``_primary_sweep_bands`` selects on ``KIND_SWEEP``, and it must.

    A v2 MEASURE program OPENS with a leading pilot pair that carries a role
    and a band, so a role-only match would take the pilot's. Today the two
    bands are equal (both are built from the same intersected ``RoleBand``), so
    this is not a live bug — which is exactly why the coupling is pinned here
    rather than assumed: the mutation below breaks it and shows which segment
    the evidence follows.
    """
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)
    program = c._program_for_phase(PHASE_LATERAL)
    pilots = [s for s in program.segments if s.kind == "pilot" and s.role]
    assert pilots, "the fixture must actually carry a leading pilot pair"
    honest = flow._primary_sweep_bands(program)
    # Today's coupling, stated rather than relied on.
    assert (pilots[0].f1_hz, pilots[0].f2_hz) == honest[pilots[0].role]
    # Break it: a pilot whose band is a narrow tone. The reader must still
    # report the SWEEP's band, because that is the band the retained curve
    # describes.
    # (A ``SimpleNamespace`` because ``ExcitationProgram`` binds ``program_id``
    # to its schedule content and refuses an edited copy — which is its own
    # guard working. The reader only ever touches ``.segments``.)
    mutated = SimpleNamespace(segments=tuple(
        replace(s, f1_hz=900.0, f2_hz=1100.0)
        if s.kind == "pilot" and s.role else s
        for s in program.segments
    ))
    assert flow._primary_sweep_bands(mutated) == honest
    for curve in c.lateral_poses[0].curves:
        assert curve.band_hz == honest[curve.role]


def test_the_anchor_solution_is_held_fixed_across_the_walk():
    """§4.4's load-bearing rule, at the level of behaviour rather than shape.

    Every pose here reports a WILDLY different alignment from the anchor's. If
    any of them were allowed to re-solve, the applied trim/delay/polarity would
    move; holding the anchor fixed is what keeps the off-axis consequence
    visible instead of absorbed.
    """
    # The reference: the same session with no walk at all, whose candidate is
    # built from the anchor alone. Re-derived, never hand-written, so this
    # cannot drift from what the fitter actually produces.
    reference = _conductor(
        FakeSeams(), index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE},
    )
    _run_phase(reference, 1, 1)
    _run_phase(reference, 2, 1)
    expected = reference.candidate
    assert expected is not None

    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX - 1)

    def elsewhere(program):
        return _measure_analysis(
            program,
            alignment=_alignment(delay_us=-900.0, polarity="inverted"),
        )

    fakes.measure = elsewhere
    for index in range(FIRST_LATERAL_INDEX, LAST_LATERAL_INDEX + 1):
        assert _run_phase(c, index, 1)["accepted"] is True
    assert len(c.lateral_poses) == LATERAL_COUNT

    walked = c.candidate
    assert walked is not None
    assert walked.alignment == expected.alignment
    assert walked.role_attenuations_db == expected.role_attenuations_db
    assert walked.fingerprint == expected.fingerprint
    assert walked.alignment.polarity != "inverted"


def test_a_retaken_pose_replaces_its_earlier_take():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)
    _run_phase(c, FIRST_LATERAL_INDEX, 2)
    poses = [p for p in c.lateral_poses if p.index == FIRST_LATERAL_INDEX]
    assert len(poses) == 1
    assert poses[0].attempt == 2


# --- honesty screens ----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"locate_confidence": 0.0}, REASON_LOCATE_FAILED),
        ({"pilot_snr_ok": False}, REASON_PILOT_LEVEL_COLLAPSE),
        ({"glitch": True}, REASON_DRIFT_BASELINES_DISAGREE),
        ({"clipped": True}, REASON_CLIPPED),
        ({"linearity": False}, REASON_AGC_BEHAVIORAL_FAIL),
    ],
)
def test_a_pose_runs_measures_own_capture_integrity_screens(kwargs, code):
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX - 1)
    fakes.measure = lambda program: _measure_analysis(program, **kwargs)
    result = _run_phase(c, FIRST_LATERAL_INDEX, 1)
    assert result["accepted"] is False
    assert result["code"] == code
    assert c.lateral_poses == ()


def test_a_pose_is_not_refused_for_an_off_axis_alignment():
    """§4.4 forbids re-solving alignment at a pose, so the three MEASURE gates
    that judge the solve must not fire here — a microphone 40 cm to the side
    legitimately blows the mark's delay-search window and GCC confidence."""
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX - 1)
    hopeless = _alignment(
        confidence=0.0, status=ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    )
    fakes.measure = lambda program: _measure_analysis(program, alignment=hopeless)
    assert _run_phase(c, FIRST_LATERAL_INDEX, 1)["accepted"] is True
    assert len(c.lateral_poses) == 1
    # The SAME analysis at the anchor is refused, so this is a pose-scoped
    # exemption rather than the gate having been deleted.
    other = _lateral_conductor(FakeSeams(measure=fakes.measure))
    _run_phase(other, 1, 1)
    anchor = _run_phase(other, 2, 1)
    assert anchor["accepted"] is False
    assert anchor["code"] == flow.REASON_DELAY_EXCEEDS_SEARCH_WINDOW


def test_a_pose_that_yielded_one_branch_is_not_evidence():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX - 1)
    fakes.measure = lambda program: replace(
        _measure_analysis(program),
        driver_responses=_measure_analysis(program).driver_responses[:1],
    )
    result = _run_phase(c, FIRST_LATERAL_INDEX, 1)
    assert result["accepted"] is False
    assert result["code"] == REASON_LOCATE_FAILED


def test_an_unmeasurable_pose_is_dropped_and_the_walk_continues():
    """§4.4: side evidence owns robustness, not the target — so its position
    floor is ZERO. A cloud below its floor ends the session; a pose does not,
    because the coefficients are the anchor's and are already in hand."""
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX - 1)
    fakes.measure = lambda program: _measure_analysis(program, locate_confidence=0.0)
    last = None
    for attempt in range(1, 2 + flow.MAX_EXTRA_ATTEMPTS_PER_POSITION):
        last = _run_phase(c, FIRST_LATERAL_INDEX, attempt)
    assert last is not None and last["accepted"] is True
    assert last["unresolved"]["index"] == FIRST_LATERAL_INDEX
    assert "terminal" not in last
    assert c._group_position_floor(PHASE_LATERAL) == 0
    assert c._group_position_floor(flow.PHASE_CLOUD_MEASURE) == (
        flow.MIN_RESOLVED_CLOUD_POSITIONS
    )
    # The rest of the walk still runs and the session still produces a
    # candidate at its close.
    fakes.measure = _measure_analysis
    for index in range(FIRST_LATERAL_INDEX + 1, LAST_LATERAL_INDEX + 1):
        closing = _run_phase(c, index, 1)
    assert closing["accepted"] is True
    assert c.candidate is not None
    assert len(c.lateral_poses) == LATERAL_COUNT - 1


# --- the walk is the last capture before the apply ----------------------------


def test_the_candidate_is_built_at_the_walks_close_not_at_the_anchor():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    results = _walk(c, through=LAST_LATERAL_INDEX - 1)
    assert c.candidate is None
    assert fakes.published_candidates == []
    assert all("candidate_fingerprint" not in r for r in results)
    closing = _run_phase(c, LAST_LATERAL_INDEX, 1)
    assert "candidate_fingerprint" in closing
    assert c.candidate is not None
    assert len(fakes.published_candidates) == 1


def test_a_dropped_final_pose_still_closes_the_walk():
    """The anchor's coefficients were never the poses' to withhold."""
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=LAST_LATERAL_INDEX - 1)
    fakes.measure = lambda program: _measure_analysis(program, locate_confidence=0.0)
    last = None
    for attempt in range(1, 2 + flow.MAX_EXTRA_ATTEMPTS_PER_POSITION):
        last = _run_phase(c, LAST_LATERAL_INDEX, attempt)
    assert last is not None and last["accepted"] is True
    assert "candidate_fingerprint" in last
    assert c.candidate is not None


def test_a_pre_r16_session_still_builds_its_candidate_at_measure():
    """No lateral group ⇒ MEASURE is still the last capture before the apply,
    byte-for-byte the pre-R16 flow."""
    fakes = FakeSeams()
    c = _conductor(fakes, index_phase_map={1: PHASE_CHECK, 2: PHASE_MEASURE})
    _run_phase(c, 1, 1)
    accepted = _run_phase(c, 2, 1)
    assert "candidate_fingerprint" in accepted
    assert c.candidate is not None


# --- the return-to-mark bracket -----------------------------------------------


def test_the_mark_return_bracket_reports_drift_and_never_invents_zero():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=LAST_LATERAL_INDEX - 1)
    # Only one at-mark pose so far, so there is no bracket to draw.
    assert c.lateral_mark_return_drift_db() is None
    _run_phase(c, LAST_LATERAL_INDEX, 1)
    drift = c.lateral_mark_return_drift_db()
    assert drift is not None
    assert set(drift) == {"woofer", "tweeter"}
    # The fixture replays an identical response, so an identical repeat reads
    # as zero drift — the number is a measurement, not a placeholder.
    assert all(value == pytest.approx(0.0, abs=1e-9) for value in drift.values())


def test_the_mark_return_bracket_measures_a_real_level_change():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=LAST_LATERAL_INDEX - 1)

    def quieter(program):
        analysis = _measure_analysis(program)
        return replace(analysis, driver_responses=tuple(
            replace(r, complex_tf=r.complex_tf * (10.0 ** (-2.0 / 20.0)))
            for r in analysis.driver_responses
        ))

    fakes.measure = quieter
    _run_phase(c, LAST_LATERAL_INDEX, 1)
    drift = c.lateral_mark_return_drift_db()
    assert drift is not None
    assert all(value == pytest.approx(2.0, abs=1e-6) for value in drift.values())


def test_the_evidence_basis_is_a_bounded_log_grid():
    grid = lateral_evidence_grid_hz()
    lo, hi = flow.LATERAL_EVIDENCE_BAND_HZ
    assert grid[0] == pytest.approx(lo)
    assert grid[-1] == pytest.approx(hi)
    ratios = grid[1:] / grid[:-1]
    assert np.allclose(ratios, ratios[0])
    # The declared density is NOMINAL: the point count is rounded to an integer
    # so the grid can land exactly on both band edges, which makes the realized
    # density differ by under one bin in a hundred. Asserted as a bound rather
    # than an equality so the test says what is actually true.
    per_octave = math.log(2.0) / math.log(ratios[0])
    nominal = flow.LATERAL_EVIDENCE_POINTS_PER_OCTAVE
    assert abs(per_octave - nominal) / nominal < 0.01
    # Bounded: the whole walk's retention is a few thousand complex values, not
    # the analysis grid's hundreds of thousands.
    assert grid.size * 2 * LATERAL_COUNT < 2000
