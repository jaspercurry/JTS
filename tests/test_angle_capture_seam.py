# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The angle-capture seam: {per-driver | summed} x {angles} x {arm | human}.

1. the angle round trip -- degrees in, degrees back out of the shipped derivation;
2. the seam's dispatch -- each stop resolves to its pose's prompt and advance policy;
3. mover parity, and the record/receipt shape the shipped consumers read;
4. the ELEVATION axis -- the same construction one plane over, its per-mover
   reach, and the one clause it adds to what a household reads;
5. the PROGRAM door -- a named table becomes a walk, in the table's order;
6. CATEGORIZED poses -- a seat or close take says where it was stated from,
   and every bearing resolves byte-identically to before they existed.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from tests.program_baseline_fixtures import banked_program_baselines  # noqa: F401

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import measurement_programs as mp
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.capture_plan import (
    POSITION_BATCH_CONFIG_KEY,
    POSITION_BATCH_SIZE_KEY,
    POSITION_BATCH_START_KEY,
)
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
from jasper.active_speaker.crossover_v2.position_gate import PositionGate
from jasper.audio_measurement import gating
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from tests.crossover_v2_banked_round import (
    bank_seat_round,
    cloud_position_record,
    pose_kind_fields,
)

_SHIPPED_ANGLES = (0, 7, -7, 22, -22)


def _stops_at(angles_deg, *, mover: str = ac.MOVER_HUMAN,
              regimes=(ac.REGIME_PER_DRIVER, ac.REGIME_SUMMED)) -> ac.AngleCaptureRequest:
    """A stop in each regime at each angle, in ``regimes`` order."""
    return ac.AngleCaptureRequest(stops=tuple(
        ac.AngleStop(mp.Pose(angle, 0), regime, purpose="speaker")
        for angle in angles_deg for regime in regimes), mover=mover)

_ROLES_BANDS = (
    RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
    RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
)


# --------------------------------------------------------------------------- #
# 1. the one new primitive: degrees round-trip through the cm-primary pose
# --------------------------------------------------------------------------- #


def test_pose_at_angle_is_the_exact_inverse_of_position_angle_deg() -> None:
    """Every whole degree the seam accepts survives the round trip.

    The prompt words the move in centimetres, exactly as a hand-walked pose
    does, and that move reads back as the bearing of the pose it carries.
    """
    for degrees in range(-ac.MAX_ANGLE_DEG, ac.MAX_ANGLE_DEG + 1):
        prompt = ac.pose_at_angle(mp.Pose(degrees, 0))
        worded = round(prompt.lateral_sign * math.degrees(
            math.atan2(prompt.offset_cm / 100.0, prompt.mark_distance_m)))
        assert (capture_plan.position_angle_deg(prompt), worded) == (degrees, degrees), degrees


def test_pose_at_angle_reproduces_the_shipped_bearings() -> None:
    """+-7 deg and +-22 deg land on the shipped walk's own offsets.

    The lateral table states 12 cm and 40 cm and `position_angle_deg` reads
    +-7 deg and +-22 deg off them; this seam asked in the opposite direction has
    to arrive at the same place, or the two vocabularies describe different
    poses. The small excess over 12.0 / 40.0 is the CHORD-VERSUS-ARC gap
    `position_angle_deg`'s own docstring names: a tape-measured lateral slide
    cuts the chord, while an angle is the constant-radius arc. Asserting the arc
    is correct here -- it is the geometry both the arm and a taut string have.
    """
    assert ac.pose_at_angle(mp.Pose(7, 0)).offset_cm == pytest.approx(12.278, abs=0.01)
    assert ac.pose_at_angle(mp.Pose(22, 0)).offset_cm == pytest.approx(40.403, abs=0.01)
    assert ac.pose_at_angle(mp.Pose(-7, 0)).lateral_sign == -1
    assert ac.pose_at_angle(mp.Pose(7, 0)).lateral_sign == 1
    assert ac.pose_at_angle(mp.Pose(0, 0)).lateral_sign == 0


def test_pose_role_derives_from_the_shipped_wide_class() -> None:
    """`role` follows WIDE_OFFSET_MIN_CM rather than a second table.

    Pinned because the shipped cloud table assigns the same way (12/25 cm onax,
    40/60 cm offax) and a divergence here would make an angle-requested pose
    answer a different question than the hand-walked pose at the same place.
    """
    inside = ac.pose_at_angle(mp.Pose(7, 0))
    outside = ac.pose_at_angle(mp.Pose(22, 0))
    assert inside.offset_cm < capture_plan.WIDE_OFFSET_MIN_CM <= outside.offset_cm
    assert inside.role == spatial.POSITION_ROLE_ONAX and not inside.wide
    assert outside.role == spatial.POSITION_ROLE_OFFAX and outside.wide


@pytest.mark.parametrize("bad", [90, -90, 81, -81, 180])
def test_pose_at_angle_refuses_an_unmeasurable_bearing(bad: int) -> None:
    """The tangent's own bound, refused loudly rather than banked absurdly."""
    with pytest.raises(contracts.CrossoverV2FlowError, match="design axis"):
        ac.pose_at_angle(mp.Pose(bad, 0))


@pytest.mark.parametrize("bad", [7.5, "7", np.float64(22.0), True])
def test_the_one_pose_record_refuses_a_non_whole_degree(bad: object) -> None:
    """Whole degrees is the resolution the placement is honest at, and every
    door takes the one pose record, so one validator judges it (ADR-0366 §1)."""
    for axis in ("azimuth_deg", "elevation_deg"):
        with pytest.raises(ValueError):
            mp.Pose(**{"azimuth_deg": 0, "elevation_deg": 0, axis: bad})


# --------------------------------------------------------------------------- #
# 2. the seam's dispatch: angle x mover
# --------------------------------------------------------------------------- #


def test_requested_angle_order_is_the_running_order() -> None:
    """The walk is the caller's order, indexed 1-based like the capture drives it."""
    stops = ac.resolve_request(_stops_at([0, 22, -7, 45], regimes=(ac.REGIME_PER_DRIVER,)))
    assert [s.prompt.pose.azimuth_deg for s in stops] == [0, 22, -7, 45]
    assert [s.index for s in stops] == [1, 2, 3, 4]


def test_arbitrary_angles_are_reachable() -> None:
    """The point of the seam: any whole-degree angle a request states."""
    stop, = ac.resolve_request(_stops_at([45], regimes=(ac.REGIME_PER_DRIVER,)))
    assert capture_plan.position_angle_deg(stop.prompt) == 45


def test_empty_and_unknown_requests_are_refused() -> None:
    with pytest.raises(contracts.CrossoverV2FlowError, match="at least one stop"):
        ac.AngleCaptureRequest(stops=())
    with pytest.raises(contracts.CrossoverV2FlowError, match="mover"):
        ac.AngleCaptureRequest(stops=(ac.AngleStop(mp.Pose(0, 0), ac.REGIME_SUMMED, purpose="speaker"),), mover="robot")
    with pytest.raises(contracts.CrossoverV2FlowError, match="regime"):
        ac.AngleStop(mp.Pose(0, 0), "sine", purpose="speaker")


# --------------------------------------------------------------------------- #
# 4. mover parity, and the shapes the shipped consumers read
# --------------------------------------------------------------------------- #


def test_mover_changes_the_advance_policy_and_nothing_else() -> None:
    """Same stops by arm and by hand: identical pose, prompt and program.

    "The remote tier is a different OPERATOR, not a different measurement",
    taken all the way: the ONLY difference between the two walks is how each
    stop begins. Everything a consumer reads -- the pose, its copy, the
    program -- is equal, so the two movers' evidence is comparable without a
    per-mover branch anywhere downstream.
    """
    angles = [0, 7, -22]
    by_hand = ac.resolve_request(_stops_at(angles, mover=ac.MOVER_HUMAN, regimes=(ac.REGIME_PER_DRIVER,)))
    by_arm = ac.resolve_request(_stops_at(angles, mover=ac.MOVER_ARM, regimes=(ac.REGIME_PER_DRIVER,)))

    for hand, arm in zip(by_hand, by_arm, strict=True):
        assert hand.regime == arm.regime
        assert hand.prompt == arm.prompt          # pose AND copy
        assert hand.screen != arm.screen          # ...only this differs
    # Stated as the whole-object claim too, so a field added to ResolvedStop
    # later cannot quietly become mover-dependent without failing here.
    assert [dataclasses.replace(s, screen={}) for s in by_hand] == [
        dataclasses.replace(s, screen={}) for s in by_arm
    ]


def test_the_string_and_protractor_combination_is_reachable() -> None:
    """A human move uses an angle prompt and waits for a tap."""
    stop, = ac.resolve_request(_stops_at([22], mover=ac.MOVER_HUMAN, regimes=(ac.REGIME_PER_DRIVER,)))
    assert "22" in stop.prompt.headline                       # degrees...
    assert stop.screen["auto_advance"] == capture_plan.AUTO_ADVANCE_TAP  # ...and a tap


def test_human_mover_taps_and_declares_no_position() -> None:
    """A human move waits for a tap and declares no position target."""
    for stop in ac.resolve_request(_stops_at([0, 22])):
        assert stop.screen == {"auto_advance": capture_plan.AUTO_ADVANCE_TAP}
        assert capture_plan.POSITION_DEG_KEY not in stop.screen


def test_arm_mover_pairs_the_countdown_with_the_position_gate() -> None:
    """An ARM's auto-advance and its target are emitted TOGETHER.

    A countdown without the gate fires into an arm still in motion. The
    converse is not a pair: a gate with no countdown is a person holding the
    tape, released by their own tap -- which is exactly the shape
    a hand-released session says apart from this one.
    """
    for stop in ac.resolve_request(_stops_at([0, -22], mover=ac.MOVER_ARM)):
        assert stop.screen["auto_advance"] == capture_plan.AUTO_ADVANCE_COUNTDOWN
        assert stop.screen["countdown_s"] == str(capture_plan.AUTO_ADVANCE_COUNTDOWN_S)
        assert stop.screen[capture_plan.POSITION_DEG_KEY] == str(stop.prompt.pose.azimuth_deg)
        assert stop.screen[capture_plan.POSITION_ROLE_KEY] == stop.prompt.role


def test_the_gate_angle_is_read_back_off_the_pose() -> None:
    """The number the gate acts on is the number the banked pose carries.

    Not copied from the request: one fact, one source. The round trip is what
    would otherwise hide a defect between them.
    """
    for stop in ac.resolve_request(_stops_at([0, 7, -7, 22, -22, 45], mover=ac.MOVER_ARM,
                                              regimes=(ac.REGIME_PER_DRIVER,))):
        assert int(stop.screen[capture_plan.POSITION_DEG_KEY]) == capture_plan.position_angle_deg(
            stop.prompt
        )


def test_a_resolved_stop_banks_in_the_shipped_record_shape() -> None:
    """A stop's pose feeds `cloud_position_record` unchanged.

    The receipt/banking contract: an angle-requested capture retains the same
    keys, the same `take_id` convention and the same derived `wide` as a
    hand-walked one, so one replay path covers both and the attribution stage
    reads them alike.
    """
    stop, = ac.resolve_request(_stops_at([22], regimes=(ac.REGIME_PER_DRIVER,)))
    record = cloud_position_record(
        position_id="angle_01", phase="measure", index=stop.index, attempt=1,
        prompt=stop.prompt.text, wide=stop.prompt.wide, role=stop.prompt.role,
        geometry=capture_plan.position_geometry(stop.prompt),
        captured_at=0.0, session_id="s", gate_window_ms=None,
        gate_floor_source=None, gate_disclosure=None, gate_moved_rms_db=None,
        gate_reflection_delay_ms=None,
        gate_entanglement_floor_hz=None,
        gate_entanglement_floor_source=gating.ENTANGLEMENT_SOURCE_UNKNOWN,
        validity_floor_hz=None,
        gating_applied=False, summed_ripple_db=None, glitch_detected=False,
        wav_sha256=None,
    )
    assert record["take_id"] == "angle_01_a01"
    assert record["wide"] is True
    assert record["role"] == spatial.POSITION_ROLE_OFFAX
    assert record["prompt"] == stop.prompt.text and record["prompt"]
    # The bearing the stop was RESOLVED at is the bearing the record banks —
    # ONE derivation off the pose, so a staged angle walk cannot bank a spot
    # that disagrees with the one it asked for.
    assert record["position_deg"] == capture_plan.position_angle_deg(stop.prompt) == 22
    assert record["position_axis"] == "horizontal"
    assert record["mark_distance_m"] == spatial.MARK_DISTANCE_M


def test_a_resolved_stop_is_actually_frozen() -> None:
    """`frozen=True` means the screen bag too, not just the fields.

    A caller holding a resolved walk must not be able to edit the angle the
    position gate is waiting for. It still compares equal to a plain dict, so
    reading it is unchanged.
    """
    stop, = ac.resolve_request(_stops_at([7], mover=ac.MOVER_ARM, regimes=(ac.REGIME_PER_DRIVER,)))
    with pytest.raises(TypeError):
        stop.screen["position_deg"] = "45"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stop.index = 45  # type: ignore[misc]
    assert stop.screen == dict(stop.screen)


def test_the_arc_removes_the_inverse_square_confound() -> None:
    """The ratified design's inverse-square argument, and its OPEN question.

    The argument: a 40 cm lateral slide off a 1 m mark puts the microphone
    107.7 cm out, ~0.64 dB of pure inverse-square level change with no
    acoustics in it. Stating a pose as an ANGLE is meant to make that confound
    structural rather than addressed in prose.

    **This docstring used to open "Every stop sits at the SAME radius", and the
    body below has always said otherwise** — it asserts
    ``radius == mark / cos(theta)``, which is 1.078 m at 22°, not constant.
    What the body pins is the TANGENT construction ``pose_at_angle`` actually
    performs; whether the physical rig swings a constant-radius arc is a
    hardware fact no test can settle, and the owner's tape measure decides it:
    `#2932 <https://github.com/jaspercurry/JTS/issues/2932>`_. The assertions
    are unchanged — only the sentence that contradicted them.
    """
    for degrees in (0, 7, -7, 22, -22, 45):
        pose = ac.pose_at_angle(mp.Pose(degrees, 0))
        radius_m = math.hypot(pose.offset_cm / 100.0, spatial.MARK_DISTANCE_M)
        chord_radius_m = math.hypot(0.40, spatial.MARK_DISTANCE_M)
        assert radius_m == pytest.approx(
            spatial.MARK_DISTANCE_M / math.cos(math.radians(abs(degrees))), abs=1e-9
        )
        # the shipped 40 cm slide is the confound this replaces
        assert chord_radius_m == pytest.approx(1.077, abs=0.001)


# --------------------------------------------------------------------------- #
# 5. the ELEVATION axis: the same construction, one plane over
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("angle_deg", [0, 22, -45])
def test_elevation_round_trips_and_leaves_the_bearing_alone(angle_deg: int) -> None:
    """The azimuth's own contract, asserted on the orthogonal axis.

    The two are INDEPENDENT numbers about one pose, so a compound pose reads
    both back: the elevation through the shipped `position_elevation_deg` and
    the bearing through `position_angle_deg`, neither disturbed by the other.
    A raised pose is not `POSITION_ROLE_XOVR` -- that role means a pose
    commanding no bearing at all, and this one commands one.
    """
    for elevation in range(-ac.MAX_ELEVATION_DEG, ac.MAX_ELEVATION_DEG + 1):
        pose = ac.pose_at_angle(mp.Pose(angle_deg, elevation))
        assert capture_plan.position_elevation_deg(pose) == elevation, elevation
        assert capture_plan.position_angle_deg(pose) == angle_deg, elevation
        assert pose.role != spatial.POSITION_ROLE_XOVR


@pytest.mark.parametrize(
    "mover, angle_deg, elevation_deg",
    [
        # The arm ROTATES about the rig's vertical axis and nothing on it
        # tilts, so ANY rise is past its reach -- including one asked at a
        # bearing it can serve.
        (ac.MOVER_ARM, 0, 1),
        (ac.MOVER_ARM, 22, -10),
        (ac.MOVER_ARM, ac.ARM_ENVELOPE_DEG + 1, 0),
        (ac.MOVER_HUMAN, 0, ac.MAX_ELEVATION_DEG + 1),
        (ac.MOVER_HUMAN, 7, -ac.MAX_ELEVATION_DEG - 1),
    ],
)
def test_a_stop_past_a_movers_reach_on_either_axis_refuses_at_staging(
    mover: str, angle_deg: int, elevation_deg: int,
) -> None:
    """Reach is per-mover AND per-axis, judged where the walk is STATED.

    Refused at statement time for the reason `MOVER_MAX_ANGLE_DEG` already
    gives about the bearing: a live session would publish a target this mover
    cannot reach and then spend its whole hold budget per stop waiting for a
    report that cannot come.
    """
    with pytest.raises(ac.LateralWalkRefused) as caught:
        ac.AngleCaptureRequest(
            stops=(ac.AngleStop(mp.Pose(angle_deg, elevation_deg), ac.REGIME_PER_DRIVER, purpose="speaker"),),
            mover=mover,
        )
    assert caught.value.reason == ac.WALK_OVER_MOVER_ENVELOPE


@pytest.mark.parametrize(
    "elevation_deg",
    [0, 7, -7, ac.MAX_ELEVATION_DEG, -ac.MAX_ELEVATION_DEG],
)
def test_a_person_may_be_asked_to_raise_within_reach(elevation_deg: int) -> None:
    """Everything inside the person's own bound stages AND resolves.

    The bound covers the plan's baseline vertical walk with margin, and the
    resolved stop's pose carries it, so the number a session gates on is the
    number the request asked for.
    """
    request = ac.AngleCaptureRequest(
        stops=(ac.AngleStop(mp.Pose(22, elevation_deg), ac.REGIME_PER_DRIVER, purpose="speaker"),),
        mover=ac.MOVER_HUMAN,
    )
    stop, = ac.resolve_request(request)

    assert capture_plan.position_elevation_deg(stop.prompt) == elevation_deg
    assert capture_plan.position_angle_deg(stop.prompt) == 22


@pytest.mark.parametrize("angle_deg", [0, 7, -22])
def test_a_pose_at_mark_height_is_the_pose_the_seam_already_shipped(
    angle_deg: int,
) -> None:
    """Elevation 0 changes NOTHING, field for field.

    Additive on the whole pose rather than only on the copy: an operator's
    horizontal walk composes exactly what it composed before this axis was
    sayable, so nothing downstream that reads a pose can tell the two apart --
    and the household is told about a rise it was never asked to make.
    """
    pose = ac.pose_at_angle(mp.Pose(angle_deg, 0))

    assert ac.pose_at_angle(mp.Pose(angle_deg, 0)) == pose
    assert (pose.vertical_sign, pose.vertical_offset_cm) == (0, 0.0)
    assert "mark height" not in pose.headline


@pytest.mark.parametrize("elevation_deg, word", [(10, "ABOVE"), (-10, "BELOW")])
def test_a_raised_pose_gains_exactly_one_elevation_clause(
    elevation_deg: int, word: str,
) -> None:
    """The composed household sentence, asserted whole.

    Prompt copy IS the externally observable behaviour here -- it is the
    instruction a person follows, and there is no structured field between
    them and it. The clause EXTENDS the shipped bearing sentence rather than
    replacing it or adding a second one: a household asked to swing and to
    rise gets one instruction, not two that could be followed in either order.

    It also states the rise as a LENGTH in the table's own both-units register,
    because the person holding the microphone has a tape measure and no
    protractor -- and it names the mark distance the conversion assumes, since
    the same angle is a different height at any other distance.
    """
    flat = ac.pose_at_angle(mp.Pose(22, 0))
    raised = ac.pose_at_angle(mp.Pose(22, elevation_deg))

    assert flat.headline == (
        "Turn the microphone to +22° (22° RIGHT of the design axis)."
    )
    assert raised.headline == (
        "Turn the microphone to +22° (22° RIGHT of the design axis), and "
        f"{abs(elevation_deg)}° {word} mark height — that is 7 in (18 cm) "
        "at the declared 1 m."
    )
    # The supporting clause is about DISTANCE and is unchanged by a rise.
    assert raised.detail == flat.detail


@pytest.mark.parametrize("elevation_deg, word", [(10, "ABOVE"), (-10, "BELOW")])
def test_a_rise_on_the_design_axis_does_not_say_LEAVE_the_microphone(
    elevation_deg: int, word: str,
) -> None:
    """A stand-still verb in front of a move instruction is a contradiction.

    The 0° bearing's shipped copy is "LEAVE the microphone on the design axis",
    which is a whole instruction on its own: do not move it. A pose that also
    asks for a rise has to state the bearing as something to HOLD instead, or
    the household is told not to move and then to move in one sentence.
    """
    assert ac.pose_at_angle(mp.Pose(0, 0)).headline == (
        "Leave the microphone on the design axis (0°)."
    )
    assert ac.pose_at_angle(mp.Pose(0, elevation_deg)).headline.startswith(
        "Keep the microphone on the design axis (0°), and "
        f"{abs(elevation_deg)}° {word} mark height"
    )


def test_a_behind_prompt_reads_differently_from_the_bearing_at_the_same_azimuth() -> None:
    """Behind and bearing share (0, 0) but are different physical places, so
    the household must not read the same instruction for both (issue #5330)."""
    bearing = ac.pose_at_angle(mp.Pose(0, 0))
    behind = ac.pose_at_angle(mp.Pose(0, 0, kind=mp.POSE_KIND_BEHIND, distance_m=0.1))

    assert behind.text != bearing.text


# --------------------------------------------------------------------------- #
# 6. the program door: a named table becomes a walk, and nothing else does
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "program",
    [
        mp.run_preset("speaker", "baseline_express"),
        mp.run_preset("speaker", "baseline_full"),
    ],
    ids=["baseline_express", "baseline_full"],
)
def test_a_program_becomes_its_own_walk_in_table_order(
    program: mp.Preset,
) -> None:
    """The table IS the walk: order, repeats, sweeps, regime and provenance.

    Asserted against the program's own derived counts rather than against
    transcribed numbers, so the table stays the single owner of the geometry
    and this test cannot drift from it.
    """
    request = ac.request_for_preset(program)

    assert len(request.stops) == program.capture_count
    assert {(s.pose.azimuth_deg, s.pose.elevation_deg) for s in request.stops} == {
        (p.azimuth_deg, p.elevation_deg) for p in program.poses
    }
    assert len({(s.pose.azimuth_deg, s.pose.elevation_deg) for s in request.stops}) == (
        program.mic_move_count
    )
    assert [stop.regime for stop in request.stops] == [program.regime for pose in program.poses for _ in range(pose.repeats)]
    # Table order, with each pose's repeats ADJACENT: the microphone moves once
    # per distinct pose, so a repeat that drifted apart would be a second trip.
    # Each stop is one take of its pose, and states the pose's sweeps, else the preset's.
    assert [(s.pose, s.sweeps_per_take) for s in request.stops] == [
        (replace(pose, repeats=1, sweeps_per_take=None), pose.sweeps_per_take or program.sweeps_per_take)
        for pose in program.poses for _ in range(pose.repeats)
    ]
    assert (request.program, request.layout) == (program.preset, program.layout)


def test_a_program_beyond_the_arms_reach_refuses_at_statement_time() -> None:
    """A program says WHERE to measure; the mover says what it can reach."""
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.request_for_preset(
            mp.run_preset("speaker", "baseline_express"), mover=ac.MOVER_ARM,
        )

    assert excinfo.value.reason == ac.WALK_OVER_MOVER_ENVELOPE


# --------------------------------------------------------------------------- #
# 8. the candidate cycle: what a stop measures, and what it may not play
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("program", "candidates"),
    [
        (mp.preset("tournament/express"), ()),
        (mp.preset("tournament/express"), ("fp-a", "fp-b")),
        (mp.run_preset("tournament", "tournament_full"), ("fp-a", "fp-b", "fp-c")),
        (mp.run_preset("speaker", "baseline_express"), ("fp-a", "fp-b")),
        (mp.run_preset("rear/express", "rear_behind"), ("base", "fp-a", "muted")),
    ],
    ids=["no-cycle", "one-pose", "three-poses", "with-repeats", "rear-behind"],
)
def test_candidates_expand_pose_major_candidate_minor(
    program: mp.Preset, candidates: tuple[str, ...],
) -> None:
    """A cycle costs CAPTURES and never travel.

    The variants at one pose are adjacent stops, so the microphone still moves
    once per distinct pose and two candidates are only ever compared from the
    same place.
    """
    request = ac.request_for_preset(program, candidates=candidates)
    cycle = candidates or ("base",)

    assert {stop.purpose for stop in request.stops} == {program.purpose}
    captures = sum(pose.repeats for pose in program.poses)
    assert len(request.stops) == captures * len(cycle)
    # Candidate-minor: the cycle repeats intact under every capture.
    assert [s["candidate_id"] for s in request.to_dict()["stops"]] == list(cycle) * captures
    # Pose-major: one contiguous run per table row, so nothing walks twice.
    assert len(request.stops) > 0
    runs = [
        key for key, _ in itertools.groupby(
            s.pose.place for s in request.stops
        )
    ]
    assert len(runs) == len(program.poses)
    assert len(set(runs)) == program.mic_move_count
    assert {stop.regime for stop in request.stops} == {
        ac.REGIME_SUMMED if candidates else ac.REGIME_PER_DRIVER,
    }


def _candidate_batch_plan(request):
    captures = prepare_plan_captures(request, roles_bands=_ROLES_BANDS)
    return capture_plan.build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id)
         for c in captures if c.spec.program_phase == PHASE_LATERAL],
        acknowledgement_binding="candidate-batch-test",
    ).capture_plan


def test_three_configs_at_three_poses_use_three_placement_grants():
    request = ac.request_for_preset(
        mp.run_preset("tournament", "tournament_full"), candidates=("base", "fp-a", "fp-b"),
    )
    entries = _candidate_batch_plan(request).entries
    gate = PositionGate()
    grants = []
    for offset, entry in enumerate(entries):
        index = entry.index + 1
        if offset % 3 == 0:
            with pytest.raises(CaptureBeginDeferred):
                gate.gate(index, index, entry)
            assert gate.published()["current"] is None
            pending = gate.published()["pending"]
            grants.append((pending["degrees"], pending["vertical_deg"]))
            gate.release(**{name: pending["actions"][0]["body"][name] for name in ("index", "attempt")})
        gate.gate(index, index, entry)
        # Every grant publishes what it is recording — the released config and
        # the ones the batch shortcut admits under it alike, since only this
        # moves while the microphone stays put.
        current = gate.published()["current"]
        assert current["index"] == index
        assert current["batch"] == {
            "start": index - offset % 3, "size": 3, "ordinal": offset % 3 + 1,
        }
        assert entry.screen[POSITION_BATCH_CONFIG_KEY] == str(offset % 3 + 1)
        assert entry.screen[POSITION_BATCH_SIZE_KEY] == "3"
        assert entry.screen[POSITION_BATCH_START_KEY] == str(index - offset % 3)
        assert entry.screen["candidate_id"] == ("", "fp-a", "fp-b")[offset % 3]
    assert len(grants) == len(set(grants)) == 3
    gate.abandon_hold()
    assert gate.published()["current"] is None


def test_a_retake_or_recovery_needs_a_new_grant_and_rejects_stale_actions():
    request = ac.request_for_preset(
        mp.run_preset("tournament", "tournament_full"), candidates=("base", "fp-a", "fp-b"),
    )
    first, second, third = _candidate_batch_plan(request).entries[:3]
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 1, first)
    gate.release(1, 1)
    gate.gate(1, 1, first)
    gate.gate(2, 2, second)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(2, 3, second)
    assert gate.published()["pending"]["mover"] == ac.MOVER_HUMAN
    for index, attempt in ((1, 1), (2, 2), (2, None)):
        with pytest.raises(ValueError):
            gate.release(index, attempt)
    assert gate.published()["pending"]["attempt"] == 3
    gate.release(2, 3)
    gate.gate(2, 3, second)
    gate.abandon_hold()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(2, 3, second)
    gate.release(2, 3)
    gate.abandon_hold()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(3, 4, third)


# --------------------------------------------------------------------------- #
# 7. categorized poses: a seat is stated from the head, a close from the baffle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "layout", ["seat_cube", "seat_express"],
)
def test_a_categorized_program_walks_summed_whatever_the_candidates_say(layout: str) -> None:
    """The room is measured THROUGH the speaker stage it sits on.

    So a seat pose is a SUMMED capture even with no candidate named, which for
    a bearing selects per-driver. The category and the head offset ride from
    the table's pose onto the stop unchanged.
    """
    program = mp.run_preset("room", layout)
    request = ac.request_for_preset(program, candidates=())

    assert {stop.regime for stop in request.stops} == {ac.REGIME_SUMMED}
    assert [s.pose for s in request.stops] == list(program.poses)


@pytest.mark.parametrize(
    "stop",
    [
        ac.AngleStop(mp.Pose(0, 0, kind=mp.POSE_KIND_SEAT, seat_offset_m=(0.0, 0.0, 0.0)), ac.REGIME_SUMMED, purpose="room"),
        ac.AngleStop(mp.Pose(0, 0, kind=mp.POSE_KIND_CLOSE, distance_m=0.3), ac.REGIME_SUMMED, purpose="reference"),
    ],
    ids=["seat", "close"],
)
def test_an_arm_reaches_bearings_at_the_mark_and_nothing_else(
    stop: ac.AngleStop,
) -> None:
    """The arm TURNS at the mark, so no pose stated from anywhere else is its.

    Refused at statement time, in the same words a bearing past its envelope
    is refused in -- a person walks the same stop without argument.
    """
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(stops=(stop,), mover=ac.MOVER_ARM)

    assert excinfo.value.reason == ac.WALK_OVER_MOVER_ENVELOPE
    assert ac.AngleCaptureRequest(stops=(stop,), mover=ac.MOVER_HUMAN).stops == (stop,)


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "nearfield"},
        {"kind": mp.POSE_KIND_SEAT},
        {"seat_offset_m": (0.0, 0.0, 0.0)},
        {"kind": mp.POSE_KIND_SEAT, "seat_offset_m": (0.0, 0.0)},
        {"distance_m": 0},
        {"distance_m": -1},
    ],
    ids=[
        "unknown-kind", "seat-with-no-offset", "bearing-with-an-offset",
        "an-offset-of-two", "no-distance", "a-distance-behind-the-speaker",
    ],
)
def test_a_pose_refuses_a_kind_it_cannot_state(fields: dict) -> None:
    """A pose states a place completely or refuses -- never half of one."""
    with pytest.raises(ValueError):
        mp.Pose(0, 0, **fields)


def test_a_seat_stop_is_stated_from_the_head_not_the_mark() -> None:
    """Seven places around one head, each its own sentence and no mark at all.

    A seat pose has no bearing to be at, so ``at_mark`` is false for every one
    of them and the geometry claims no mark distance -- the take record says
    where the head was instead.
    """
    program = mp.run_preset("room", "seat_cube")
    stops = ac.resolve_request(ac.request_for_preset(program))

    assert len(stops) == len({stop.prompt.text for stop in stops}) == 7
    assert not any(stop.prompt.at_mark for stop in stops)
    for stop, pose in zip(stops, program.poses):
        geometry = capture_plan.position_geometry(stop.prompt)
        assert (geometry.kind, geometry.degrees, geometry.mark_distance_m) == (
            mp.POSE_KIND_SEAT, 0, None,
        )
        assert geometry.seat_offset_m == pose.seat_offset_m


@pytest.mark.parametrize("elevation", [0, 10])
def test_position_gate_names_the_behind_pose_without_changing_the_action_body(elevation):
    request = ac.request_for_preset(mp.run_preset("rear/pair", "rear_behind"), candidates=("rear-candidate",))
    front, behind = ac.resolve_request(request)
    gate = PositionGate()
    actions = [gate.invitation(SimpleNamespace(screen={**capture_plan.position_screen_keys(stop.prompt),
               capture_plan.POSITION_VERTICAL_DEG_KEY: str(elevation)}))["actions"][0]
               for stop in (front, behind)]
    assert [action["id"] for action in actions] == ["position_ready", "position_ready"]
    assert actions[0]["label"] != actions[1]["label"]
    assert actions[0]["body"] == actions[1]["body"] == {
        "index": 1, "attempt": 1, "degrees": 0, "vertical_deg": elevation,
    }
    if elevation:
        assert all(action["label"].endswith(capture_plan.elevation_clause(elevation)) for action in actions)


def test_a_close_stop_is_a_bearing_at_its_own_distance() -> None:
    """A close pose is on the design axis, at a standoff it declares."""
    program = mp.run_preset("nearfield")
    stop = ac.resolve_request(ac.request_for_preset(program))[0]
    geometry = capture_plan.position_geometry(stop.prompt)

    assert (geometry.kind, geometry.degrees, geometry.seat_offset_m) == (
        mp.POSE_KIND_CLOSE, 0, None,
    )
    assert geometry.mark_distance_m == program.poses[0].distance_m
    assert capture_plan.position_angle_deg(stop.prompt) == 0


#: The sentences ``baseline_express`` prompts, the first four transcribed from a
#: walk resolved BEFORE poses had a kind. Copy is the half of a walk a person
#: acts on, so it is stated here literally rather than re-derived.
_ON_AXIS = (
    "Leave the microphone on the design axis (0°). "
    "On the mark, 1 m out, pointed at the speaker."
)
_LEFT_20 = (
    "Turn the microphone to -20° (20° LEFT of the design axis). "
    "Keep it 1 m from the speaker and pointed at it."
)
_RIGHT = (
    "Turn the microphone to +{deg}° ({deg}° RIGHT of the design axis). "
    "Keep it 1 m from the speaker and pointed at it."
)
_RAISED = (
    "Keep the microphone on the design axis (0°), and 10° {word} mark "
    "height — that is 7 in (18 cm) at the declared 1 m. "
    "On the mark, 1 m out, pointed at the speaker."
)

#: ``baseline_express``: ``(angle_deg, elevation_deg, prompt text, degrees,
#: vertical_deg)`` per stop, in walk order and with the mark's two takes spelled out.
_GOLDEN_BASELINE_EXPRESS = (
    (0, 0, _ON_AXIS, 0, 0),
    (0, 0, _ON_AXIS, 0, 0),
    (-20, 0, _LEFT_20, -20, 0),
    (20, 0, _RIGHT.format(deg=20), 20, 0),
    (30, 0, _RIGHT.format(deg=30), 30, 0),
    (0, -10, _RAISED.format(word="BELOW"), 0, -10),
    (0, 10, _RAISED.format(word="ABOVE"), 0, 10),
)


@pytest.mark.parametrize(
    ("candidates", "regime", "price"),
    [
        ((), ac.REGIME_PER_DRIVER, (6, 9)),
        (("base", "fpA"), ac.REGIME_SUMMED, (6, 15)),
    ],
    ids=["no-cycle", "two-candidates"],
)
def test_shipped_program_geometry_and_full_capture_price(
    candidates: tuple[str, ...], regime: str, price: tuple[int, int],
) -> None:
    request = ac.request_for_preset(
        mp.run_preset("speaker", "baseline_express"), candidates=candidates,
    )
    stops = ac.resolve_request(request)
    geometries = [capture_plan.position_geometry(stop.prompt) for stop in stops]

    assert [
        (stop.prompt.pose.azimuth_deg, stop.prompt.pose.elevation_deg, stop.regime,
         dict(stop.screen), stop.prompt.text,
         geometry.axis, geometry.degrees, geometry.mark_distance_m,
         geometry.vertical_deg)
        for stop, geometry in zip(stops, geometries)
    ] == [
        (angle, elevation, regime, {"auto_advance": "tap"}, text,
         "horizontal", degrees, 1.0, vertical)
        for angle, elevation, text, degrees, vertical in _GOLDEN_BASELINE_EXPRESS
        # Candidate-MINOR: the cycle repeats under each pose, in place.
        for _candidate in (candidates or ("",))
    ]
    assert [pose_kind_fields(geometry) for geometry in geometries] == [
        {"mark_distance_m": 1.0, "pose_kind": mp.POSE_KIND_BEARING}] * len(stops)
    captures = prepare_plan_captures(request)
    assert (mp.mic_moves(capture.stop.pose for capture in captures), len(captures)) == price


def test_the_seat_cube_banks_as_seven_distinct_ungated_seat_takes(
    tmp_path: Path,
) -> None:
    """The cube reaches the bundle as seven takes nothing can confuse.

    The evidence a later reader opens: each take says it is a seat take, says
    its response kept the room, claims no mark distance, and keys to its own
    place -- so seven poses at one bearing are seven poses, not one measured
    seven times.
    """
    bundle, = (bank_seat_round(tmp_path) / "bundle").iterdir()
    rows = bundle_measurements(bundle, phase=PHASE_LATERAL)
    takes = [
        json.loads(take_artifact_path(bundle, row.path).read_text(encoding="utf-8"))
        for row in rows
    ]

    assert len(takes) == 7
    assert {take["pose_kind"] for take in takes} == {mp.POSE_KIND_SEAT}
    assert {take["gating_applied"] for take in takes} == {False}
    assert {take["mark_distance_m"] for take in takes} == {None}
    assert {curve["role"] for take in takes for curve in take["curves"]} == {"summed"}
    assert len({doc_pose_key(take) for take in takes}) == 7


@pytest.mark.parametrize("layout", ["seat_cloud", "room_quick"])
def test_room_candidate_batch_needs_a_new_start_at_each_physical_position(layout):
    program = mp.run_preset("room", layout)
    request = ac.request_for_preset(program, mover=program.mover or ac.MOVER_HUMAN, candidates=("base", "room-fp"))
    plan = _candidate_batch_plan(request)
    entries = plan.entries
    assert len(entries) == program.capture_count * 2
    for offset, entry in enumerate(entries):
        assert entry.screen[POSITION_BATCH_CONFIG_KEY] == str(offset % 2 + 1)
        assert entry.screen[POSITION_BATCH_SIZE_KEY] == "2"
        assert entry.screen[POSITION_BATCH_START_KEY] == str(plan.entries.index(entry) - offset % 2 + 1)
        assert str(offset % 2 + 1) in entry.screen["progress"] and "2" in entry.screen["progress"]
    assert len({e.screen[POSITION_BATCH_START_KEY] for e in entries}) == program.mic_move_count


# --------------------------------------------------------------------------- #
# 9. the specs each stop plays, and the request's level policy
# --------------------------------------------------------------------------- #


def test_each_summed_stop_gets_a_spec_at_its_own_pose_and_a_per_driver_stop_none() -> None:
    """Each summed stop gets a spec at ITS pose, prompt, candidate and scope, and a
    per-driver stop gets no spec at all (it plays the phase's own program)."""
    request = ac.AngleCaptureRequest(
        stops=(
            ac.AngleStop(mp.Pose(0, 0), ac.REGIME_PER_DRIVER, purpose="speaker"),
            ac.AngleStop(mp.Pose(20, 5), ac.REGIME_SUMMED, "fp-a", purpose="speaker"),
        ),
        candidates=("base", "fp-a"),
    )
    prompts = tuple(stop.prompt for stop in ac.resolve_request(request))

    assert ac.stop_specs(request, baseline_id="banked-base", prompts=prompts) == (
        None,
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE, positions=(20,), vertical_deg=5,
                    pose_prompts=(prompts[1].text,), candidate_id="fp-a", graph_scope="candidate"),
    )


@pytest.mark.parametrize("repeats", [1, 3])
@pytest.mark.parametrize("candidates", [(), ("base",), ("base", "room-fp"), ("base", "room-fp", "base")])
def test_request_document_and_capture_schedule(repeats, candidates):
    request = ac.request_for_preset(
        mp.run_preset("room", "room_quick"), mover=ac.MOVER_ARM, candidates=candidates, repeats=repeats,
        level=ac.LevelPolicy(level_db=-25),
    )
    doc = request.to_dict()
    assert doc["candidates"] == list(candidates)
    assert [stop["candidate_id"] for stop in doc["stops"]] == list(candidates or ("base",)) * 3
    assert doc["level"] == {"level_db": -25}
    assert doc["level_source"] == "operator"
    assert doc["repeats"] == repeats
    specs = ac.stop_specs(request, baseline_id="banked-base",
                          prompts=[s.prompt for s in ac.resolve_request(request)])
    assert {spec.kind for spec in specs if spec.candidate_id == "banked-base"} == {MEASURE_KIND_VERIFY}
    assert all(spec.kind == MEASURE_KIND_CANDIDATE for spec in specs if spec.candidate_id == "room-fp")
    assert len(specs) == 3 * max(1, len(candidates)) * repeats
    assert [spec.positions for spec in specs] == [
        (angle,) for angle in (0, -20, 20) for _ in range(max(1, len(candidates)) * repeats)
    ]
    captures = prepare_plan_captures(request)
    assert (len(captures), mp.mic_moves(capture.stop.pose for capture in captures)) == (len(specs), 3)


@pytest.mark.parametrize("row, pair", [("branches", ("woofer", "tweeter")),
                                       ("front_rear", ("woofer", "woofer:rear"))])
def test_a_branch_row_names_its_pair_onto_the_spec(row, pair):
    """The registry row, not the box's acoustic roles, decides which two targets
    a branch take excites."""
    request = ac.request_for_preset(mp.preset(row), candidates=("trial",))
    assert {stop.branch_pair for stop in request.stops} == {mp.preset(row).branch_pair}
    specs = ac.stop_specs(request, baseline_id="banked-base", roles_bands=_ROLES_BANDS,
                          prompts=[stop.prompt for stop in ac.resolve_request(request)])
    assert {spec.branch_target_ids for spec in specs} == {pair}


def test_only_a_branches_stop_may_name_a_branch_pair():
    with pytest.raises(contracts.CrossoverV2FlowError):
        ac.AngleStop(mp.Pose(0, 0), ac.REGIME_SUMMED, branch_pair=mp.BRANCH_PAIR_FRONT_REAR, purpose="speaker")


@pytest.mark.parametrize("preset, candidates, parent", [
    ("rear/pair", (), "applied"), ("rear/pair", ("base",), "applied"), ("rear/pair", ("fp-a",), "fp-a"),
    ("rear/pair", ("fp-a", "fp-b"), None),
    ("branches/express", (), None), ("branches/express", ("base",), None), ("branches/express", ("fp-a",), "fp-a"),
])
def test_a_rear_pair_plays_its_parent_with_the_rear_stage_cleared(preset, candidates, parent):
    """A rear pair's parent is the applied base unless one saved candidate is
    named; any other pair names one (ADR-0386). A rear pair plays its parent with
    the rear stage, bass and room cleared (ADR-0436)."""
    row = mp.run_preset(preset)
    if parent is None:
        with pytest.raises(ac.LateralWalkRefused) as refused:
            ac.request_for_preset(row, candidates=candidates)
        assert refused.value.reason == ac.REASON_MEASUREMENT_CANDIDATE_REQUIRED
        return
    request = ac.request_for_preset(row, candidates=candidates)
    specs = ac.stop_specs(request, baseline_id="applied", roles_bands=_ROLES_BANDS,
                          prompts=[stop.prompt for stop in ac.resolve_request(request)])
    cleared = ("room_correction", "bass_extension", "rear_calibration") if row.purpose == mp.PURPOSE_REAR else ()
    assert {(spec.graph_scope, spec.candidate_id, spec.cleared_layers) for spec in specs} == {
        ("candidate_branches", parent, cleared)}


@pytest.mark.parametrize("level_db", [math.nan, math.inf, -math.inf, True, "-20", 1, -60, -1000])
def test_invalid_level_policy_refuses_at_construction(level_db):
    with pytest.raises(ac.LateralWalkRefused) as refused:
        ac.LevelPolicy(level_db=level_db)
    assert refused.value.reason == ac.WALK_LEVEL_POLICY_INVALID


@pytest.mark.parametrize("fields", [{"repeats": v} for v in (0, -1, True, 1.5)])
def test_invalid_walk_fields_refuse_by_name(fields):
    with pytest.raises(ac.LateralWalkRefused) as refused:
        replace(_stops_at([0], regimes=(ac.REGIME_SUMMED,)), **fields)
    assert refused.value.reason == ac.WALK_LEVEL_POLICY_INVALID


@pytest.mark.parametrize("program,layout,mover,reason", [
    ("room", "room_quick", ac.MOVER_ARM, None),
    ("room", "room_quick", ac.MOVER_HUMAN, ac.REASON_WALK_MOVER_MISMATCH),
    ("room", "seat_cloud", ac.MOVER_HUMAN, None),
    ("room", "seat_cloud", ac.MOVER_ARM, ac.WALK_OVER_MOVER_ENVELOPE),
])
def test_program_mover_constraints_refuse_by_name(program, layout, mover, reason):
    row = mp.run_preset(program, layout)
    if reason is None:
        assert ac.request_for_preset(row, mover=mover).mover == mover
    else:
        with pytest.raises(ac.LateralWalkRefused) as refused:
            ac.request_for_preset(row, mover=mover)
        assert refused.value.reason == reason


def test_stop_specs_places_the_banked_baseline_without_opening_it(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("pure planning opened the candidate bank")
    monkeypatch.setattr("jasper.active_speaker.candidate_parts.baseline_candidate_id", unexpected)
    request = _stops_at([0, 20], regimes=(ac.REGIME_SUMMED,))
    specs = ac.stop_specs(request, baseline_id="banked-base",
                          prompts=[stop.prompt for stop in ac.resolve_request(request)])
    assert [spec.candidate_id for spec in specs] == ["banked-base", "banked-base"]
    assert {spec.graph_scope for spec in specs} == {"candidate"}


def test_a_stop_is_one_take_of_its_pose():
    """A layout's take count repeats the stop, so a stop's pose states none,
    and a stop whose pose states one refuses: the request's ``repeats`` is the
    one per-stop repeat (#5737 F1)."""
    row = mp.run_preset("speaker", "baseline_express")
    doc = ac.request_for_preset(row).to_dict()
    assert [stop["pose"] for stop in doc["stops"]].count({"azimuth_deg": 0, "elevation_deg": 0}) == row.poses[0].repeats > 1
    assert not any("repeats" in stop["pose"] for stop in doc["stops"])
    with pytest.raises(contracts.CrossoverV2FlowError):
        ac.AngleStop(mp.Pose(0, 0, repeats=3), ac.REGIME_PER_DRIVER, purpose="speaker")


_NEAR_FIELD, _CEILING = mp.preset("nearfield/each").stimulus, {"ceiling_hz": 1100.0}


@pytest.mark.parametrize("regime,driver,stimulus", [
    (mp.REGIME_SUMMED, "woofer", _CEILING), (mp.REGIME_SUMMED, "", _NEAR_FIELD),
    (mp.REGIME_SUMMED, "woofer", {**_NEAR_FIELD, "gap_s": 3.0}),
    (mp.REGIME_PER_DRIVER, "", _CEILING)],
    ids=["ceiling-on-one-driver", "band-on-the-candidate-graph", "gap-past-the-standby-bound", "ceiling-on-each-driver"])
def test_a_stop_whose_stimulus_cannot_play_refuses_by_name(regime, driver, stimulus):
    """A plan's stimulus is judged before anything composes it (#5737): a band
    row plays on one driver alone, with silences the analysis and the amplifier
    both keep, and no stimulus row plays on the candidate graph (ADR-0431)."""
    with pytest.raises(ac.LateralWalkRefused) as refused:
        ac.AngleStop(mp.Pose(0, 0, driver=driver), regime, purpose="speaker", stimulus=stimulus)
    assert refused.value.reason == ac.WALK_STIMULUS_NOT_ACCEPTED
