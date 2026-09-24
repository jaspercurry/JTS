"""R16 — the lateral-evidence producer (crossover-linearization plan §4.4).

Hardware-free. The harness is the conductor suite's own, imported rather than
re-built: two copies of a conductor factory is two definitions of a session.
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2 import pose_curve
from jasper.active_speaker.crossover_v2 import programs
from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.spatial import (
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
    POSITION_ROLE_XOVR,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_v2_cloud_index_phase_map,
)
from jasper.active_speaker.crossover_v2.pose_curve import lateral_evidence_grid_hz
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.audio_measurement import gating
from jasper.audio_measurement.program import build_verify_program
from jasper.audio_measurement.program_analysis import (
    DriverResponse,
)

from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    _conductor,
    _measure_analysis,
    _roles,
    _run_phase,
)

LATERAL_COUNT = len(capture_plan.LATERAL_POSE_PROMPTS)
FIRST_LATERAL_INDEX = 3
LAST_LATERAL_INDEX = FIRST_LATERAL_INDEX + LATERAL_COUNT - 1


def _lateral_conductor(fakes: FakeSeams, **kwargs):
    """A conductor whose stage 1 is CHECK + MEASURE + the lateral walk."""
    return _conductor(
        fakes,
        index_phase_map=build_v2_cloud_index_phase_map(
            include_lateral=True,
        ),
        **kwargs,
    )


def _walk(conductor, *, through: int = LAST_LATERAL_INDEX) -> list[dict]:
    """CHECK, MEASURE, then the lateral poses up to and including ``through``."""
    out = [_run_phase(conductor, 1, 1), _run_phase(conductor, 2, 1)]
    for index in range(FIRST_LATERAL_INDEX, through + 1):
        out.append(_run_phase(conductor, index, 1))
    return out


def test_the_walk_is_derived_from_the_cloud_table_and_bracketed_by_the_mark():
    """§4.4: "reuses the existing ±12 cm and ±40 cm left/right moves" — derived
    by PREDICATE off ``CLOUD_POSITION_PROMPTS`` so the two tables cannot state
    different distances, and bracketed by the two at-mark poses.
    """
    poses = capture_plan.LATERAL_POSE_PROMPTS
    assert poses[0] is capture_plan.LATERAL_MARK_PROMPT
    assert poses[-1] is capture_plan.LATERAL_MARK_RETURN_PROMPT
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
            assert capture_plan.format_position_distance(pose.offset_cm) in pose.headline
        else:
            assert "cm)" not in pose.headline
    # Mutation of the import-time guard: drop one 40 cm row from the cloud
    # table and the derived walk is lopsided, so the guard's count must fire.
    survivors = tuple(
        p for p in capture_plan.CLOUD_POSITION_PROMPTS
        if not (p.offset_cm == 40.0 and "LEFT" in p.headline)
    )
    derived = (capture_plan.LATERAL_MARK_PROMPT,) + tuple(
        p for p in survivors
        if p.role != POSITION_ROLE_XOVR
        and float(p.offset_cm) in capture_plan._LATERAL_POSE_OFFSETS_CM
    ) + (capture_plan.LATERAL_MARK_RETURN_PROMPT,)
    assert len(derived) != 2 * len(capture_plan._LATERAL_POSE_OFFSETS_CM) + 2


def test_a_flag_on_mid_walk_state_reaches_the_lateral_wizard_screen():
    """The third guard of the completeness claim — it fails if a SURFACE was
    missed rather than a rule broken. Driven end to end: a real conductor's
    recorded ``session_phases``, through ``crossover_v2_phase``, into the
    envelope, flag-on and mid-walk.
    """
    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
        crossover_v2_phase,
    )

    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)  # anchor done, walk under way
    session_phases = c.snapshot().session_phases
    assert PHASE_LATERAL in session_phases

    # What the durable state looks like standing at pose two of six.
    phase = crossover_v2_phase({
        "session_phases": list(session_phases),
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": False,
    }, review_declined=False)
    assert phase == PHASE_LATERAL

    env = build_crossover_envelope_v2({
        "active": True,
        "capture": {"status": "awaiting_capture"},
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


@pytest.mark.parametrize("purpose", ["speaker", "room"])
def test_inline_summed_lateral_entries_budget_the_requested_sweep(purpose):
    request = ac.AngleCaptureRequest((
        ac.AngleStop(22, ac.REGIME_SUMMED, candidate_id="trial", purpose=purpose),
    ), candidates=("trial",))
    captures = prepare_plan_captures(request, roles_bands=_roles())
    plan = capture_plan.build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        roles_bands=_roles(), fc_hz=FC_HZ, acknowledgement_binding="b" * 24,
        retries_per_pose=0,
    ).capture_plan
    (entry,) = plan.entries
    assert entry.kind_label == PHASE_LATERAL
    band = (150.0, 20000.0) if purpose == "room" else None
    assert captures[0].spec.sweep_band_hz == (band or ())
    program = build_verify_program(
        FC_HZ, measurement_band_hz=programs.measurement_band_hz(_roles()),
        sweep_band_hz=band,
    )
    assert entry.duration_ms == capture_plan._program_duration_ms(program) + capture_plan.CAPTURE_ENTRY_MARGIN_MS
    assert entry.screen[capture_plan.POSITION_DEG_KEY] == "22"


@pytest.mark.parametrize("capture_target", [2, 3, 2 + LATERAL_COUNT, 3 + LATERAL_COUNT])
def test_the_retry_budget_grows_with_lateral_entries(capture_target):
    assert capture_plan.stage1_plan_max_attempts(capture_target) == (
        capture_target + capture_plan.CLOUD_RETAKE_ALLOWANCE
    )


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


def test_each_pose_is_analyzed_under_its_own_gate_exemption():
    """The session reads the pose the operator followed: a pose at one driver
    ungated as near-field (ADR-0360), a room seat as the room, a bearing gated."""
    request = ac.AngleCaptureRequest(stops=(
        ac.AngleStop(0, ac.REGIME_NEAR_FIELD, kind="close", distance_m=0.015, purpose="reference",
                     driver="woofer:rear"),
        ac.AngleStop(0, ac.REGIME_SUMMED, kind="seat", seat_offset_m=(0.0, 0.0, 0.0), purpose="room"),
        ac.AngleStop(20, ac.REGIME_SUMMED, purpose="speaker"),
    ))
    prompts = tuple(stop.prompt for stop in ac.resolve_request(request))
    index_phase_map = build_v2_cloud_index_phase_map(include_lateral=True, lateral_prompts=prompts)
    c = _conductor(FakeSeams(), index_phase_map=index_phase_map, lateral_prompts=prompts,
                   lateral_consumer=journey.LATERAL_CONSUMER_FORWARD_MODEL)

    assert [c.capture_geometry(PHASE_LATERAL, index).gate_exempt_reason
            for index, phase in sorted(index_phase_map.items()) if phase == PHASE_LATERAL] == [
        gating.NEAR_FIELD_EXEMPT, gating.SEAT_EXEMPT, None]


def test_a_pose_replays_the_anchors_own_program_object():
    fakes = FakeSeams()
    c = _lateral_conductor(fakes)
    _walk(c, through=FIRST_LATERAL_INDEX)
    assert c.program_for_phase(PHASE_LATERAL) is c.program_for_phase(PHASE_MEASURE)
    # …and therefore is NOT the summed sweep every cloud position plays.
    assert PHASE_LATERAL not in programs.SUMMED_SWEEP_PHASES


# ``lateral_pose_curve`` indexes its input's own frequency axis
# (``freqs[left]`` after a ``searchsorted``/``clip``), so a degenerate response
# with an EMPTY axis is an ``IndexError`` rather than a zero-length curve —
# measured, not inferred: ``index -2 is out of bounds for axis 0 with size 0``.


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
    """Building a curve from an empty-axis response raises rather than
    returning a zero-length curve."""
    with pytest.raises(IndexError):
        pose_curve.lateral_pose_curve(_empty_axis_response("woofer"), (100.0, 20000.0))


def test_the_evidence_basis_is_a_bounded_log_grid():
    grid = lateral_evidence_grid_hz()
    lo, hi = pose_curve.LATERAL_EVIDENCE_BAND_HZ
    assert grid[0] == pytest.approx(lo)
    assert grid[-1] == pytest.approx(hi)
    ratios = grid[1:] / grid[:-1]
    assert np.allclose(ratios, ratios[0])
    # The declared density is NOMINAL — the point count rounds to an integer so
    # the grid lands exactly on both band edges — so this is a bound, not an
    # equality, because that is what is actually true.
    per_octave = math.log(2.0) / math.log(ratios[0])
    nominal = pose_curve.LATERAL_EVIDENCE_POINTS_PER_OCTAVE
    assert abs(per_octave - nominal) / nominal < 0.01
    # Bounded: a few thousand complex values, not the analysis grid's hundreds
    # of thousands.
    assert grid.size * 2 * LATERAL_COUNT < 2000


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
    )


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
    with pytest.raises(contracts.CrossoverV2FlowError, match=fragment):
        _lateral_conductor(FakeSeams(), **kwargs)
