"""R16 — the lateral-evidence producer (crossover-linearization plan §4.4).

Hardware-free. The harness is the conductor suite's own, imported rather than
re-built: two copies of a conductor factory is two definitions of a session.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
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
    DriverResponse,
)

from tests.crossover_v2_fixtures import (
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
    """§4.4: "reuses the existing ±12 cm and ±40 cm left/right moves" — derived
    by PREDICATE off ``CLOUD_POSITION_PROMPTS`` so the two tables cannot state
    different distances, and bracketed by the two at-mark poses.
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
    # Mutation of the import-time guard: drop one 40 cm row from the cloud
    # table and the derived walk is lopsided, so the guard's count must fire.
    survivors = tuple(
        p for p in flow.CLOUD_POSITION_PROMPTS
        if not (p.offset_cm == 40.0 and "LEFT" in p.headline)
    )
    derived = (flow.LATERAL_MARK_PROMPT,) + tuple(
        p for p in survivors
        if p.role != POSITION_ROLE_XOVR
        and float(p.offset_cm) in flow._LATERAL_POSE_OFFSETS_CM
    ) + (flow.LATERAL_MARK_RETURN_PROMPT,)
    assert len(derived) != 2 * len(flow._LATERAL_POSE_OFFSETS_CM) + 2


# --- the shipped stage-1 shape, now that R17 has flipped the walk on ----------


def test_the_walk_is_on_and_stage_1_is_the_pinned_six_pose_shape():
    """R17 flipped ``STAGE1_INCLUDES_LATERAL``, so a household now walks the
    six poses after the anchor; #2291 added one held-still capture at the mark
    after them. Same guard as the dormancy pin it replaces, one state over: the
    shape is written out INDEPENDENTLY of the flags, so a build that flipped a
    flag and also changed the shape some other way still fails. (The retired
    assertion was ``… is False`` over a 794-byte plan.)
    """
    assert flow.STAGE1_INCLUDES_LATERAL is True
    assert flow.STAGE1_INCLUDES_ENTRY_BASELINE is True

    index_phase = build_v2_cloud_index_phase_map(
        tier="full",
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    plan = build_v2_capture_plan(
        _roles(), FC_HZ, tier="full",
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )

    # Stage 1 stated in full: the anchor pair, one entry per prompted pose, and
    # #2291's entry baseline LAST — the capture immediately before apply, which
    # is the whole reason it is at the end rather than anywhere else. The
    # pre-apply cloud stays off — a separate flag, and R17 did not authorize it.
    poses = len(flow.LATERAL_POSE_PROMPTS)
    assert index_phase == (
        {1: PHASE_CHECK, 2: PHASE_MEASURE}
        | {index: PHASE_LATERAL for index in range(3, 3 + poses)}
        | {3 + poses: flow.PHASE_ENTRY_BASELINE}
    )
    assert [e.kind_label for e in plan.entries] == [
        "check", "measure", *["lateral"] * poses, "entry_baseline",
    ]
    assert plan.capture_target == 3 + poses
    assert plan.max_attempts == 3 + poses + flow.CLOUD_RETAKE_ALLOWANCE
    assert flow._stage1_capture_target(resolve_plan_shape("full")) == 3 + poses

    # The walk reaches the wire, and its bytes are pinned exactly as the
    # dormant shape's were — this plan is what the phone renders.
    raw = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
    assert b"lateral" in raw
    assert b"entry_baseline" in raw
    assert (len(raw), hashlib.sha256(raw).hexdigest()) == (
        2692, "7737d2b3cecd04bae6c43aa40f872da0617d86319a0544d431558df8ce9c8940",
    ), "the shipped stage-1 plan's wire bytes moved"

    # …and the consent screen now tells the household they will be walked,
    # rather than promising a stationary microphone.
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24, tier="full",
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    notes = [c["text"] for c in spec.screen if c["type"] == "note"]
    assert any("of the mark" in note for note in notes)


def test_a_session_with_no_lateral_group_still_folds_the_candidate_into_measure():
    """The fit-timing deferral keys off ``PHASE_LATERAL`` being in the journey's
    walk, so a session built WITHOUT the walk keeps MEASURE as the last capture
    before the apply — and R17's sweep, which fires on the same condition, does
    not run.

    ``include_lateral=False`` is written out rather than read off
    ``STAGE1_INCLUDES_LATERAL``: this pins the no-walk SHAPE, which stays a
    real shape (Express, the verify re-arm) now that R17 has flipped the
    stage-1 flag on. Reading the flag would have made it silently retest the
    walk instead.
    """
    fakes = FakeSeams()
    c = _conductor(
        fakes,
        index_phase_map=build_v2_cloud_index_phase_map(
            tier="full",
            include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
            include_lateral=False,
        ),
    )
    assert PHASE_LATERAL not in c.session_phases
    _run_phase(c, 1, 1)
    accepted = _run_phase(c, 2, 1)
    assert "candidate_fingerprint" in accepted
    assert c.candidate is not None
    assert c.lateral_poses == ()
    assert c.lateral_mark_return_drift_db() is None


def test_a_flag_on_mid_walk_state_reaches_the_lateral_wizard_screen():
    """The third guard of the completeness claim — it fails if a SURFACE was
    missed rather than a rule broken. Driven end to end: a real conductor's
    recorded ``session_phases``, through ``_phase_from_state``, into the
    envelope, flag-on and mid-walk.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.web.correction_crossover_v2 import _phase_from_state

    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)  # anchor done, walk under way
    session_phases = c.snapshot().session_phases
    assert PHASE_LATERAL in session_phases

    # What the durable state looks like standing at pose two of six.
    phase = _phase_from_state({
        "session_phases": list(session_phases),
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
    })
    assert phase == PHASE_LATERAL

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {"phase": phase},
    })
    assert env["screen"] == "measure"
    verdict = env["verdict_text"]
    # Movement-appropriate: MEASURE's keep-still instruction would be wrong
    # while the household walks poses, and the cloud's explanation is about a
    # different question (telling the speaker apart from the room).
    assert "still" not in verdict.lower()
    assert "apart from the room" not in verdict.lower()
    assert "either side of the mark" in verdict
    assert "back on it" in verdict  # the walk ends at the mark
    # The stepper is genuinely past step 1.
    steps = {s["id"]: s["status"] for s in env["steps"]}
    assert steps["microphone_check"] == "done"
    assert steps["measure"] == "active"

    # …and the REJECTION screens, where a missing ``_PHASE_STEP`` entry ACTUALLY
    # bites: they read the precomputed ``active_step``, not the phase branch's
    # own, and silent-auto-retry uses it as the SCREEN — so a glitched pose
    # would send the household back to step 1 over one bad sweep. Every code is
    # one ``_consume_lateral_pose`` can return.
    for code in (
        REASON_DRIFT_BASELINES_DISAGREE,   # silent auto-retry: screen == step
        REASON_LOCATE_FAILED, REASON_PILOT_LEVEL_COLLAPSE,
        REASON_CLIPPED, REASON_AGC_BEHAVIORAL_FAIL,
    ):
        failed = build_crossover_envelope_v2({
            "active": True, "setup": {"active": True, "status": "ready"},
            "crossover_v2": {
                "phase": phase, "failure": {"code": code, "at": time.time()},
            },
        })
        failed_steps = {s["id"]: s["status"] for s in failed["steps"]}
        assert failed_steps["measure"] == "active", code
        assert failed_steps["microphone_check"] == "done", code
        assert failed["screen"] != "microphone_check", code


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


def test_the_relay_capacity_guard_counts_the_lateral_walk_when_it_is_on():
    """The guard reads the SAME flag-aware producer ``jasper-doctor`` does, so
    the two can never disagree. ``ceiling`` is re-derived to sit strictly
    between the cloud-only and cloud-plus-walk cases, so a guard that stopped
    counting the poses would pass there — and, walk off, must still pass.
    """
    import jasper.capture_relay.spec as spec

    flow.assert_cloud_plan_fits_relay_capacity()  # holds today, walk ON
    worst = dict(
        cloud_measure_positions=flow.MAX_CLOUD_MEASURE_POSITIONS,
        cloud_verify_positions=flow.DEFAULT_CLOUD_VERIFY_POSITIONS,
    )
    with pytest.MonkeyPatch.context() as mp:
        # The walk-off baseline is now the patched state, not the shipped one
        # (R17 flipped the flag), so it is established under the patch.
        mp.setattr(flow, "STAGE1_INCLUDES_LATERAL", False)
        cloud_only = flow.relay_plan_attempts_required(**worst)
    ceiling = cloud_only + 1
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(flow, "STAGE1_INCLUDES_LATERAL", True)
        with_walk = flow.relay_plan_attempts_required(**worst)
        assert with_walk == cloud_only + LATERAL_COUNT
        assert with_walk <= spec.MAX_CAPTURE_PLAN_ATTEMPTS, (
            "the shipped relay ceiling no longer carries the worst-case plan"
        )
        assert ceiling < with_walk, "the walk must be long enough to be countable"
        mp.setattr(spec, "MAX_CAPTURE_PLAN_ATTEMPTS", ceiling)
        with pytest.raises(flow.CrossoverV2FlowError):
            flow.assert_cloud_plan_fits_relay_capacity()
    with pytest.MonkeyPatch.context() as mp:
        # The control: at the SAME reduced ceiling, a build whose walk is off
        # must still pass — otherwise the failure above would prove only that
        # the ceiling is small, not that the poses are counted. The flag is
        # patched off explicitly because R17 ships it on.
        mp.setattr(flow, "STAGE1_INCLUDES_LATERAL", False)
        mp.setattr(spec, "MAX_CAPTURE_PLAN_ATTEMPTS", ceiling)
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
    # The reach the sentence quotes is the FURTHEST group the session runs: the
    # walk's own 50 cm alone, the cloud's unchanged when it runs, and nothing
    # at all when neither does.
    assert flow.format_position_distance(50.0) in flow.walk_shape_for(
        cloud_positions=0, lateral=True)
    for lateral in (False, True):
        assert flow.walk_shape_for(cloud_positions=9, lateral=lateral) == (
            flow.cloud_walk_shape(9))
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
    # FIRST call per phase, not last: R17's candidate sweep re-analyzes this
    # same capture once per proposable Fc, all of them under ``PHASE_MEASURE``,
    # so a last-wins read would compare a pose against a CANDIDATE's priors
    # instead of the anchor's.
    by_phase: dict[str, object] = {}
    for phase, _pp, _r, priors, _g in fakes.analyzed:
        by_phase.setdefault(phase, priors)
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
        # A pose commits no alignment, so it is never told the one the speaker
        # already plays (#2617) — the same withholding, one field over.
        "applied_alignment",
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
    program = c.program_for_phase(PHASE_LATERAL)
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
            # Sampled, never interpolated: every retained frequency is real.
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
    # Idempotent per index: a retake REPLACES its earlier take.
    _run_phase(c, FIRST_LATERAL_INDEX, 2)
    retaken = [p for p in c.lateral_poses if p.index == FIRST_LATERAL_INDEX]
    assert len(retaken) == 1 and retaken[0].attempt == 2


def test_the_retained_band_reads_the_sweep_segment_not_a_pilot():
    """A v2 MEASURE program OPENS with a leading pilot pair carrying a role and
    a band, so a role-only match would take the pilot's. Today the two bands are
    equal (same intersected ``RoleBand``), so this is not a live bug — which is
    why the coupling is pinned rather than assumed: the mutation below breaks it
    and shows which segment the evidence follows.
    """
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)
    program = c.program_for_phase(PHASE_LATERAL)
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
    """§4.4's load-bearing rule as BEHAVIOUR, not shape. Every pose here reports
    a wildly different alignment; if any were allowed to re-solve, the applied
    trim/delay/polarity would move.
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


# --------------------------------------------------------------------------- #
# the screens run BEFORE the curves are built (#2291 Phase 5a-iv)
#
# ``_consume_lateral_pose`` screens first and only then resamples each driver
# response onto the shared basis. That order is not a style choice:
# ``lateral_pose_curve`` indexes its input's own frequency axis
# (``freqs[left]`` after a ``searchsorted``/``clip``), so a degenerate response
# with an EMPTY axis is an ``IndexError`` rather than a zero-length curve —
# measured, not inferred: ``index -2 is out of bounds for axis 0 with size 0``.
#
# Hoisting the build above the ladder — which is what folding the two-curve
# count into ``spatial.lateral_pose_screens`` would require — turns a household
# retry screen into a terminal ``internal_error`` for exactly the captures the
# ladder exists to reject.
#
# **The inversion was invisible, re-derived rather than quoted** (2026-08-11):
# applied to `_consume_lateral_pose` with the three tests below DESELECTED, the
# 16 crossover-reaching suites came back 891 passed / 11 skipped / 5 deselected,
# exit 0. Two independent reviewers reached the same conclusion from a smaller
# set. With the tests below in place the same inversion fails 3.
# --------------------------------------------------------------------------- #


def _empty_axis_response(role: str):
    """A driver response the analyzer could emit and the resampler cannot take.

    Degenerate rather than absent: it carries a role the sweep-band map knows
    and ``repeat_index is None``, so it passes the comprehension's filter and
    reaches ``lateral_pose_curve`` — which is the only way to exercise the
    hazard. Every array is empty, which is the shape a capture that located
    nothing reduces to.
    """
    return DriverResponse(
        role=role,
        freqs_hz=np.asarray([], dtype=float),
        magnitude_db=np.asarray([], dtype=float),
        complex_tf=np.asarray([], dtype=complex),
        gating={"applied": True, "window_ms": 8.0},
        snr=None,
        validity_floor_hz=None,
    )


def test_the_resampler_really_does_raise_on_an_empty_axis():
    """The premise, asserted rather than assumed.

    The pin below is only meaningful if building a curve from this response
    genuinely raises. If ``lateral_pose_curve`` ever grows a guard of its own,
    this fails first and says so — rather than leaving the ordering test passing
    for a reason that no longer exists.
    """
    with pytest.raises(IndexError):
        flow.lateral_pose_curve(_empty_axis_response("woofer"), (100.0, 20000.0))


@pytest.mark.parametrize(
    "rung,mutate,expected_code",
    [
        # Confidence 0.2 clears LOCATE_MIN_CONFIDENCE (0.1) and fails
        # SWEEP_LOCATE_CONFIDENCE_FLOOR (0.3): a MID-ladder rung, so this proves
        # the whole ladder runs before the build rather than only that the first
        # check short-circuits.
        (
            "sweep_locate_confidence",
            lambda a: replace(
                a,
                locations=tuple(
                    replace(loc, confidence=0.2) for loc in a.locations
                ),
            ),
            REASON_LOCATE_FAILED,
        ),
        (
            "glitch",
            lambda a: replace(a, glitch_detected=True),
            REASON_DRIFT_BASELINES_DISAGREE,
        ),
        (
            "linearity",
            lambda a: replace(a, linearity_ok=False),
            REASON_AGC_BEHAVIORAL_FAIL,
        ),
    ],
)
def test_a_screened_out_pose_refuses_before_any_curve_is_built(
    rung, mutate, expected_code,
):
    """A rejected pose is refused cleanly, even when its curves cannot be built.

    The two failures are independent in production — a capture too quiet to
    locate is also a capture whose reduction can come back degenerate — so the
    household-visible difference is entirely one of ORDER: screens first is
    ``locate_failed`` and a retry; build first is an uncaught ``IndexError`` and
    a terminal internal-error screen.

    Driven through ``_run_phase`` rather than by calling the ladder, because the
    ordering under test lives in ``_consume_lateral_pose`` and not in either
    piece it sequences.
    """
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)

    def _degenerate(program):
        analysis = mutate(_measure_analysis(program))
        return replace(
            analysis,
            driver_responses=(
                _empty_axis_response("woofer"), _empty_axis_response("tweeter"),
            ),
        )

    fakes.measure = _degenerate

    verdict = _run_phase(c, FIRST_LATERAL_INDEX, 1)

    assert verdict["accepted"] is False, rung
    # Each rung keeps its OWN household code — the refusal is the ladder's
    # verdict, not a generic "something went wrong" the raise would have become.
    assert verdict["code"] == expected_code, rung
    # Nothing was retained from a pose that never became evidence.
    assert c.lateral_poses == ()


def test_the_evidence_basis_is_a_bounded_log_grid():
    grid = lateral_evidence_grid_hz()
    lo, hi = flow.LATERAL_EVIDENCE_BAND_HZ
    assert grid[0] == pytest.approx(lo)
    assert grid[-1] == pytest.approx(hi)
    ratios = grid[1:] / grid[:-1]
    assert np.allclose(ratios, ratios[0])
    # The declared density is NOMINAL — the point count rounds to an integer so
    # the grid lands exactly on both band edges — so this is a bound, not an
    # equality, because that is what is actually true.
    per_octave = math.log(2.0) / math.log(ratios[0])
    nominal = flow.LATERAL_EVIDENCE_POINTS_PER_OCTAVE
    assert abs(per_octave - nominal) / nominal < 0.01
    # Bounded: a few thousand complex values, not the analysis grid's hundreds
    # of thousands.
    assert grid.size * 2 * LATERAL_COUNT < 2000
