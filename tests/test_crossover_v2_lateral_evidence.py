"""R16 — the lateral-evidence producer (crossover-linearization plan §4.4).

Hardware-free. The harness is the conductor suite's own, imported rather than
re-built: two copies of a conductor factory is two definitions of a session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_AGC_BEHAVIORAL_FAIL,
    REASON_CLIPPED,
    REASON_DRIFT_BASELINES_DISAGREE,
    REASON_LOCATE_FAILED,
    REASON_PILOT_LEVEL_COLLAPSE,
)
from jasper.active_speaker.crossover_v2_flow import (
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
    POSITION_ROLE_XOVR,
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
    bank_into,
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


# --- the shipped stage-1 shape --------------------------------------------------


def _stage1(**flags):
    """The index map, plan and spec one set of stage-1 flags produces."""
    return (
        build_v2_cloud_index_phase_map(tier="full", **flags),
        build_v2_capture_plan(_roles(), FC_HZ, tier="full", **flags),
        build_v2_session_spec(
            _roles(), FC_HZ, acknowledgement_binding="b" * 24, tier="full",
            **flags,
        ),
    )


def _shipped_flags():
    return dict(
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )


def test_stage_1_is_the_pinned_three_capture_shape():
    """A household is not walked: stage 1 is the anchor pair plus #2291's one
    held-still capture at the mark.

    The shape is written out INDEPENDENTLY of the flags, so a build that flipped
    a flag and also changed the shape some other way still fails.

    Why no walk, in one line: over the 8 banked rounds it was 59.4% of all
    session audio, never changed an outcome, and fed a statistic whose
    rank-1-to-rank-2 gaps (0.004–2.13 dB) sit under its own 3.54 dB repeat
    noise. It was paused on 2026-08-18 and retired with the corner hunt it fed;
    :func:`test_a_walk_still_builds_r17s_shape_byte_for_byte` is the executable
    half of the promise that the walk MACHINERY an operator's staged angle walk
    runs is untouched.
    """
    assert flow.STAGE1_INCLUDES_ENTRY_BASELINE is True

    index_phase, plan, spec = _stage1(**_shipped_flags())

    # Stage 1 stated in full: the anchor pair, then #2291's entry baseline. The
    # pre-apply cloud stays off on its own separate flag, as it has since R15.
    assert index_phase == {
        1: PHASE_CHECK, 2: PHASE_MEASURE, 3: flow.PHASE_ENTRY_BASELINE,
    }
    assert [e.kind_label for e in plan.entries] == [
        "check", "measure", "entry_baseline",
    ]
    assert plan.capture_target == 3
    assert plan.max_attempts == 3 + flow.CLOUD_RETAKE_ALLOWANCE
    assert flow.stage1_base_entries(resolve_plan_shape("full")) == 3

    # No pose reaches the wire — this plan is what the phone renders.
    raw = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
    assert b"lateral" not in raw
    assert b"entry_baseline" in raw
    # RE-DERIVED 2026-08-18 (session trims): the courtesy prelude now rides
    # only the capture that OPENS a session, so MEASURE budgets 3600 ms less
    # while CHECK and the entry baseline are untouched. The byte LENGTH is
    # unchanged (same digit count), which is why the digest is the part that
    # moved.
    assert (len(raw), hashlib.sha256(raw).hexdigest()) == (
        1107, "d2f5c5a40bc2466dd3660c7ec783dff6bd3c05109fbd246dc7daf7978c5d2b77",
    ), "the shipped stage-1 plan's wire bytes moved"

    # …and the consent screen no longer tells the household they will be
    # walked. Its walk note is the one R17 added; with the poses gone the
    # screen must not still promise a move nobody will be asked to make.
    notes = [c["text"] for c in spec.screen if c["type"] == "note"]
    assert not any("of the mark" in note for note in notes)


def test_a_walk_still_builds_r17s_shape_byte_for_byte():
    """The MACHINERY-not-DELETED promise, executable.

    Every piece of the walk stays in the tree for an operator's staged angle
    walk, so asking the builders for one must reproduce R17's shipped stage 1
    EXACTLY — same phases, same entry labels, same capture target, and the same
    wire bytes the phone rendered (2,692 / ``c5cfa51f…``, carried over verbatim
    from the pin this file kept while stage 1 shipped the walk on). A refactor
    that quietly ate a prompt, a screen or a plan entry fails here rather than
    on the first operator who stages a walk.

    ``include_lateral=True`` is asked of the builders directly, which is what
    ``prepare_v2_session`` itself does once it has taken a staged walk.
    """
    poses = len(flow.LATERAL_POSE_PROMPTS)
    index_phase, plan, spec = _stage1(
        **{**_shipped_flags(), "include_lateral": True}
    )

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

    raw = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
    # RE-DERIVED 2026-08-18 (session trims), same cause and same byte length as
    # the shipped shape above: MEASURE and every pose that replays it budget one
    # prelude less. R17's plan SHAPE is what this pins — entry count, order and
    # copy — and none of that moved.
    assert (len(raw), hashlib.sha256(raw).hexdigest()) == (
        2692, "c5cfa51f34c770aa83b9907c6a66b6d75a006b8f84a0f58eb888400739b76da2",
    ), "a walk no longer reproduces the plan R17 shipped"

    # The consent copy comes back with it — the household is told they will be
    # moved, which is the whole reason that note exists.
    notes = [c["text"] for c in spec.screen if c["type"] == "note"]
    assert any("of the mark" in note for note in notes)


def test_a_session_with_no_lateral_group_still_folds_the_candidate_into_measure():
    """A session built WITHOUT the walk keeps MEASURE as the last capture
    before the apply.

    ``include_lateral=False`` is written out rather than read off a flag.
    Pinning the SHAPE rather than the flag is what kept this test answering the
    same question through every state of the stage-1 arming it outlived.
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
    from jasper.web.correction_crossover_v2_status import _phase_from_state

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
            flow.cloud_walk_shape(flow.CLOUD_POSITION_PROMPTS[:8]))
    assert flow.walk_shape_for(cloud_positions=0, lateral=False) == ""


# --- priors: the pose evidence stays NEUTRAL ----------------------------------


def test_a_pose_is_analyzed_neutrally_while_the_anchor_is_composed():
    """§4.2's composition is per-candidate, so it is the consumer's step.

    The anchor carries the configured ``C``/``P``/polarity maps; a pose carries
    none of them, which is what leaves its retained curve as ``M``.
    """
    fakes = FakeSeams()
    # A protected-neutral session's anchor really is composed, and MEASURE now
    # builds its candidate right there — so the fake has to say so or the fitter
    # refuses the capture before a pose is ever analyzed.
    fakes.measure = lambda program: replace(
        _measure_analysis(program), configured_path_composed=True,
    )
    c = _lateral_conductor(
        fakes,
        measurement_protection_sections_by_role={"woofer": (), "tweeter": ()},
    )
    _walk(c, through=FIRST_LATERAL_INDEX)
    # FIRST call per phase, not last: a phase may analyze its capture more than
    # once under one phase name, so a last-wins read could compare a pose
    # against something other than the anchor's priors.
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
    assert anchor["code"] == refusal_copy.REASON_DELAY_EXCEEDS_SEARCH_WINDOW


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
        spatial.MIN_RESOLVED_CLOUD_POSITIONS
    )
    # The rest of the walk still runs and the session still produces a
    # candidate at its close.
    fakes.measure = _measure_analysis
    for index in range(FIRST_LATERAL_INDEX + 1, LAST_LATERAL_INDEX + 1):
        closing = _run_phase(c, index, 1)
    assert closing["accepted"] is True
    assert c.candidate is not None
    assert len(c.lateral_poses) == LATERAL_COUNT - 1


# --- the candidate is built at the anchor -------------------------------------


def test_the_candidate_is_built_at_the_anchor_even_under_a_walk():
    """MEASURE is the last capture the proposal depends on, walk or no walk.

    The walk deferred the build while its close adjudicated a corner; the poses
    are evidence for the offline forward model now, and their close publishes
    nothing, so deferring would only age the proposal the household reviews.
    """
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    anchor = _walk(c, through=FIRST_LATERAL_INDEX - 1)[-1]
    assert "candidate_fingerprint" in anchor
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


# --- the consumer: who a lateral group is FOR (#2732 P2) ----------------------
#
# Every test below drives the SAME shipped per-driver-at-a-pose machinery and
# differs only in which pose table the walk runs, which is the whole claim: an
# operator's staged walk is not a second capture path, it is the same path over
# the poses the operator stated.


def _angle_prompts(angles=(0, 7, -7, 22, -22)):
    """The poses an operator's staged walk composes to, through the seam."""
    return ac.session_lateral_walk(
        ac.per_driver_at(list(angles)),
        externally_positioned=False,
        base_entries=3,
        plans_cloud_group=False,
    )


def _evidence_conductor(fakes: FakeSeams, *, prompts=None, **kwargs):
    """A conductor whose lateral group is EVIDENCE for the offline P2 model."""
    prompts = _angle_prompts() if prompts is None else prompts
    return _conductor(
        fakes,
        index_phase_map=build_v2_cloud_index_phase_map(
            tier="full", include_cloud_measure=False, include_lateral=True,
            lateral_prompts=prompts,
        ),
        lateral_consumer=journey.LATERAL_CONSUMER_FORWARD_MODEL,
        lateral_prompts=prompts,
        **kwargs,
    )


def _evidence_walk(conductor, prompts) -> list[dict]:
    out = [_run_phase(conductor, 1, 1), _run_phase(conductor, 2, 1)]
    for offset in range(len(prompts)):
        out.append(_run_phase(conductor, FIRST_LATERAL_INDEX + offset, 1))
    return out


def test_an_evidence_walk_reaches_its_last_pose_and_publishes_nothing():
    """The walk's last accepted pose is the ONE place ``_close_lateral_walk``
    runs for an accepted capture, and that close publishes nothing: the poses
    are evidence an offline model reads off the banked round.
    """
    prompts = _angle_prompts()
    fakes = FakeSeams()
    c = _evidence_conductor(fakes, prompts=prompts)
    verdicts = _evidence_walk(c, prompts)

    assert verdicts[-1]["accepted"] is True
    assert len(c.lateral_poses) == len(prompts)
    # No candidate was published AT the close — MEASURE already published one,
    # and the close added nothing to its verdict.
    assert "candidate_fingerprint" not in verdicts[-1]
    # No selector surface at all since ticket 2.4 — not a surface that answers
    # "nothing to recommend", which would be a live comparator declining.
    assert not hasattr(c, "fc_selection")


@pytest.mark.parametrize(
    "angles, bracketed",
    [((0, 0, 20), False), ((0, 20, 0), True)],
    ids=["repeats-then-off-axis", "off-axis-then-back"],
)
def test_the_mark_return_bracket_needs_a_pose_taken_after_the_mic_moved(
    angles, bracketed
):
    """Adjacent at-mark repeats are repeat noise, not return drift.

    Both baseline tiers open with their anchor repeats and never come back, so
    a bracket drawn from the first and last at-mark pose would publish
    take-to-take spread as if the household had nudged something.
    """
    prompts = _angle_prompts(angles)
    fakes = FakeSeams()
    c = _evidence_conductor(fakes, prompts=prompts)
    _evidence_walk(c, prompts)

    assert [pose.at_mark for pose in c.lateral_poses] == [a == 0 for a in angles]
    assert (c.lateral_mark_return_drift_db() is not None) is bracketed


def test_a_settled_last_pose_closes_the_walk_too():
    """The OTHER route into the close, pinned independently.

    A last pose that is SETTLED rather than accepted (its slot spent) reaches
    ``_close_lateral_walk`` through ``_settled_group_verdict``, so a walk whose
    final capture could not be measured still ends. It is a different route, so
    a pin at the accepted path alone would leave it uncovered.
    """
    prompts = _angle_prompts()
    fakes = FakeSeams()
    c = _evidence_conductor(fakes, prompts=prompts)
    last = FIRST_LATERAL_INDEX + len(prompts) - 1
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    with c._close_lock:
        verdict = c._settled_group_verdict(PHASE_LATERAL, last, {"left_out": True})

    # Still "accepted" on the wire — the capture's only "move on" signal.
    assert verdict.accepted is True
    assert verdict.payload == {"left_out": True}


def test_measure_publishes_immediately_under_a_walk():
    """The deferral's stated reason was "the walk is the fit's input", and that
    is false of a walk feeding an offline model. So MEASURE takes the no-walk
    branch and publishes right there — otherwise the household would wait on a
    candidate the close never publishes.
    """
    prompts = _angle_prompts()
    fakes = FakeSeams()
    c = _evidence_conductor(fakes, prompts=prompts)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)

    assert c.candidate is not None
    assert fakes.published_candidates, "MEASURE must publish its own candidate"
    # ...and it consumed the analysis rather than holding it for a close that
    # reads nothing (the deferring branch's tens-of-megabytes retention).
    assert c._measure_analysis is None
    # The ratified table's walk answers the same way — the fit timing is not a
    # property of which poses the walk runs.
    ratified = _lateral_conductor(FakeSeams())
    _run_phase(ratified, 1, 1)
    _run_phase(ratified, 2, 1)
    assert ratified.candidate is not None
    assert ratified._measure_analysis is None


def test_an_evidence_pose_banks_the_stated_prompt_not_the_ratified_table():
    """A pose's prompt is the only durable statement of WHERE it was measured.

    The walk this session was handed is the operator's, so every banked pose
    must carry that walk's copy and geometry — reading the module's ratified
    table here would record five poses at spots the microphone never visited.
    """
    prompts = _angle_prompts()
    fakes = FakeSeams()
    c = _evidence_conductor(fakes, prompts=prompts)
    _evidence_walk(c, prompts)

    assert [p.prompt for p in c.lateral_poses] == [p.text for p in prompts]
    assert [round(p.offset_cm, 1) for p in c.lateral_poses] == [
        round(p.offset_cm, 1) for p in prompts
    ]
    # The ratified table is a DIFFERENT walk, so this cannot pass by accident.
    assert [p.text for p in prompts] != [
        p.text for p in flow.LATERAL_POSE_PROMPTS[: len(prompts)]
    ]


def test_an_accepted_pose_is_retained_with_its_angle():
    """The durable shape the offline forward model rebuilds a plant from.

    Before this, a lateral pose reached the evidence bundle not at all: the
    curves lived in memory and the WAV was dropped. What an offline consumer
    needs is the bytes AND the bearing they were taken at.
    """
    prompts = _angle_prompts()
    retained: list = []
    fakes = FakeSeams()
    c = _evidence_conductor(
        fakes,
        prompts=prompts,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(
                retained, with_capture=True, phase=PHASE_LATERAL,
            ),
        ),
    )
    _evidence_walk(c, prompts)

    banked = [meta for _r, meta in retained]
    assert [m["pose_id"] for m in banked] == [
        f"{PHASE_LATERAL}_{FIRST_LATERAL_INDEX + i:02d}"
        for i in range(len(prompts))
    ]
    # The RAW capture crosses the seam, not a derived curve — a replay needs
    # the bytes.
    assert all(getattr(result, "wav", None) for result, _m in retained)
    assert [m["position_deg"] for m in banked] == [0, 7, -7, 22, -22]
    assert [round(m["offset_cm"], 1) for m in banked] == [
        round(p.offset_cm, 1) for p in prompts
    ]
    assert [m["at_mark"] for m in banked] == [True, False, False, False, False]
    assert {m["regime"] for m in banked} == {ac.REGIME_PER_DRIVER}
    assert {m["lateral_consumer"] for m in banked} == {
        journey.LATERAL_CONSUMER_FORWARD_MODEL
    }
    # ...and the identity/verifier fields a cloud position already carries, so
    # one replay path covers both kinds of retained take.
    first = banked[0]
    assert first["session_id"] == c.session_id
    assert first["phase"] == PHASE_LATERAL
    assert first["take_id"] == f"{first['pose_id']}_a{first['attempt']:02d}"
    assert first["prompt"] == prompts[0].text
    assert first["wav_sha256"] == hashlib.sha256(b"fake-wav").hexdigest()


def test_an_accepted_pose_publishes_its_id_under_the_POSE_key():
    """``pose_id`` is the canonical per-pose key on every surface, the verdict
    payload included.

    ``position_id`` / ``position_index`` are the WALK's keys — which slot of a
    walk this is, assigned by whatever drives it — and they answer a different
    question from "which pose was measured". Publishing a pose id under the
    position key hands a reader the right string for the wrong question, and it
    reads as correct because the two strings coincide today.
    """
    prompts = _angle_prompts()
    c = _evidence_conductor(FakeSeams(), prompts=prompts)
    verdicts = _evidence_walk(c, prompts)

    lateral = verdicts[2:]
    assert [v["pose_id"] for v in lateral] == [p.pose_id for p in c.lateral_poses]
    assert not any("position_id" in v for v in lateral)


def test_a_raised_pose_banks_the_elevation_the_operator_was_SENT_to():
    """Both bearings come off the prompt that was actually shown.

    The sidecar is the only durable statement of where a curve was measured,
    and a raised pose recorded at mark height would place the microphone
    somewhere it never was. The axis word stays ``horizontal`` because the pose
    still commands a bearing — it is COMPOUND, not vertical.

    ``at_mark`` answers the same question on both axes at once: a pose raised
    over the mark with no bearing at all is not AT the mark, and one that says
    it is brackets the walk's drift with a pose the operator was standing up
    for.
    """
    prompts = ac.session_lateral_walk(
        ac.AngleCaptureRequest(
            stops=(
                ac.AngleStop(0, ac.REGIME_PER_DRIVER, 0),
                ac.AngleStop(0, ac.REGIME_PER_DRIVER, 10),
                ac.AngleStop(22, ac.REGIME_PER_DRIVER, 20),
                ac.AngleStop(-22, ac.REGIME_PER_DRIVER, -20),
            ),
            mover=ac.MOVER_HUMAN,
        ),
        externally_positioned=False, base_entries=3, plans_cloud_group=False,
    )
    retained: list = []
    fakes = FakeSeams()
    c = _evidence_conductor(
        fakes,
        prompts=prompts,
        seams=replace(
            fakes.seams(),
            bank_take=bank_into(
                retained, with_capture=True, phase=PHASE_LATERAL,
            ),
        ),
    )
    _evidence_walk(c, prompts)

    banked = [meta for _r, meta in retained]
    assert [m["vertical_deg"] for m in banked] == [0, 10, 20, -20]
    assert [m["position_deg"] for m in banked] == [0, 0, 22, -22]
    assert [m["at_mark"] for m in banked] == [True, False, False, False]
    assert {m["position_axis"] for m in banked} == {
        spatial.POSITION_AXIS_HORIZONTAL
    }


def test_a_closed_walk_says_so_by_name(caplog):
    """A walk that finished has to be a POSITIVE journal statement.

    The absence of the line is indistinguishable from a walk that never
    finished, which is the reading an operator would most like to be wrong
    about. ONE event covers every walk — the close publishes nothing whichever
    pose table ran, so a second "suppressed" name would only invite a reader to
    look for a suppression that has no alternative.
    """
    prompts = _angle_prompts()
    fakes = FakeSeams()
    c = _evidence_conductor(fakes, prompts=prompts)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 1)
    with caplog.at_level(logging.INFO):
        for offset in range(len(prompts)):
            _run_phase(c, FIRST_LATERAL_INDEX + offset, 1)

    line = next(
        rec.getMessage() for rec in caplog.records
        if "crossover_v2_lateral_walk_closed" in rec.getMessage()
    )
    assert f"consumer={journey.LATERAL_CONSUMER_FORWARD_MODEL}" in line
    assert f"planned={len(prompts)}" in line and f"captured={len(prompts)}" in line


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        ({"lateral_consumer": "whoever"}, "must be one of"),
        (
            {"lateral_prompts": _angle_prompts()},
            "states its own poses",
        ),
        (
            {"lateral_consumer": journey.LATERAL_CONSUMER_FORWARD_MODEL},
            "states its own poses",
        ),
    ],
    ids=["unknown-consumer", "table-on-the-selector", "evidence-with-no-table"],
)
def test_a_session_refuses_an_incoherent_lateral_declaration(kwargs, fragment):
    """Fail-closed at construction, because what a mistake reaches is a walk
    banked at poses the microphone never visited. The refusal is the flow's own
    error, so a caller that already handles session construction handles this."""
    with pytest.raises(flow.CrossoverV2FlowError, match=fragment):
        _lateral_conductor(FakeSeams(), **kwargs)
